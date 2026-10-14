#!/usr/bin/env python3
"""Read-only fixture restart check INSIDE the already restarted Compose container.

Requires --run-fixture-gate and the passed /data/milestone5-gate/checkpoint.json.
The coordinator/CI performs Docker restart externally. This script never starts
Docker, sends mail, downloads a list, runs screening, updates settings, resets data
or prints credentials/message bodies. Only legitimate dummy-Admin login writes a
session. SQLite is opened in mode=ro. Output is safe counts and booleans only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import zipfile
from email import policy
from email.parser import BytesParser
from pathlib import Path

import httpx
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.canonical import canonical_hash
from app.config import Config
from app.models import (
    AuditLog,
    CSLEntry,
    CSLVersion,
    Disposition,
    Hit,
    NotificationBatch,
    NotificationDelivery,
    OperationAttempt,
    Party,
    RetentionHold,
    RunPartyEvidence,
    ScreeningRun,
    Source,
    User,
)
from app.screening.engine import snapshot
from app.screening.normalize import party_record_hash
from app.security import COOKIE_NAME, decrypt_json

BASE = "http://localhost:8000"
DIRECTORY = Path("/data/milestone5-gate")
ADMIN_EMAIL = "docker-gate-admin@example.test"
FIXTURE_PASSWORD = "Synthetic gate password 2026!"  # Public real-csv.spec.ts dummy.
CRON_IDS = frozenset({"csl-daily", "morning-daily", "retention-weekly"})
REFERENCES = {
    User: ("admin_id",),
    Source: ("source_id", "control_source_id"),
    Party: ("review_party_id", "likely_party_id"),
    Hit: ("human_queue_anchor_hit_id",),
    AuditLog: ("auto_clear_acknowledgment_audit_id",),
    CSLVersion: ("v2_version_id",),
    Disposition: (
        "human_disposition_id",
        "human_interaction_disposition_ids",
        "effective_suppression_disposition_id",
        "prior_human_disposition_id",
    ),
    ScreeningRun: (
        "suppressed_observation_run_id",
        "hash_edit_run_ids",
        "control_import_run_ids",
        "auto_clear_run_id",
        "likely_run_id",
        "policy_full_run_id",
        "repeat_run_id",
    ),
    OperationAttempt: ("monitor_operation_id", "operation_ids"),
    NotificationBatch: ("morning_report_batch_id",),
    RetentionHold: ("retention_hold_id",),
}


class RestartFailure(RuntimeError):
    pass


def require(value, check):
    if not value:
        raise RestartFailure(check)


def read_checkpoint(path):
    checkpoint = json.loads(path.read_text())
    require(
        isinstance(checkpoint, dict)
        and checkpoint.get("fixture_only") is True
        and checkpoint.get("passed") is True,
        "passed_fixture_checkpoint_required",
    )
    require(
        set(checkpoint.get("cron_next_runs", {})) == CRON_IDS
        and CRON_IDS <= set(checkpoint.get("schedule_ids", [])),
        "complete_saved_cron_checkpoint",
    )
    return checkpoint


def reference_ids(checkpoint, keys):
    ids = set()
    for key in keys:
        value = checkpoint.get(key)
        if isinstance(value, str):
            ids.add(value)
        elif isinstance(value, list):
            require(all(isinstance(id, str) for id in value), "saved_reference_ids_valid")
            ids.update(value)
        else:
            # Optional older checkpoints lack the explicit fan-out/anchor fields.
            require(
                key
                in {
                    "human_queue_anchor_hit_id",
                    "human_interaction_disposition_ids",
                    "effective_suppression_disposition_id",
                    "prior_human_disposition_id",
                    "policy_full_run_id",
                },
                "saved_reference_present",
            )
    return ids


def require_schedules(checkpoint, schedules):
    current = {row["id"]: row["next_run_at"] for row in schedules}
    require(set(checkpoint["schedule_ids"]) <= set(current), "all_saved_schedule_ids_persisted")
    require(
        all(current.get(id) == next_run for id, next_run in checkpoint["cron_next_runs"].items()),
        "cron_next_runs_exactly_preserved",
    )
    # Recovery intervals are deliberately awakened by startup; no equality check.
    return len(checkpoint["schedule_ids"])


def immutable_hit(hit):
    return canonical_hash({key: value for key, value in snapshot(hit).items() if key != "status"})


def wait_for_health(http, *, seconds=60, clock=time.monotonic, pause=time.sleep):
    """Docker restart completion precedes API readiness; poll only public health."""
    deadline = clock() + seconds
    while (remaining := deadline - clock()) > 0:
        try:
            response = http.get("/api/v1/health", timeout=min(2, remaining))
            if response.status_code == 200 and response.json() == {"status": "ok"}:
                return
        except (httpx.HTTPError, ValueError):
            pass  # No raw connection errors, response bodies, or credentials.
        remaining = deadline - clock()
        if remaining > 0:
            pause(min(0.25, remaining))
    raise RestartFailure("api_startup_health_timeout")


class RestartGate:
    def __init__(self):
        self.checkpoint = read_checkpoint(DIRECTORY / "checkpoint.json")
        self.config = Config()
        require(self.config.database_url == "sqlite:////data/screening.db", "fixture_database_path")
        self.engine = create_engine("sqlite:///file:/data/screening.db?mode=ro&uri=true")
        self.http = httpx.Client(base_url=BASE, timeout=60, trust_env=False, follow_redirects=False)

    def get(self, path):
        response = self.http.get("/api/v1" + path)
        require(response.status_code == 200, "read_api_" + path)
        return response.json()

    def authenticate(self):
        with Session(self.engine) as db:
            users = list(db.scalars(select(User)))
            require(
                len(users) == 1
                and users[0].email == ADMIN_EMAIL
                and users[0].id == self.checkpoint["admin_id"]
                and users[0].active
                and users[0].role == "admin",
                "original_synthetic_admin_only",
            )
            require(
                all(
                    source.kind == "csv" and source.credentials_enc is None
                    for source in db.scalars(select(Source))
                ),
                "fixture_csv_sources_only",
            )
        response = self.http.post(
            "/api/v1/auth/login",
            headers={"Origin": BASE},
            json={"email": ADMIN_EMAIL, "password": FIXTURE_PASSWORD},
        )
        require(response.status_code == 200, "dummy_admin_login")
        cookie = response.cookies.get(COOKIE_NAME)
        require(
            cookie and "Secure" in response.headers.get("set-cookie", ""),
            "secure_cookie_fixture_transport",
        )
        self.http.headers.update(
            {
                "Cookie": f"{COOKIE_NAME}={cookie}",
                "X-CSRF-Token": response.json()["csrf_token"],
                "Origin": BASE,
            }
        )

    def verify_ids(self, db):
        counts = {}
        for model, keys in REFERENCES.items():
            ids = reference_ids(self.checkpoint, keys)
            present = set(db.scalars(select(model.id).where(model.id.in_(ids))))
            require(ids == present, "saved_" + model.__tablename__ + "_ids_exist")
            counts[model.__tablename__] = len(ids)
        return counts

    def verify_original_evidence(self, db):
        cp = self.checkpoint
        selected = db.get(Disposition, cp["human_disposition_id"])
        require(
            selected
            and selected.actor_user_id == cp["admin_id"]
            and selected.kind == "false_positive",
            "original_human_decision_persisted",
        )
        anchor = db.get(Hit, selected.hit_id)
        require(
            anchor and anchor.party_hash == cp["original_party_hash"],
            "original_human_hash_unchanged",
        )
        current = db.get(Party, anchor.party_id)
        require(
            current
            and current.record_hash == cp["changed_party_hash"]
            and current.record_hash == party_record_hash(snapshot(current)),
            "changed_party_hash_unchanged",
        )
        events = {
            event.target_id: snapshot(event)
            for event in db.scalars(select(AuditLog).where(AuditLog.action == "review.disposition"))
            if (event.after or {}).get("queue_hit_id") == anchor.id
        }
        require(selected.id in events, "original_clicked_anchor_audit_persisted")
        if cp.get("human_interaction_audit_sha256"):
            require(
                canonical_hash(events) == cp["human_interaction_audit_sha256"],
                "original_human_audit_fingerprint_unchanged",
            )
        humans = {id: db.get(Disposition, id) for id in events}
        require(
            all(row is not None for row in humans.values()), "all_original_fanout_decisions_exist"
        )
        require(
            canonical_hash({id: canonical_hash(snapshot(row)) for id, row in humans.items()})
            == cp["original_human_fingerprints_sha256"],
            "original_human_fingerprint_unchanged",
        )
        # There was no comparison between preflight and the first human action.
        # Its immutable time therefore identifies the exact original seed hits.
        first_action = min(row.created_at for row in humans.values())
        original = {
            hit.id: immutable_hit(hit)
            for hit in db.scalars(select(Hit).where(Hit.created_at < first_action))
        }
        require(
            canonical_hash(original) == cp["original_hit_fingerprints_sha256"],
            "original_hit_fingerprint_unchanged",
        )
        runs = {run.id: run for run in db.scalars(select(ScreeningRun))}
        require(
            len(runs) == cp["runs"] and all(run.finished_at for run in runs.values()),
            "completed_run_count_unchanged",
        )
        evidence_count = zero_hits = 0
        original_participation = {}
        zero_sample = None
        for row in db.scalars(select(RunPartyEvidence).execution_options(yield_per=200)):
            run = runs.get(row.run_id)
            require(run is not None, "participation_actual_run")
            scope = run.settings_snapshot["scope"]
            require(
                row.party_id in scope["party_ids"]
                and scope["party_hashes"].get(row.party_id) == row.party_hash
                and row.party_snapshot["id"] == row.party_id
                and party_record_hash(row.party_snapshot) == row.party_hash,
                "participation_original_snapshot_hash_and_scope",
            )
            require(
                set(scope["entry_ids"]) == set(scope["entry_hashes"]),
                "actual_shared_entry_hash_manifest",
            )
            evidence_count += 1
            if row.created_at < first_action:
                original_participation[row.id] = canonical_hash(snapshot(row))
            if row.hits_recorded == 0:
                zero_hits += 1
                if zero_sample is None:
                    zero_sample = snapshot(row)
        require(
            evidence_count == sum(run.parties_screened for run in runs.values()) and zero_hits > 0,
            "every_participant_including_zero_hits_has_evidence",
        )
        if cp.get("original_evidence_fingerprints_sha256"):
            require(
                canonical_hash(original_participation)
                == cp["original_evidence_fingerprints_sha256"],
                "original_participation_fingerprint_unchanged",
            )
        archive_path = DIRECTORY / "full-audit.zip"
        with archive_path.open("rb") as handle:
            archive_hash = hashlib.file_digest(handle, "sha256").hexdigest()
        require(
            archive_hash == cp["full_zip_sha256"],
            "saved_audit_zip_checksum_unchanged",
        )
        with zipfile.ZipFile(archive_path) as archive:
            for run in runs.values():
                recorded_run = json.loads(archive.read(f"runs/{run.id}.json"))
                original_run = snapshot(run)
                require(
                    {key: recorded_run[key] for key in original_run} == original_run,
                    "finished_run_and_full_manifests_match_prerestart_export",
                )
            document = json.loads(archive.read(f"parties/{zero_sample['party_id']}.json"))
            event = next(
                (
                    event
                    for event in document["events"]
                    if event["run"]["id"] == zero_sample["run_id"]
                ),
                None,
            )
            require(
                event
                and event["zero_hits"]
                and event["participation"]["origin"] == "run_party_evidence"
                and {key: event["participation"][key] for key in zero_sample} == zero_sample,
                "zero_hit_snapshot_matches_prerestart_export",
            )
        return {
            "original_hits": len(original),
            "original_human_decisions": len(humans),
            "participation_rows": evidence_count,
            "zero_hit_participations": zero_hits,
        }

    def verify_mail(self, db):
        cp = self.checkpoint
        report = db.get(NotificationBatch, cp["morning_report_batch_id"])
        require(
            report
            and report.status == "sent"
            and report.window_end.isoformat() == cp["morning_window_end"],
            "morning_sent_window_unchanged",
        )
        require(
            canonical_hash(decrypt_json(report.payload_enc, self.config))
            == cp["frozen_report_payload_sha256"],
            "morning_frozen_payload_unchanged",
        )
        deliveries = list(db.scalars(select(NotificationDelivery)))
        require(
            len(deliveries) == cp["captured_messages"]
            and all(row.status == "sent" for row in deliveries),
            "recipient_delivery_states_not_reset",
        )
        # One morning recipient failed once before the frozen retry; all other
        # recorded recipients were delivered exactly once by the original gate.
        require(
            sum(row.attempts for row in deliveries) == len(deliveries) + 1,
            "sent_recipients_not_retried_or_resent",
        )
        ids = {row.message_id for row in deliveries}
        captured_ids = []
        files = list((DIRECTORY / "private-mime").glob("*.eml"))
        for path in files:
            with path.open("rb") as handle:
                captured_ids.append(
                    BytesParser(policy=policy.default).parse(handle, headersonly=True)["Message-ID"]
                )
        require(
            len(files) == cp["captured_messages"]
            and len(set(captured_ids)) == len(captured_ids)
            and set(captured_ids) == ids,
            "original_capture_ids_and_counts_no_new_send",
        )
        return len(deliveries)

    def run(self):
        try:
            wait_for_health(self.http)
            self.authenticate()
            monitoring = self.get("/monitoring/status")
            require(monitoring["enabled"], "scheduler_enabled_after_restart")
            schedule_count = require_schedules(self.checkpoint, monitoring["schedules"])
            require(
                self.get("/reports/morning/status")["last_report_at"]
                == self.checkpoint["morning_window_end"],
                "successful_report_boundary_unchanged",
            )
            with Session(self.engine) as db:
                ids = self.verify_ids(db)
                evidence = self.verify_original_evidence(db)
                deliveries = self.verify_mail(db)
                require(
                    db.scalar(select(func.count()).select_from(Party)) == 2062
                    and db.scalar(
                        select(func.count())
                        .select_from(CSLEntry)
                        .where(CSLEntry.removed_version.is_(None))
                    )
                    == 300,
                    "fixture_party_and_active_list_counts",
                )
            print(
                json.dumps(
                    {
                        "fixture_only": True,
                        "passed": True,
                        "restart_verified": True,
                        "saved_ids_verified": sum(ids.values()),
                        "persistent_schedules": schedule_count,
                        "cron_next_runs_preserved": 3,
                        "sent_deliveries_unchanged": deliveries,
                        "report_window_unchanged": True,
                        "original_fingerprints_unchanged": True,
                        **evidence,
                    },
                    sort_keys=True,
                )
            )
        finally:
            self.http.close()
            self.engine.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-fixture-gate", action="store_true")
    if not parser.parse_args(argv).run_fixture_gate:
        parser.error("The explicit --run-fixture-gate flag is required")
    try:
        RestartGate().run()
    except Exception as exc:
        # Do not print a traceback, raw transport/config exception, token or body.
        print(
            json.dumps(
                {
                    "fixture_only": True,
                    "passed": False,
                    "failed_check": str(exc)
                    if isinstance(exc, RestartFailure)
                    else "unexpected_restart_fixture_error",
                }
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
