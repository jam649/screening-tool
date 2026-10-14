#!/usr/bin/env python3
"""Fixture-only M5 gate INSIDE a disposable Compose container, never a Docker driver.

First run frontend/tests/real-csv.spec.ts on a NEW volume. Copy this script into
that container and invoke its packaged Python with --run-fixture-gate. It refuses
other installations and previously used gate volumes. Business mutations use the
real localhost API and packaged CLI; this process opens SQLite read-only. There
are no external downloads, ERP polls, customer recipients, volume resets, secret
prints or MIME prints. Private fixture MIME/exports and a safe restart checkpoint
remain in /data/milestone5-gate. Root performs the actual external restart check.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import zipfile
from copy import deepcopy
from datetime import datetime, timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.canonical import canonical_hash
from app.config import DISCLAIMER, Config
from app.csl.parser import parse_bulk
from app.models import (
    AuditLog,
    CSLEntry,
    CSLVersion,
    Disposition,
    Hit,
    NotificationBatch,
    NotificationDelivery,
    OperationAttempt,
    OperationLease,
    Party,
    RunPartyEvidence,
    ScreeningRun,
    ScreeningTask,
    Setting,
    Source,
    Suppression,
    User,
)
from app.notifications.fixture import CaptureServer
from app.screening.engine import snapshot
from app.screening.normalize import normalize_country, party_record_hash
from app.screening.score import entry_type, score_pair
from app.screening.settings import load_settings
from app.security import COOKIE_NAME, decrypt_json

BASE = "http://localhost:8000"
FIXTURES = Path("/app/fixtures")
DESTINATION = Path("/data/milestone5-gate")
ADMIN_EMAIL = "docker-gate-admin@example.test"
# Public dummy password from frontend/tests/real-csv.spec.ts, never a customer secret.
FIXTURE_PASSWORD = "Synthetic gate password 2026!"
RECIPIENTS = ("milestone5-one@example.test", "milestone5-two@example.test")
CSV_LABEL = "Synthetic 2060-party gate"


class GateFailure(RuntimeError):
    """Only a fixed nonsecret check identifier may reach stdout."""


def require(value, check):
    if not value:
        raise GateFailure(check)


def require_synthetic_installation(users, sources):
    require(
        len(users) == 1
        and users[0]["email"] == ADMIN_EMAIL
        and users[0]["role"] == "admin"
        and users[0]["active"],
        "synthetic_admin_only",
    )
    require(
        len(sources) == 1
        and sources[0]["kind"] == "csv"
        and sources[0]["label"] == CSV_LABEL
        and not sources[0].get("credentials_enc"),
        "synthetic_csv_source_only",
    )


def csv_bytes(rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


def upload_form(source, rows):
    """Reuse saved book mappings; explicitly map only the new control CSV."""
    if source.get("column_mapping"):
        return {}
    columns = {"external_id", "name", "party_type", "role", "country"}
    require(
        bool(rows) and all(set(row) == columns for row in rows),
        "unmapped_upload_is_exact_control_schema",
    )
    return {"mapping": json.dumps({column: column for column in sorted(columns)})}


def message_parts(raw):
    message = BytesParser(policy=policy.default).parsebytes(raw)
    text, html = (
        message.get_body(preferencelist=("plain",)),
        message.get_body(preferencelist=("html",)),
    )
    require(text is not None and html is not None, "mime_alternatives")
    return (
        message,
        text.get_content().replace("\r\n", "\n"),
        html.get_content().replace("\r\n", "\n"),
    )


def private_write(path, body):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(body)


def immutable_hit(record):
    return canonical_hash(
        {key: value for key, value in snapshot(record).items() if key != "status"}
    )


def require_human_interaction(decision, event, historical_hit, authority):
    """Prove one original human action, including its immutable fan-out rows.

    Each covered observation has a distinct Disposition UUID. The recorded
    queue_hit_id identifies the actual human interaction; matching the actor,
    note and exact historical pair prevents accepting an unrelated later action.
    """
    after = event.get("after") or {}
    shared = (
        "queue_hit_id",
        "party_id",
        "csl_entry_id",
        "party_hash",
        "entry_hash",
        "kind",
        "note",
        "actor",
    )
    require(
        event.get("action") == "review.disposition"
        and event.get("target_table") == "dispositions"
        and event.get("target_id") == decision.get("id")
        and decision.get("actor_user_id") is not None
        and event.get("actor_user_id") == decision.get("actor_user_id")
        and all(after.get(key) == authority.get(key) for key in shared)
        and isinstance(after.get("actor"), dict)
        and after.get("actor", {}).get("id") == decision.get("actor_user_id")
        and after.get("hit_id") == decision.get("hit_id") == historical_hit.get("id")
        and decision.get("kind") == after.get("kind") == "false_positive"
        and decision.get("note") == after.get("note")
        and all(
            historical_hit.get(key) == after.get(key)
            for key in ("party_id", "csl_entry_id", "party_hash", "entry_hash")
        ),
        "original_human_interaction_audit_authority",
    )


class Gate:
    def __init__(self):
        self.config = Config()
        require(
            self.config.database_url == "sqlite:////data/screening.db", "disposable_database_path"
        )
        require(not DESTINATION.exists(), "gate_has_not_already_mutated_fixture")
        self.engine = create_engine(
            "sqlite:///file:/data/screening.db?mode=ro&uri=true",
            connect_args={"check_same_thread": False},
        )
        self.http = httpx.Client(
            base_url=BASE, timeout=180, trust_env=False, follow_redirects=False
        )
        self.checkpoint = {"fixture_only": True, "passed": False}
        self.server = None
        self.stage = "fixture_preflight"

    def db(self):
        return Session(self.engine)

    def request(self, method, path, *, status=200, **kwargs):
        response = self.http.request(method, "/api/v1" + path, **kwargs)
        require(response.status_code == status, "http_" + method.lower() + path.split("?")[0])
        return response

    def api(self, method, path, **kwargs):
        return self.request(method, path, **kwargs).json()

    def cli(self, *arguments):
        # Fixed local commands only; captured streams never reach stdout/stderr.
        process = subprocess.run(
            ["screening-tool", *arguments], capture_output=True, timeout=240, check=False
        )
        require(process.returncode == 0, "cli_" + arguments[0])
        try:
            return json.loads(process.stdout)
        except (ValueError, UnicodeError):
            raise GateFailure("cli_json") from None

    def wait(self, predicate, check, seconds=90):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.2)
        raise GateFailure(check)

    def idle(self):
        def ready():
            with self.db() as db:
                return not db.scalar(
                    select(func.count())
                    .select_from(ScreeningTask)
                    .where(ScreeningTask.status != "complete")
                ) and not db.scalar(
                    select(func.count())
                    .select_from(OperationAttempt)
                    .where(OperationAttempt.status.in_(["queued", "running"]))
                )

        self.wait(ready, "durable_tasks_finished")

    def mail_idle(self):
        def ready():
            with self.db() as db:
                return not db.scalar(
                    select(OperationLease.owner_id).where(OperationLease.key == "alert-dispatch")
                )

        self.wait(ready, "outbox_lease_released")

    def preflight(self):
        self.rows = list(csv.DictReader(io.StringIO((FIXTURES / "parties.csv").read_text())))
        require(len(self.rows) == 2060, "packaged_party_fixture")
        with self.db() as db:
            users, sources, parties = (
                list(db.scalars(select(User))),
                list(db.scalars(select(Source))),
                list(db.scalars(select(Party))),
            )
            require_synthetic_installation(
                [{"email": u.email, "role": u.role, "active": u.active} for u in users],
                [
                    {"kind": s.kind, "label": s.label, "credentials_enc": s.credentials_enc}
                    for s in sources
                ],
            )
            require(
                len(parties) == 2060 and all(p.deleted_at is None for p in parties),
                "fresh_fixture_parties",
            )
            by_external = {p.external_id: p for p in parties}
            require(set(by_external) == {r["external_id"] for r in self.rows}, "fixture_stable_ids")
            for row in self.rows:
                party = by_external[row["external_id"]]
                require(
                    party.name == row["name"]
                    and party.party_type == row["party_type"]
                    and party.role == row["role"]
                    and party.country_iso2 == normalize_country(row["country"]),
                    "untouched_synthetic_party_inputs",
                )
                require(
                    party.record_hash == party_record_hash(snapshot(party)),
                    "current_type_aware_hashes",
                )
            entries = list(db.scalars(select(CSLEntry)))
            expected = parse_bulk((FIXTURES / "csl_sample_v1.json").read_bytes())
            require(
                len(entries) == 300 and all(e.removed_version is None for e in entries),
                "v1_list_only",
            )
            require(
                {(e.source_list, e.csl_id): e.entry_hash for e in entries}
                == {(e.source_list, e.csl_id): e.entry_hash for e in expected.entries},
                "untouched_v1_listing_hashes",
            )
            require(
                db.scalar(select(Disposition)) is None
                and db.scalar(select(NotificationBatch)) is None,
                "no_prior_gate_decisions_or_mail",
            )
            settings = load_settings(db)
            require(
                not settings.auto_clear_enabled
                and settings.review_threshold == 65
                and settings.likely_threshold == 90,
                "default_fixture_scoring",
            )
            values = {s.key: s.value for s in db.scalars(select(Setting))}
            require(
                not values.get("smtp_host")
                and not values.get("report_recipients")
                and not values.get("csl_api_key")
                and values.get("retention_years") is None,
                "no_customer_outbound_or_retention_configuration",
            )
            self.source_id, self.admin_id = sources[0].id, users[0].id
            self.original_hits = {h.id: immutable_hit(h) for h in db.scalars(select(Hit))}
            self.original_runs = {
                r.id: canonical_hash(snapshot(r)) for r in db.scalars(select(ScreeningRun))
            }
            self.original_evidence = {
                e.id: canonical_hash(snapshot(e)) for e in db.scalars(select(RunPartyEvidence))
            }
        login = self.request(
            "POST",
            "/auth/login",
            json={"email": ADMIN_EMAIL, "password": FIXTURE_PASSWORD},
            headers={"Origin": BASE},
        )
        token = login.cookies.get(COOKIE_NAME)
        require(
            bool(token) and "Secure" in login.headers.get("set-cookie", ""),
            "secure_cookie_fixture_transport",
        )
        self.http.headers.update(
            {
                "Cookie": f"{COOKIE_NAME}={token}",
                "X-CSRF-Token": login.json()["csrf_token"],
                "Origin": BASE,
            }
        )
        self.idle()
        require(self.api("GET", "/queue")["total"] == 77, "real_csv_seed_queue_77")
        require(
            self.api("GET", "/dashboard")["coverage_latest_version"] == 2060,
            "real_csv_seed_coverage",
        )
        schedules = self.api("GET", "/monitoring/status")
        require(
            schedules["enabled"] and schedules["schedules"], "real_persistent_scheduler_enabled"
        )
        # Move autonomous cron work safely beyond this local fixture run, through
        # audited public settings. Explicit fixture CLI refreshes still run below.
        timezone = ZoneInfo(self.api("GET", "/settings")["timezone"])
        local_now = datetime.now(timezone)
        self.report_time = (local_now + timedelta(hours=13)).strftime("%H:%M")
        self.api(
            "PATCH",
            "/settings/monitoring",
            json={
                "csl_fetch_time": (local_now + timedelta(hours=12)).strftime("%H:%M"),
                "retention_time": (local_now + timedelta(hours=14)).strftime("%H:%M"),
            },
        )
        DESTINATION.mkdir(mode=0o700)
        self.checkpoint.update(
            {
                "initial_parties": 2060,
                "initial_entries": 300,
                "initial_queue": 77,
                "admin_id": self.admin_id,
                "source_id": self.source_id,
            }
        )

    def import_rows(self, source_id, rows):
        source = self.api("GET", f"/sources/{source_id}")
        result = self.api(
            "POST",
            f"/sources/{source_id}/upload",
            files={"file": ("complete-fixture.csv", csv_bytes(rows), "text/csv")},
            data=upload_form(source, rows),
        )
        require(
            result["applied"] and not result["rejected_count"] and not result["screening_errors"],
            "complete_fixture_import_and_screen",
        )
        self.idle()
        return result

    def hit_for(self, party_id, entry_id, run_id=None):
        with self.db() as db:
            query = select(Hit).where(Hit.party_id == party_id, Hit.csl_entry_id == entry_id)
            if run_id:
                query = query.where(Hit.run_id == run_id)
            hit = db.scalar(query.order_by(Hit.created_at.desc(), Hit.id.desc()))
            require(hit is not None, "actual_target_comparison")
            hit_id = hit.id
        return self.api("GET", f"/hits/{hit_id}")

    def original_human_link(self, payload):
        require(
            payload and payload.get("id") in self.human_interaction_events,
            "linked_decision_belongs_to_original_human_interaction",
        )
        with self.db() as db:
            decision = db.get(Disposition, payload["id"])
            require(decision is not None, "linked_original_human_decision_exists")
            historical_hit = db.get(Hit, decision.hit_id)
            require(historical_hit is not None, "original_human_hit_exists")
            require_human_interaction(
                snapshot(decision),
                self.human_interaction_events[decision.id],
                snapshot(historical_hit),
                self.human_interaction_authority,
            )
        require(
            payload.get("actor_user_id") == self.admin_id
            and payload.get("actor") == self.human_interaction_authority["actor"]
            and payload.get("note") == self.human_interaction_authority["note"]
            and payload.get("kind") == "false_positive",
            "linked_original_human_payload_actor_note",
        )

    def human_memory(self):
        self.stage = "human_false_positive_and_hash_edit"
        with self.db() as db:
            party = db.scalar(select(Party).where(Party.external_id == "case-exact-02"))
            self.memory_party_id, self.old_party_hash = party.id, party.record_hash
        hit = next(
            h for h in self.api("GET", "/queue")["items"] if h["party_id"] == self.memory_party_id
        )
        self.memory_entry_id = hit["csl_entry_id"]
        note = "Milestone 5 synthetic human false-positive proof; no customer record."
        result = self.api(
            "POST",
            f"/hits/{hit['id']}/dispositions",
            status=201,
            json={"kind": "false_positive", "note": note},
        )
        decision = result["disposition"]
        self.memory_decision_id = decision["id"]
        require(
            decision["actor_user_id"] == self.admin_id and decision["note"] == note,
            "real_human_attribution",
        )
        with self.db() as db:
            count = db.scalar(select(func.count()).select_from(Disposition))
            self.human_fingerprints = {
                d.id: canonical_hash(snapshot(d)) for d in db.scalars(select(Disposition))
            }
            events = {
                event.target_id: snapshot(event)
                for event in db.scalars(
                    select(AuditLog).where(AuditLog.action == "review.disposition")
                )
                if (event.after or {}).get("queue_hit_id") == hit["id"]
            }
            require(
                self.memory_decision_id in events
                and len(events) == result["observations_dispositioned"],
                "human_anchor_and_all_observation_audit_proofs",
            )
            self.human_interaction_events = events
            self.human_interaction_authority = deepcopy(events[self.memory_decision_id]["after"])
            remembered = db.scalar(
                select(Suppression).where(
                    Suppression.party_id == self.memory_party_id,
                    Suppression.csl_entry_id == self.memory_entry_id,
                    Suppression.party_hash == self.old_party_hash,
                    Suppression.entry_hash == hit["entry_hash"],
                )
            )
            require(
                remembered and remembered.disposition_id == self.memory_decision_id,
                "remembered_exact_pair_points_to_clicked_anchor_decision",
            )
        self.original_human_link(decision)
        repeat = self.api("POST", "/screen", json={"party_id": self.memory_party_id})
        observed = self.hit_for(self.memory_party_id, self.memory_entry_id, repeat["id"])
        require(
            observed["status"] == "suppressed" and not observed["dispositions"],
            "repeat_suppressed_without_fake_decision",
        )
        self.original_human_link(observed["suppression_disposition"])
        with self.db() as db:
            require(
                db.scalar(select(func.count()).select_from(Disposition)) == count,
                "repeat_no_new_human_decisions",
            )
        rows = deepcopy(self.rows)
        target = next(r for r in rows if r["external_id"] == "case-exact-02")
        target["name"] += " LLC"
        target["party_type"] = "company" if target["party_type"] == "individual" else "individual"
        imported = self.import_rows(self.source_id, rows)
        require(
            imported["changed"] == 1 and imported["unchanged"] == 2059 and not imported["deleted"],
            "one_stable_id_type_name_edit",
        )
        reopened = self.hit_for(self.memory_party_id, self.memory_entry_id)
        require(
            reopened["party_hash"] != self.old_party_hash and reopened["status"] == "open",
            "changed_hash_reopened_before_slider",
        )
        prior = reopened["prior_disposition"]
        self.original_human_link(prior)
        require(
            prior and prior["actor"]["id"] == self.admin_id and prior["note"] == note,
            "reopened_original_actor_note",
        )
        require(
            "name" in prior["what_changed"]["party"]
            and "party_type" in prior["what_changed"]["party"],
            "real_name_type_before_after_diff",
        )
        self.checkpoint.update(
            {
                "human_disposition_id": self.memory_decision_id,
                "human_queue_anchor_hit_id": hit["id"],
                "human_interaction_disposition_ids": sorted(self.human_interaction_events),
                "human_interaction_audit_sha256": canonical_hash(self.human_interaction_events),
                "effective_suppression_disposition_id": observed["suppression_disposition"]["id"],
                "prior_human_disposition_id": prior["id"],
                "original_party_hash": self.old_party_hash,
                "changed_party_hash": reopened["party_hash"],
                "suppressed_observation_run_id": repeat["id"],
                "hash_edit_run_ids": imported["screening_run_ids"],
            }
        )

    def controls(self):
        self.stage = "acknowledged_strict_review_only_auto_clear"
        with self.db() as db:
            settings = load_settings(db)
            v2 = parse_bulk((FIXTURES / "csl_sample_v2.json").read_bytes())
            stable = {(e.source_list, e.csl_id): e.entry_hash for e in v2.entries}
            chosen = None
            for entry in db.scalars(select(CSLEntry)):
                country = next(
                    (normalize_country(c) for c in entry.countries if normalize_country(c)), None
                )
                if (
                    not country
                    or entry_type(entry) != "company"
                    or stable.get((entry.source_list, entry.csl_id)) != entry.entry_hash
                ):
                    continue
                wrong = next(
                    (c for c in ("CA", "NZ", "US", "GB") if c not in entry.countries), None
                )
                if not wrong:
                    continue
                control = SimpleNamespace(
                    name=entry.name,
                    alt_names=[],
                    party_type="individual",
                    country_iso2=wrong,
                    city=None,
                    address_lines=[],
                )
                score = score_pair(control, entry, settings).score
                if float(score).is_integer() and 65 <= score < 90:
                    chosen = (entry.id, entry.name, country, wrong, int(score))
                    break
            require(chosen, "real_stable_review_control_available")
            entry_id, name, country, wrong, boundary = chosen
        source = self.api(
            "POST", "/sources", status=201, json={"label": "Milestone 5 synthetic slider controls"}
        )
        rows = [
            {
                "external_id": "m5-review",
                "name": name,
                "party_type": "individual",
                "role": "vendor",
                "country": wrong,
            },
            {
                "external_id": "m5-likely",
                "name": name,
                "party_type": "company",
                "role": "vendor",
                "country": country,
            },
        ]
        imported = self.import_rows(source["id"], rows)
        with self.db() as db:
            ids = {
                p.external_id: p.id
                for p in db.scalars(select(Party).where(Party.source_id == source["id"]))
            }
            old_hits = {h.id: canonical_hash(snapshot(h)) for h in db.scalars(select(Hit))}
            tasks = db.scalar(select(func.count()).select_from(ScreeningTask))
        self.review_party_id, self.likely_party_id = ids["m5-review"], ids["m5-likely"]
        require(
            self.hit_for(self.review_party_id, entry_id)["final_confidence"] == boundary,
            "control_review_score_real",
        )
        require(
            self.hit_for(self.likely_party_id, entry_id)["det_score"] >= 90,
            "control_likely_score_real",
        )
        rejected = self.http.patch(
            "/api/v1/settings", json={"auto_clear_enabled": True, "auto_clear_threshold": boundary}
        )
        require(rejected.status_code == 422, "enable_requires_exact_typed_acknowledgment")
        enabled = self.api(
            "PATCH",
            "/settings",
            json={
                "auto_clear_enabled": True,
                "auto_clear_threshold": boundary,
                "auto_clear_acknowledgment": "I understand",
            },
        )
        require(
            enabled["auto_clear_acknowledgment_valid"] and not enabled["pending_screening_task_id"],
            "acknowledged_slider_future_only",
        )
        ack_id = enabled["auto_clear_acknowledgment_audit_id"]
        with self.db() as db:
            proof = db.get(AuditLog, ack_id)
            require(
                proof.actor_user_id == self.admin_id
                and proof.after["acknowledgment"] == "I understand"
                and proof.after["threshold"] == boundary,
                "immutable_slider_admin_proof",
            )
            require(
                db.scalar(select(func.count()).select_from(ScreeningTask)) == tasks
                and all(
                    canonical_hash(snapshot(db.get(Hit, id))) == value
                    for id, value in old_hits.items()
                ),
                "slider_enable_no_prior_hit_mutation_or_task",
            )
        equal_run = self.api("POST", "/screen", json={"party_id": self.review_party_id})
        equal = self.hit_for(self.review_party_id, entry_id, equal_run["id"])
        require(
            equal["status"] == "open" and equal["bucket"] == "review" and not equal["dispositions"],
            "equal_boundary_remains_open",
        )
        with self.db() as db:
            equal_frozen = canonical_hash(snapshot(db.get(Hit, equal["id"])))
            tasks = db.scalar(select(func.count()).select_from(ScreeningTask))
        moved = self.api("PATCH", "/settings", json={"auto_clear_threshold": 90})
        require(not moved["pending_screening_task_id"], "slider_move_no_full_task")
        with self.db() as db:
            require(
                canonical_hash(snapshot(db.get(Hit, equal["id"]))) == equal_frozen
                and db.scalar(select(func.count()).select_from(ScreeningTask)) == tasks,
                "slider_move_preserves_old_observation",
            )
        auto_run = self.api("POST", "/screen", json={"party_id": self.review_party_id})
        auto = self.hit_for(self.review_party_id, entry_id, auto_run["id"])
        require(
            auto["bucket"] == "review"
            and auto["final_confidence"] < 90
            and auto["status"] == "dispositioned",
            "future_strict_review_auto_clear",
        )
        decisions = auto["dispositions"]
        require(
            len(decisions) == 1
            and decisions[0]["kind"] == "auto_cleared"
            and decisions[0]["actor_user_id"] is None
            and decisions[0]["slider_value_at_time"] == 90,
            "immutable_system_slider_decision",
        )
        require(
            auto_run["id"] in decisions[0]["note"]
            and "det_score=" in decisions[0]["note"]
            and "final_confidence=" in decisions[0]["note"]
            and "deterministic-only" in decisions[0]["note"],
            "system_score_run_model_note",
        )
        require(auto_run["auto_cleared"] >= 1, "run_auto_clear_counter")
        self.api(
            "PATCH",
            "/settings/notifications",
            json={
                "smtp_host": "127.0.0.1",
                "smtp_port": 2525,
                "smtp_tls": "none",
                "smtp_from": "milestone5@example.test",
                "report_recipients": list(RECIPIENTS),
                "public_base_url": "https://milestone5.example.test",
                "likely_alerts_enabled": True,
                "report_time": self.report_time,
            },
        )
        likely_run = self.api("POST", "/screen", json={"party_id": self.likely_party_id})
        likely = self.hit_for(self.likely_party_id, entry_id, likely_run["id"])
        require(
            likely["bucket"] == "likely"
            and likely["status"] == "open"
            and not likely["dispositions"],
            "deterministic_likely_never_auto_clears",
        )
        require(
            not any(
                h["party_id"] == self.review_party_id and h["csl_entry_id"] == entry_id
                for h in self.api("GET", "/queue")["items"]
            ),
            "auto_cleared_pair_not_queued",
        )
        self.checkpoint.update(
            {
                "control_source_id": source["id"],
                "review_party_id": self.review_party_id,
                "likely_party_id": self.likely_party_id,
                "auto_clear_acknowledgment_audit_id": ack_id,
                "auto_clear_run_id": auto_run["id"],
                "likely_run_id": likely_run["id"],
                "strict_boundary": boundary,
                "control_import_run_ids": imported["screening_run_ids"],
            }
        )

    def monitor_delta(self):
        self.stage = "actual_cli_fixture_delta_and_repeat"
        cycle = self.cli("monitor", "cycle", "--file", str(FIXTURES / "csl_sample_v2.json"))
        require(
            cycle["status"] == "success" and not cycle["outcome"]["failed_operations"],
            "fixture_monitor_cycle_success",
        )
        self.idle()
        with self.db() as db:
            version = db.scalar(select(CSLVersion).order_by(CSLVersion.fetched_at.desc()))
            require(
                (version.added, version.changed, version.removed, version.entry_count)
                == (5, 5, 5, 300),
                "frozen_v2_exact_5_5_5_delta",
            )
            removed = list(
                db.scalars(select(CSLEntry.id).where(CSLEntry.removed_version == version.id))
            )
            closures = list(
                db.scalars(
                    select(Disposition)
                    .join(Hit)
                    .where(
                        Hit.csl_entry_id.in_(removed),
                        Disposition.kind == "auto_cleared",
                        Disposition.slider_value_at_time.is_(None),
                    )
                )
            )
            require(
                closures and all(d.actor_user_id is None for d in closures),
                "removed_listing_system_closures",
            )
            self.checkpoint.update(
                {
                    "v2_version_id": version.id,
                    "removed_entry_closure_observations": len(closures),
                    "monitor_operation_id": cycle["id"],
                }
            )
        # The delta covers only changed entries. An explicit first full screen
        # applies the new future-only slider policy to unchanged review pairs;
        # compare the NEXT identical full screen with that committed baseline.
        policy_run = self.cli("screen", "--all")
        require(len(policy_run) == 1, "full_current_policy_baseline")
        before = self.api("GET", "/queue")
        repeated = self.cli("screen", "--all")
        require(
            len(repeated) == 1 and repeated[0]["hits_new"] == 0,
            "full_repeat_zero_new_current_queue_items",
        )
        after = self.api("GET", "/queue")
        require(
            {h["id"] for h in before["items"]} == {h["id"] for h in after["items"]},
            "unchanged_full_repeat_stable_anchors",
        )
        self.checkpoint.update(
            {
                "repeat_run_id": repeated[0]["id"],
                "policy_full_run_id": policy_run[0]["id"],
                "repeat_new_queue_items": 0,
                "current_queue": after["total"],
            }
        )

    def batch_state(self, id, status):
        with self.db() as db:
            batch = db.get(NotificationBatch, id)
            return snapshot(batch) if batch and batch.status == status else None

    def frozen_mail(self):
        self.stage = "captured_frozen_morning_and_likely_mime"
        self.server.reject_recipient = RECIPIENTS[1]
        report = self.api("POST", "/reports/morning/send", status=202)
        id = report["id"]
        self.wait(lambda: self.batch_state(id, "failed"), "morning_partial_failure_recorded")
        self.mail_idle()
        require(
            self.api("GET", "/reports/morning/status")["last_report_at"] is None,
            "partial_report_no_window_advance",
        )
        with self.db() as db:
            batch = db.get(NotificationBatch, id)
            frozen = deepcopy(decrypt_json(batch.payload_enc, self.config))
            frozen_hash, window = canonical_hash(frozen), batch.window_end.isoformat()
            recipients = {
                d.recipient: d.message_id
                for d in db.scalars(
                    select(NotificationDelivery).where(NotificationDelivery.batch_id == id)
                )
            }
        require(set(recipients) == set(RECIPIENTS), "morning_frozen_two_recipients")
        self.api(
            "PATCH",
            "/settings/notifications",
            json={
                "report_recipients": [RECIPIENTS[0]],
                "smtp_from": "changed-fixture@example.test",
                "public_base_url": "https://changed-fixture.example.test",
            },
        )
        self.server.reject_recipient = None
        self.api("POST", f"/notifications/batches/{id}/retry", status=202, json={})
        self.wait(lambda: self.batch_state(id, "sent"), "morning_retry_all_sent")
        self.mail_idle()
        require(
            self.api("GET", "/reports/morning/status")["last_report_at"] == window,
            "report_boundary_equals_frozen_as_of",
        )
        with self.db() as db:
            require(
                canonical_hash(decrypt_json(db.get(NotificationBatch, id).payload_enc, self.config))
                == frozen_hash,
                "retry_preserves_frozen_report_payload",
            )
        self.cli("alert", "send")
        with self.server.capture_lock:
            captured = list(self.server.messages)
        with self.db() as db:
            payloads = {}
            for batch in db.scalars(select(NotificationBatch)):
                payload = decrypt_json(batch.payload_enc, self.config)
                for delivery in db.scalars(
                    select(NotificationDelivery).where(NotificationDelivery.batch_id == batch.id)
                ):
                    if delivery.status == "sent":
                        payloads[delivery.message_id] = (delivery.recipient, batch.kind, payload)
        seen, kinds = set(), set()
        for recipient, raw in captured:
            message, plain, html = message_parts(raw)
            message_id = message["Message-ID"]
            require(message_id in payloads and message_id not in seen, "stable_unique_message_ids")
            expected_recipient, kind, payload = payloads[message_id]
            require(
                recipient == expected_recipient
                and message["To"] == recipient
                and recipient in RECIPIENTS,
                "fixture_only_frozen_recipient",
            )
            require(
                message["Subject"] == payload["subject"] and message["From"] == payload["sender"],
                "frozen_subject_sender",
            )
            require(
                plain.rstrip("\n") == payload["text"].replace("\r\n", "\n").rstrip("\n")
                and html.rstrip("\n") == payload["html"].replace("\r\n", "\n").rstrip("\n"),
                "mime_matches_encrypted_frozen_body",
            )
            require(
                DISCLAIMER in plain and DISCLAIMER in html, "exact_ita_disclaimer_both_mime_parts"
            )
            seen.add(message_id)
            kinds.add(kind)
        require(
            kinds == {"morning", "likely"} and set(payloads) == seen,
            "all_morning_likely_deliveries_captured",
        )
        require(
            sum(message_parts(raw)[0]["Message-ID"] in recipients.values() for _, raw in captured)
            == 2,
            "partial_retry_only_unsent_recipient",
        )
        self.checkpoint.update(
            {
                "morning_report_batch_id": id,
                "morning_window_end": window,
                "frozen_report_payload_sha256": frozen_hash,
                "captured_messages": len(captured),
                "captured_morning_and_likely": True,
            }
        )
        # Restart cannot send real mail; capture is closed after this run.
        self.api(
            "PATCH",
            "/settings/notifications",
            json={"likely_alerts_enabled": False, "report_recipients": []},
        )

    def current_auto_clear_filter(self, *, before_report):
        """The party/CSV filter covers the current successful-report window."""
        filters = {"source_id": self.checkpoint["control_source_id"], "auto_cleared": "true"}
        filtered = self.api("GET", "/parties", params={**filters, "limit": 20000})
        exported = self.request("GET", "/exports/parties.csv", params=filters)
        records = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
        require(
            exported.headers.get("x-export-sha256") == hashlib.sha256(exported.content).hexdigest()
            and exported.headers.get("cache-control") == "no-store",
            "current_filter_csv_private_checksum",
        )
        csv_ids = {r["id"] for r in records if r["id"] != "ITA disclaimer"}
        api_ids = {r["id"] for r in filtered["items"]}
        require(
            csv_ids == api_ids and len(api_ids) == filtered["total"],
            "current_csv_filters_match_party_api",
        )
        if before_report:
            require(
                self.review_party_id in api_ids,
                "pre_report_auto_cleared_party_filter_actual_system_decision",
            )
            self.checkpoint["pre_report_auto_cleared_control_parties"] = len(api_ids)
            with self.db() as db:
                self.pre_report_run_ids = set(db.scalars(select(ScreeningRun.id)))
        else:
            require(
                not api_ids and self.api("GET", "/dashboard")["auto_cleared_since_report"] == 0,
                "successful_report_clears_current_auto_clear_filter_window",
            )
            with self.db() as db:
                require(
                    set(db.scalars(select(ScreeningRun.id))) == self.pre_report_run_ids,
                    "no_new_screening_fabricated_after_report_boundary",
                )
            self.checkpoint["post_report_auto_cleared_control_parties"] = 0

    def exports_and_holds(self):
        self.stage = "actual_export_and_retention_public_endpoints"
        for selection, id in (
            ("parties", self.memory_party_id),
            ("runs", self.checkpoint["repeat_run_id"]),
        ):
            for format in ("pdf", "csv"):
                response = self.request(
                    "GET", f"/{selection}/{id}/export", params={"format": format}
                )
                require(
                    response.headers.get("cache-control") == "no-store"
                    and "attachment" in response.headers.get("content-disposition", ""),
                    "private_download_headers",
                )
                require(
                    response.headers.get("x-export-sha256")
                    == hashlib.sha256(response.content).hexdigest(),
                    "export_response_checksum",
                )
                private_write(DESTINATION / f"{selection}-{id}.{format}", response.content)
                if format == "pdf":
                    # The runtime deliberately excludes dev-only pypdf. Verify the
                    # real artifact structure/font packaging here; text/footer
                    # extraction is covered by focused export tests outside Docker.
                    require(
                        response.content.startswith(b"%PDF-")
                        and b"%%EOF" in response.content[-100:]
                        and b"/FontFile2" in response.content,
                        "real_pdf_with_packaged_embedded_font",
                    )
                elif selection == "parties":
                    require(
                        self.memory_decision_id in response.text
                        and self.old_party_hash in response.text,
                        "csv_original_human_and_hash_evidence",
                    )
        self.current_auto_clear_filter(before_report=False)
        archive_response = self.request("GET", "/exports/full.zip")
        require(
            archive_response.headers.get("x-export-sha256")
            == hashlib.sha256(archive_response.content).hexdigest(),
            "full_zip_response_checksum",
        )
        private_write(DESTINATION / "full-audit.zip", archive_response.content)
        with zipfile.ZipFile(io.BytesIO(archive_response.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            require(
                manifest["selection"]["party_count"] == 2062, "zip_complete_fixture_plus_controls"
            )
            for path, proof in manifest["files"].items():
                require(
                    hashlib.sha256(archive.read(path)).hexdigest() == proof["sha256"],
                    "zip_manifest_file_checksums",
                )
            recorded = archive.read(f"parties/{self.memory_party_id}.json")
            require(
                self.memory_decision_id.encode() in recorded
                and self.old_party_hash.encode() in recorded,
                "zip_original_human_and_hash_evidence",
            )
        require(not self.api("GET", "/retention/status")["enabled"], "retention_disabled_default")
        hold = self.api(
            "POST",
            "/retention/holds",
            status=201,
            json={
                "party_id": self.memory_party_id,
                "kind": "government",
                "reason": "Synthetic fixture government hold; preserve original proof.",
            },
        )
        rejected = self.http.post(
            f"/api/v1/retention/holds/{hold['id']}/release",
            json={"reason": "Synthetic missing authorization test"},
        )
        require(rejected.status_code == 422, "government_hold_requires_written_authorization")
        require(
            any(
                h["id"] == hold["id"] for h in self.api("GET", "/retention/status")["active_holds"]
            ),
            "public_hold_remains_active",
        )
        preview, applied = (
            self.api("POST", "/retention/preview"),
            self.api("POST", "/retention/apply"),
        )
        require(
            not preview["enabled"]
            and not preview["hits"]
            and not applied["enabled"]
            and not applied["hits"]
            and not applied["dispositions"],
            "disabled_retention_no_deletion",
        )
        require(
            not self.cli("retention", "apply")["outcome"]["enabled"],
            "cli_disabled_retention_same_policy",
        )
        self.checkpoint.update(
            {
                "retention_hold_id": hold["id"],
                "retention_disabled": True,
                "export_checksums_verified": True,
                "full_zip_sha256": hashlib.sha256(archive_response.content).hexdigest(),
            }
        )

    def finish(self):
        self.stage = "immutable_provenance_and_restart_checkpoint"
        self.idle()
        with self.db() as db:
            require(
                all(
                    immutable_hit(db.get(Hit, id)) == proof
                    for id, proof in self.original_hits.items()
                ),
                "all_original_hit_evidence_immutable",
            )
            require(
                all(
                    canonical_hash(snapshot(db.get(ScreeningRun, id))) == proof
                    for id, proof in self.original_runs.items()
                ),
                "all_original_finished_runs_immutable",
            )
            require(
                all(
                    canonical_hash(snapshot(db.get(RunPartyEvidence, id))) == proof
                    for id, proof in self.original_evidence.items()
                ),
                "all_original_party_participation_immutable",
            )
            require(
                all(
                    canonical_hash(snapshot(db.get(Disposition, id))) == proof
                    for id, proof in self.human_fingerprints.items()
                ),
                "original_human_decisions_immutable",
            )
            for run in db.scalars(select(ScreeningRun)):
                scope = run.settings_snapshot["scope"]
                require(
                    set(scope["party_ids"]) == set(scope["party_hashes"])
                    and set(scope["entry_ids"]) == set(scope["entry_hashes"]),
                    "actual_run_input_hash_manifests",
                )
                require(
                    db.scalar(
                        select(func.count())
                        .select_from(RunPartyEvidence)
                        .where(RunPartyEvidence.run_id == run.id)
                    )
                    == run.parties_screened,
                    "zero_hit_party_immutable_evidence",
                )
            self.checkpoint.update(
                {
                    "runs": db.scalar(select(func.count()).select_from(ScreeningRun)),
                    "system_auto_clear_observations": db.scalar(
                        select(func.count())
                        .select_from(Disposition)
                        .where(Disposition.slider_value_at_time.is_not(None))
                    ),
                    "original_hit_fingerprints_sha256": canonical_hash(self.original_hits),
                    "original_evidence_fingerprints_sha256": canonical_hash(self.original_evidence),
                    "original_human_fingerprints_sha256": canonical_hash(self.human_fingerprints),
                    "immutable_original_provenance_verified": True,
                }
            )
        monitoring = self.api("GET", "/monitoring/status")
        schedules = monitoring["schedules"]
        require(
            {"csl-daily", "morning-daily", "retention-weekly", "screen-recovery", "outbox-recovery"}
            <= {s["id"] for s in schedules},
            "durable_fixed_schedule_ids",
        )
        self.checkpoint.update(
            {
                "passed": True,
                "schedule_ids": sorted(s["id"] for s in schedules),
                "cron_next_runs": {
                    s["id"]: s["next_run_at"]
                    for s in schedules
                    if s["id"] in {"csl-daily", "morning-daily", "retention-weekly"}
                },
                "operation_ids": [o["id"] for o in monitoring["operations"]],
                "external_restart_check_pending": True,
            }
        )
        private_write(
            DESTINATION / "checkpoint.json", (json.dumps(self.checkpoint, indent=2) + "\n").encode()
        )
        print(json.dumps(self.checkpoint, sort_keys=True))

    def run(self):
        try:
            self.preflight()
            self.server = CaptureServer(2525, output_dir=DESTINATION / "private-mime")
            threading.Thread(target=self.server.serve_forever, daemon=True).start()
            self.human_memory()
            self.controls()
            self.monitor_delta()
            self.current_auto_clear_filter(before_report=True)
            self.frozen_mail()
            self.exports_and_holds()
            self.finish()
        finally:
            if self.server:
                self.server.shutdown()
                self.server.server_close()
            self.http.close()
            self.engine.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-fixture-gate",
        action="store_true",
        help="Authorize ONLY a disposable real-CSV fixture installation.",
    )
    args = parser.parse_args(argv)
    if not args.run_fixture_gate:
        parser.error("The explicit --run-fixture-gate flag is required")
    gate = None
    try:
        gate = Gate()
        gate.run()
    except Exception as exc:
        # No traceback/body/subprocess output/environment/cookie-bearing exception.
        check = str(exc) if isinstance(exc, GateFailure) else "unexpected_fixture_gate_error"
        print(
            json.dumps(
                {
                    "passed": False,
                    "fixture_only": True,
                    "stage": gate.stage if gate else "preflight",
                    "failed_check": check,
                }
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
