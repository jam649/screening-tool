#!/usr/bin/env python3
"""Fixture-only M8 API/CLI/restart gate; synthetic responses are never live proof.

Run on a NEW isolated Compose volume. The actual app, scheduler, engine, review
and export code run unchanged. An in-memory fixed System One transport supplies
explicitly synthetic probabilities; the real CLI can reach the same dummy wire
over loopback TCP. --actual-laya-base-url separately opts into a known same-host
Laya process. No hosted Jev request leaves this fixture server. Keep all failed
volumes/checkpoints; this script never resets data or resumes a failed run.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Literal

import httpx
from milestone6 import Gate, GateFailure, fingerprint, fixture_csl_data, require
from milestone7 import wait_for_health
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text

from app.adjudicators.budget import local_state_room, local_tokens
from app.adjudicators.contracts import (
    JEV_ENDPOINT,
    JEV_MODEL,
    LAYA_TOKENIZER_SHA256,
    PRIVACY_NOTICE,
)
from app.adjudicators.settings import validate_laya_base_url
from app.api.deps import AdminUser
from app.config import Config
from app.csl.service import refresh_csl
from app.main import create_app
from app.models import CSLVersion
from app.security import COOKIE_NAME

ADMIN_EMAIL = "model-fixture-admin@example.test"
PASSWORD = "Synthetic model gate password2026!"
PRIVATE_KEY = "fixture-jev-write-only-key-v8"
CONTROL = "M8 Synthetic Zqxv Identity Control 42"
CONTROL_ID = "m8-fixture-control-42"
CHECKPOINT = Path("/data/milestone8-backend-checkpoint.json")
ARTIFACTS = Path("/data/milestone8-gate")
FIXTURE_URL = "http://127.0.0.1:8000"
HEADERS = ["external_id", "name", "party_type", "role", "country", "city", "address_lines"]
PHASES = Literal["valid", "invalid_response", "actual_laya"]


class PhaseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phase: PHASES


def fixture_rows():
    rows = json.loads(fixture_csl_data())
    # Reuse the 300 unchanged public fixture entries, replace the earlier
    # milestone's synthetic extra with this explicitly labeled model control.
    rows = [row for row in rows if row["id"] != "qbo-fixture-control-42"]
    rows.append(
        {
            "id": CONTROL_ID,
            "source": "Entity List (EL) - Bureau of Industry and Security",
            "name": CONTROL,
            "type": "Entity",
            "programs": ["Synthetic model control"],
            "alt_names": [
                f"Synthetic alternative {i} remote irrelevant identity descriptor"
                for i in range(200)
            ],
            "remarks": "Synthetic model fixture. " + "long remarks evidence " * 1000,
            "addresses": [
                {
                    "country": "US",
                    "city": "Boston",
                    "address": "1 Main Street " + "long street identity evidence " * 250,
                }
            ],
        }
    )
    return rows


def csv_book(changed=False):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(HEADERS)
    address = "1 Main Street " + "long street identity evidence " * 200
    writer.writerow(
        [
            "fp",
            CONTROL + (" LLC" if changed else ""),
            "individual" if changed else "company",
            "customer",
            "US",
            "Boston",
            address,
        ]
    )
    writer.writerow(["confirmed", CONTROL, "company", "vendor", "US", "Boston", address])
    return output.getvalue().encode()


@contextmanager
def readonly_database(config):
    require(config.database_url == "sqlite:////data/milestone8-fixture.db", "fixture_database_only")
    with sqlite3.connect("file:/data/milestone8-fixture.db?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        yield db


def verify_training(body, *, actor_id, originals):
    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
    manifests = [row for row in rows if row["row_type"] == "manifest"]
    pairs = [row for row in rows if row["row_type"] == "training_pair"]
    require(
        len(manifests) == 1 and int(manifests[0]["exported_pairs"]) == len(pairs),
        "training_manifest_count",
    )
    require(
        int(manifests[0]["unavailable_purged_pairs"]) == 0
        and int(manifests[0]["unavailable_actor_provenance"]) == 0,
        "training_retained_provenance_complete",
    )
    require(
        {row["label"] for row in pairs} == {"false_positive", "confirmed_match"},
        "human_only_training_labels",
    )
    groups = {}
    for row in pairs:
        actor = json.loads(row["actor_snapshot"])
        original = originals[row["party_id"]]
        require(
            actor["id"] == actor_id and actor["email"] == ADMIN_EMAIL,
            "training_original_human_actor",
        )
        require(
            row["party_hash"] == original["party_hash"]
            and row["entry_hash"] == original["entry_hash"]
            and json.loads(row["party_snapshot"])["name"] == CONTROL
            and json.loads(row["party_snapshot"])["party_type"] == "company",
            "training_original_snapshots_and_hashes",
        )
        require(row["queue_hit_id"] == original["id"], "training_actual_fanout_group")
        require(
            int(row["label_value"]) == int(row["label"] == "confirmed_match"),
            "training_label_value",
        )
        groups.setdefault(row["party_id"], []).append(row)
    require(all(len(groups.get(id, [])) >= 2 for id in originals), "training_real_fanout_labels")
    return len(pairs)


class ModelFixture:
    def __init__(self):
        self.app, self.phase = None, "valid"
        self.lock = threading.Lock()
        self.calls = self.writer_checks = 0

    def response(self, payload, *, hosted=False):
        with self.app.state.session_factory() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            db.rollback()
        with self.lock:
            self.writer_checks += 1
            self.calls += 1
            invalid = self.phase == "invalid_response"
        require(
            payload.get("questions", {}).get("same_party", {}).get("type") == "noul",
            "fixed_noul_wire",
        )
        require(
            payload.get("model") == (JEV_MODEL if hosted else "multilingual"),
            "fixed_requested_model",
        )
        count = (
            len(json.dumps(payload["state"]).encode()) // 4
            if hosted
            else local_tokens(payload["state"]) + 1024 - local_state_room()
        )
        require(hosted or count <= 1024, "actual_local_tokenizer_sequence_bound")
        return {
            "model": JEV_MODEL if hosted else "multilingual",
            "answers": {"same_party": {"type": "noul", "noul": True if invalid else 0.0}},
            "usage": {"input_tokens": count, "output_tokens": 0},
        }

    def request(self, request):
        require(
            str(request.url) in {JEV_ENDPOINT, FIXTURE_URL + "/v1/systemone"},
            "fixed_synthetic_provider_origin",
        )
        return httpx.Response(
            200,
            json=self.response(
                json.loads(request.content), hosted=str(request.url) == JEV_ENDPOINT
            ),
        )


def fixture_app(actual_laya_base_url=None):
    config = Config()
    require(config.database_url == "sqlite:////data/milestone8-fixture.db", "fixture_database_only")
    require(
        set(config.allowed_hosts) <= {"localhost", "127.0.0.1", "::1"}, "fixture_local_hosts_only"
    )
    data = json.dumps(fixture_rows()).encode()
    from app.csl import service as csl_service

    csl_service.download_bulk = lambda: data
    csl_service.download_stamps = lambda api_key: {}
    app = create_app(config)
    if actual_laya_base_url:
        validate_laya_base_url(actual_laya_base_url)
    remote = ModelFixture()
    remote.app = app
    transport = httpx.MockTransport(remote.request)
    remote.phase = "actual_laya" if actual_laya_base_url else "valid"
    app.state.model_transport = None if actual_laya_base_url else transport
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application):
            with application.state.session_factory() as db:
                if db.scalar(select(CSLVersion.id)) is None:
                    refresh_csl(db, data=data)
            yield

    app.router.lifespan_context = lifespan

    @app.post("/_fixture/model/phase")
    def phase(body: PhaseBody, user: AdminUser):
        require(
            not actual_laya_base_url or body.phase == "actual_laya",
            "actual_server_never_installs_synthetic_transport",
        )
        with remote.lock:
            remote.phase = body.phase
            app.state.model_transport = None if body.phase == "actual_laya" else transport
        return {
            "phase": body.phase,
            "validation": "actual-local-laya-data-fixture"
            if actual_laya_base_url
            else "synthetic-model-contract",
        }

    @app.get("/_fixture/model/status")
    def status(user: AdminUser):
        with remote.lock:
            return {
                "phase": remote.phase,
                "calls": remote.calls,
                "writer_checks": remote.writer_checks,
                "validation": "actual-local-laya-data-fixture"
                if actual_laya_base_url
                else "synthetic-model-contract",
                "synthetic_transport_installed": app.state.model_transport is not None,
            }

    def loopback_fixture_wire(payload: dict):
        # Real CLI loopback TCP; fixed dummy contract, never a live Laya claim.
        return remote.response(payload)

    if not actual_laya_base_url:
        app.post("/v1/systemone")(loopback_fixture_wire)

    fallback = next(
        row for row in app.router.routes if getattr(row, "path", None) == "/{path:path}"
    )
    app.router.routes.remove(fallback)
    app.router.routes.append(fallback)
    return app


class ModelGate(Gate):
    def __init__(self, base):
        super().__init__(base)
        self.http.headers["origin"] = base.rstrip("/")
        self.operation_ids, self.run_ids = set(), set()
        self.evidence = {
            "milestone": 8,
            "validation": "synthetic-model-contract-real-application",
            "live_jev": "not_run",
            "actual_laya": "not_requested",
            "checks": [],
        }

    def authenticate(self, create=False):
        session = self.call(
            "POST",
            "/api/v1/setup" if create else "/api/v1/auth/login",
            {
                "email": ADMIN_EMAIL,
                "password": PASSWORD,
                **(
                    {"display_name": "Fixture Model Admin", "disclaimer_acknowledged": True}
                    if create
                    else {}
                ),
            },
            201 if create else 200,
        ).json()
        self.admin_id, self.csrf = session["user"]["id"], session["csrf_token"]
        token = self.http.cookies.get(COOKIE_NAME)
        require(token, "secure_fixture_cookie")
        self.http.headers["cookie"] = f"{COOKIE_NAME}={token}"

    def model(self):
        return self.call("GET", "/api/v1/settings/model").json()

    def phase(self, phase):
        self.call("POST", "/_fixture/model/phase", {"phase": phase})

    def wait_task(self, task_id):
        run = super().wait_screening({"pending_task_id": task_id}) if task_id else None
        if run:
            self.run_ids.add(run["id"])
        self.wait_screening_idle()
        return run

    def wait_screening_idle(self):
        # Task evidence commits just before the enclosing protected operation
        # records success and releases its lease. Wait on that real boundary.
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            with readonly_database(Config()) as db:
                lease = db.execute(
                    "SELECT owner_id FROM operation_leases WHERE key='screening'"
                ).fetchone()
            if lease is None or lease["owner_id"] is None:
                return
            time.sleep(0.1)
        raise GateFailure("screening_operation_release_timeout")

    def policy(self, **changes):
        result = self.call(
            "PATCH",
            "/api/v1/settings/model",
            {"expected_generation": self.model()["generation"], **changes},
        ).json()
        self.wait_task(result["pending_screening_task_id"])
        return result

    def screen(self, party_id=None):
        self.wait_screening_idle()
        result = self.call(
            "POST", "/api/v1/screen", {"party_id": party_id} if party_id else {"all": True}
        ).json()
        self.run_ids.add(result["id"])
        return result

    def book(self):
        return {
            row["external_id"]: row
            for row in self.call("GET", f"/api/v1/parties?source_id={self.source_id}").json()[
                "items"
            ]
        }

    def upload(self, changed=False):
        response = self.http.post(
            f"/api/v1/sources/{self.source_id}/upload",
            headers={"x-csrf-token": self.csrf},
            files={"file": ("synthetic-model.csv", csv_book(changed), "text/csv")},
            data={"mapping": json.dumps({name: name for name in HEADERS})},
        )
        require(response.status_code == 200, "actual_complete_csv_upload")
        result = response.json()
        require(
            result["applied"] and not result.get("screening_pending"),
            "complete_import_and_screening",
        )
        self.run_ids.update(result["screening_run_ids"])
        return result

    def control_hits(self, run_id):
        with readonly_database(Config()) as db:
            ids = [
                row["id"]
                for row in db.execute(
                    "SELECT h.id FROM hits h JOIN csl_entries e ON e.id=h.csl_entry_id "
                    "WHERE h.run_id=? AND e.csl_id=?",
                    (run_id, CONTROL_ID),
                )
            ]
        return [self.call("GET", "/api/v1/hits/" + id).json() for id in ids]

    def cli(self, arguments):
        self.wait_screening_idle()
        output = subprocess.run(
            [sys.executable, "-m", "app.cli", *arguments], capture_output=True, timeout=90
        )
        require(output.returncode == 0, "actual_cli_operation_success")
        try:
            return json.loads(output.stdout)
        except (ValueError, UnicodeError):
            raise GateFailure("actual_cli_safe_json") from None

    def unchanged(self, changed):
        before = self.book()
        result = self.upload(changed)
        after = self.book()
        fields = ("id", "name", "party_type", "record_hash", "raw", "updated_at", "last_synced_at")
        require(
            result["unchanged"] == 2 and result["screening_run_ids"] == [], "unchanged_csv_no_task"
        )
        require(
            {id: {k: row.get(k) for k in fields} for id, row in before.items()}
            == {id: {k: row.get(k) for k in fields} for id, row in after.items()},
            "unchanged_csv_no_party_writes",
        )

    def training(self, originals):
        ARTIFACTS.mkdir(exist_ok=True, mode=0o700)
        path = ARTIFACTS / "human-training-pairs.csv"
        require(not path.exists(), "training_artifact_not_overwritten")
        result = self.cli(["export", "training-pairs", "--output", str(path)])
        body = path.read_bytes()
        require(
            hashlib.sha256(body).hexdigest() == result["sha256"] and len(body) == result["size"],
            "training_real_cli_checksum",
        )
        count = verify_training(body, actor_id=self.admin_id, originals=originals)
        require(PRIVATE_KEY.encode() not in body, "training_no_private_key")
        self.evidence["training_pairs"] = count
        self.check("actual_cli_human_training_original_fanout_checksum_manifest")

    def zip_privacy(self):
        response = self.call("GET", "/api/v1/exports/full.zip")
        require(
            hashlib.sha256(response.content).hexdigest() == response.headers["x-export-sha256"],
            "zip_real_checksum",
        )
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            for path, checksum in manifest["files"].items():
                require(
                    hashlib.sha256(archive.read(path)).hexdigest() == checksum["sha256"]
                    and len(archive.read(path)) == checksum["bytes"],
                    "zip_file_manifest_checksum",
                )
            for path in archive.namelist():
                require(PRIVATE_KEY.encode() not in archive.read(path), "zip_no_private_model_key")
                if path.endswith(".json"):
                    value = json.loads(archive.read(path))
                    verify_private_fields(value)
                elif path.endswith(".jsonl"):
                    for line in archive.read(path).splitlines():
                        if line:
                            verify_private_fields(json.loads(line))
        self.check("real_zip_checksums_encrypted_keys_and_public_measurements")

    def run(self):
        require(not CHECKPOINT.exists(), "fresh_checkpoint_only")
        with readonly_database(Config()) as db:
            require(
                db.execute("SELECT count(*) FROM sources").fetchone()[0] == 0
                and db.execute("SELECT count(*) FROM users").fetchone()[0] == 0,
                "fresh_fixture_installation",
            )
            require(
                db.execute(
                    "SELECT count(*) FROM csl_entries WHERE csl_id=?", (CONTROL_ID,)
                ).fetchone()[0]
                == 1,
                "synthetic_control_seeded",
            )
        self.authenticate(create=True)
        require(
            self.model()["provider"] == "none"
            and self.model()["w_det"] == 1
            and self.model()["w_model"] == 0,
            "default_deterministic_model_policy",
        )
        self.source_id = self.call(
            "POST", "/api/v1/sources", {"kind": "csv", "label": "Synthetic model gate book"}, 201
        ).json()["id"]
        self.upload()
        book = self.book()
        require(len(book) == 2, "actual_two_party_csv_book")
        self.fp_id = book["fp"]["id"]
        repeat = self.screen()
        require(repeat["hits_new"] == 0, "repeat_no_new_open_queue")
        originals = {}
        with readonly_database(Config()) as db:
            entry_id = db.execute(
                "SELECT id FROM csl_entries WHERE csl_id=?", (CONTROL_ID,)
            ).fetchone()[0]
        queue = self.call("GET", "/api/v1/queue").json()["items"]
        for external_id, kind in (("fp", "false_positive"), ("confirmed", "confirmed_match")):
            party_id = book[external_id]["id"]
            hit = next(
                row
                for row in queue
                if row["party_id"] == party_id and row["csl_entry_id"] == entry_id
            )
            detail = self.call("GET", "/api/v1/hits/" + hit["id"]).json()
            decision = self.call(
                "POST",
                f"/api/v1/hits/{hit['id']}/dispositions",
                {"kind": kind, "note": "Recorded synthetic human " + kind},
                201,
            ).json()
            require(decision["observations_dispositioned"] >= 2, "real_human_fanout")
            originals[party_id] = {
                "id": hit["id"],
                "fingerprint": fingerprint(detail),
                "party_hash": detail["party_hash"],
                "entry_hash": detail["entry_hash"],
                "decision_id": decision["disposition"]["id"],
            }
        learned = self.screen()
        require(
            learned["hits_new"] == 0 and learned["hits_suppressed"] >= 1,
            "human_memory_identical_observation",
        )
        self.unchanged(False)
        self.upload(changed=True)
        require(
            self.book()["fp"]["id"] == self.fp_id
            and self.book()["fp"]["record_hash"] != originals[self.fp_id]["party_hash"],
            "type_name_edit_same_identity_new_hash",
        )
        changed = [
            row
            for row in self.call("GET", "/api/v1/queue").json()["items"]
            if row["party_id"] == self.fp_id and row["csl_entry_id"] == entry_id
        ]
        require(len(changed) == 1, "changed_pair_reopens")
        detail = self.call("GET", "/api/v1/hits/" + changed[0]["id"]).json()
        require(
            detail["prior_disposition"]["actor"]["id"] == self.admin_id
            and detail["prior_disposition"]["kind"] == "false_positive"
            and detail["explanation"]["what_changed"]["party"]["party_type"]["before"] == "company",
            "changed_pair_original_human_type_diff",
        )
        self.check("actual_csv_human_fanout_memory_type_hash_reopening")

        generation = self.model()["generation"]
        self.call(
            "PATCH",
            "/api/v1/settings/model",
            {"expected_generation": generation, "provider": "jev", "jev_api_key": PRIVATE_KEY},
            422,
        )
        hosted = self.policy(
            provider="jev", jev_api_key=PRIVATE_KEY, hosted_acknowledgment=PRIVACY_NOTICE
        )
        require(
            hosted["hosted_acknowledgment_valid"]
            and hosted["jev_api_key_configured"]
            and PRIVATE_KEY not in json.dumps(hosted),
            "hosted_explicit_ack_key_write_only",
        )
        self.call(
            "PATCH",
            "/api/v1/settings/model",
            {"expected_generation": generation, "provider": "none"},
            409,
        )
        audit = self.call(
            "GET", "/api/v1/audit-log?action=settings.model_hosted_acknowledged"
        ).json()["items"]
        require(
            len(audit) == 1
            and audit[0]["after"]["acknowledgment"] == PRIVACY_NOTICE
            and audit[0]["actor_user_id"] == self.admin_id,
            "hosted_ack_actual_actor_audit",
        )
        local = self.policy(provider="laya", laya_base_url=FIXTURE_URL, w_det=0, w_model=1)
        tested = self.call(
            "POST", "/api/v1/settings/model/test", {"expected_generation": local["generation"]}
        ).json()
        require(
            tested["status"] == "connected"
            and tested["model_prob"] == 0
            and tested["returned_model"] == "multilingual",
            "saved_model_real_test_route_zero_probability",
        )
        rail = self.screen(self.fp_id)
        hit = self.control_hits(rail["id"])[0]
        metadata = hit["explanation"]["adjudication"]
        require(
            hit["det_score"] >= rail["settings_snapshot"]["likely_threshold"]
            and hit["model_prob"] == 0
            and hit["final_confidence"] == 0
            and hit["bucket"] == "likely",
            "engine_adversarial_probability_likely_rail",
        )
        budget = metadata["budget"]
        require(
            budget["removed_fields"] == ["remarks", "address_lines", "extra_aliases"]
            and budget["exact"]
            and budget["tokenizer_sha256"] == LAYA_TOKENIZER_SHA256
            and metadata["input_tokens"] == budget["sequence_tokens"] <= 1024,
            "actual_tokenizer_ordered_trimming_usage_pin",
        )
        before_calls = self.call("GET", "/_fixture/model/status").json()["calls"]
        self.screen(book["confirmed"]["id"])
        require(
            self.call("GET", "/_fixture/model/status").json()["calls"] == before_calls,
            "learned_human_bypasses_new_model_call",
        )
        cli_runs = self.cli(["screen", "--party", self.fp_id])
        require(len(cli_runs) == 1, "actual_cli_screen_run")
        self.run_ids.add(cli_runs[0]["id"])
        require(
            self.control_hits(cli_runs[0]["id"])[0]["model_prob"] == 0,
            "actual_cli_fixture_tcp_probability",
        )
        self.check("native_model_settings_ack_test_engine_rail_usage_and_cli")

        self.phase("invalid_response")
        failed = self.screen(self.fp_id)
        fallback = self.control_hits(failed["id"])[0]
        require(
            fallback["model_prob"] is None
            and fallback["final_confidence"] == fallback["det_score"]
            and failed["model_fallbacks"],
            "invalid_response_deterministic_fallback",
        )
        proof = self.call("GET", "/api/v1/audit-log?action=screening.model_fallback").json()[
            "items"
        ]
        require(
            any(row["target_id"] == failed["id"] for row in proof),
            "fallback_actual_immutable_audit",
        )
        self.phase("valid")
        self.unchanged(True)
        self.phase("valid")
        self.policy(provider="none", w_det=1, w_model=0)
        self.training(originals)
        self.zip_privacy()
        for original in originals.values():
            require(
                fingerprint(self.call("GET", "/api/v1/hits/" + original["id"]).json())
                == original["fingerprint"],
                "original_hit_immutable_after_all_models",
            )
        schedules = self.call("GET", "/api/v1/monitoring/status").json()["schedules"]
        with readonly_database(Config()) as db:
            audits = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM audit_log WHERE action='review.disposition' ORDER BY id"
                )
            ]
            operations = [row["id"] for row in db.execute("SELECT id FROM operation_attempts")]
            runs = [row["id"] for row in db.execute("SELECT id FROM screening_runs")]
        saved = {
            "passed": True,
            "source_id": self.source_id,
            "party_ids": sorted(row["id"] for row in self.book().values()),
            "originals": originals,
            "human_audit_fingerprint": canonical_fingerprint(audits),
            "operation_ids": operations,
            "run_ids": runs,
            "schedule_ids": sorted(row["id"] for row in schedules),
            "model_generation": self.model()["generation"],
            "training_sha256": hashlib.sha256(
                (ARTIFACTS / "human-training-pairs.csv").read_bytes()
            ).hexdigest(),
            "actual_laya": self.evidence["actual_laya"],
        }
        with CHECKPOINT.open("x") as output:
            json.dump(saved, output, sort_keys=True)
        CHECKPOINT.chmod(0o600)
        self.evidence.update(
            passed=True, party_count=2, run_count=len(runs), operation_count=len(operations)
        )
        return self.evidence

    def restart(self):
        require(CHECKPOINT.exists(), "completed_checkpoint_required")
        saved = json.loads(CHECKPOINT.read_text())
        require(saved["passed"], "passed_checkpoint_required")
        self.authenticate()
        self.source_id = saved["source_id"]
        require(
            self.model()["provider"] == "none"
            and self.model()["generation"] == saved["model_generation"],
            "restart_encrypted_model_policy_and_generation",
        )
        require(
            sorted(row["id"] for row in self.book().values()) == saved["party_ids"],
            "restart_party_identities",
        )
        for original in saved["originals"].values():
            require(
                fingerprint(self.call("GET", "/api/v1/hits/" + original["id"]).json())
                == original["fingerprint"],
                "restart_original_hit_immutable",
            )
        with readonly_database(Config()) as db:
            for table, ids in (
                ("operation_attempts", saved["operation_ids"]),
                ("screening_runs", saved["run_ids"]),
                ("dispositions", [row["decision_id"] for row in saved["originals"].values()]),
            ):
                for id in ids:
                    require(
                        db.execute(f"SELECT 1 FROM {table} WHERE id=?", (id,)).fetchone(),
                        "restart_durable_evidence_exists",
                    )
            audits = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM audit_log WHERE action='review.disposition' ORDER BY id"
                )
            ]
        require(
            canonical_fingerprint(audits) == saved["human_audit_fingerprint"],
            "restart_human_audit_immutable",
        )
        require(
            sorted(
                row["id"]
                for row in self.call("GET", "/api/v1/monitoring/status").json()["schedules"]
            )
            == saved["schedule_ids"],
            "restart_persistent_schedule_identities",
        )
        if saved.get("training_sha256"):
            require(
                hashlib.sha256((ARTIFACTS / "human-training-pairs.csv").read_bytes()).hexdigest()
                == saved["training_sha256"],
                "restart_training_artifact_immutable",
            )
        for id, recorded in saved.get("actual_hit_fingerprints", {}).items():
            require(
                fingerprint(self.call("GET", "/api/v1/hits/" + id).json()) == recorded,
                "restart_actual_model_hit_immutable",
            )
        return {
            "milestone": 8,
            "passed": True,
            "restart": True,
            "party_count": len(saved["party_ids"]),
            "run_count": len(saved["run_ids"]),
            "actual_laya": saved["actual_laya"],
        }

    def run_actual(self, endpoint):
        endpoint = validate_laya_base_url(endpoint)
        require(not CHECKPOINT.exists(), "fresh_checkpoint_only")
        with readonly_database(Config()) as db:
            require(
                db.execute("SELECT count(*) FROM sources").fetchone()[0] == 0
                and db.execute("SELECT count(*) FROM users").fetchone()[0] == 0,
                "fresh_actual_laya_installation",
            )
        self.authenticate(create=True)
        status = self.call("GET", "/_fixture/model/status").json()
        require(
            status["phase"] == "actual_laya"
            and not status["synthetic_transport_installed"]
            and status["calls"] == 0,
            "actual_server_no_synthetic_transport",
        )
        self.source_id = self.call(
            "POST",
            "/api/v1/sources",
            {"kind": "csv", "label": "Actual local Laya synthetic book"},
            201,
        ).json()["id"]
        self.upload()
        self.fp_id = self.book()["fp"]["id"]
        selected = self.policy(provider="laya", laya_base_url=endpoint, w_det=1, w_model=0)
        tested = self.call(
            "POST", "/api/v1/settings/model/test", {"expected_generation": selected["generation"]}
        ).json()
        require(
            tested["status"] == "connected" and tested["model_prob"] is not None,
            "actual_laya_saved_config_test_probability",
        )
        run = self.screen(self.fp_id)
        hit = self.control_hits(run["id"])[0]
        metadata = hit["explanation"]["adjudication"]
        require(
            hit["model_prob"] is not None
            and hit["final_confidence"] == hit["det_score"]
            and hit["bucket"] == "likely",
            "actual_laya_probability_default_blend_likely_rail",
        )
        require(
            metadata["returned_model"]
            in {"multilingual", "laya-rl-agent", "convaiinnovations/laya"}
            and metadata["input_tokens"] == metadata["budget"]["sequence_tokens"] <= 1024
            and metadata["budget"]["tokenizer_sha256"] == LAYA_TOKENIZER_SHA256,
            "actual_laya_returned_model_usage_tokenizer_pin",
        )
        original = {hit["id"]: fingerprint(hit)}
        self.policy(laya_base_url="http://127.0.0.1:65530")
        unreachable = self.screen(self.fp_id)
        fallback = self.control_hits(unreachable["id"])[0]
        require(
            fallback["model_prob"] is None
            and fallback["final_confidence"] == fallback["det_score"]
            and unreachable["model_fallbacks"],
            "actual_unreachable_laya_deterministic_fallback",
        )
        self.policy(provider="none")
        self.zip_privacy()
        status = self.call("GET", "/_fixture/model/status").json()
        require(
            not status["synthetic_transport_installed"] and status["calls"] == 0,
            "actual_run_never_used_dummy_response",
        )
        schedules = self.call("GET", "/api/v1/monitoring/status").json()["schedules"]
        with readonly_database(Config()) as db:
            runs = [row["id"] for row in db.execute("SELECT id FROM screening_runs")]
            operations = [row["id"] for row in db.execute("SELECT id FROM operation_attempts")]
        saved = {
            "passed": True,
            "source_id": self.source_id,
            "party_ids": sorted(row["id"] for row in self.book().values()),
            "originals": {},
            "actual_hit_fingerprints": original,
            "human_audit_fingerprint": canonical_fingerprint([]),
            "operation_ids": operations,
            "run_ids": runs,
            "schedule_ids": sorted(row["id"] for row in schedules),
            "model_generation": self.model()["generation"],
            "actual_laya": "real_same_host_probability_and_unreachable_fallback",
        }
        with CHECKPOINT.open("x") as output:
            json.dump(saved, output, sort_keys=True)
        CHECKPOINT.chmod(0o600)
        self.evidence.update(
            passed=True,
            validation="actual-local-laya-real-application",
            actual_laya=saved["actual_laya"],
            party_count=2,
            run_count=len(runs),
            synthetic_model_calls=0,
        )
        self.check("actual_native_config_test_inference_usage_and_unreachable_fallback")
        return self.evidence


def canonical_fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def verify_private_fields(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {
                "jev_api_key",
                "laya_api_key",
                "credentials_enc",
                "client_secret",
                "refresh_token",
                "access_token",
            }:
                require(item in (None, "[redacted]"), "export_exact_private_redaction")
            verify_private_fields(item)
    elif isinstance(value, list):
        for item in value:
            verify_private_fields(item)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--serve-fixture", action="store_true")
    mode.add_argument("--run-fixture-gate", action="store_true")
    mode.add_argument("--run-actual-laya-gate", action="store_true")
    mode.add_argument("--verify-restart", action="store_true")
    parser.add_argument("--base-url", default=FIXTURE_URL)
    parser.add_argument("--actual-laya-base-url")
    args = parser.parse_args(argv)
    gate = None
    try:
        require(
            not args.run_actual_laya_gate or args.actual_laya_base_url,
            "actual_laya_endpoint_required",
        )
        require(
            not args.actual_laya_base_url or args.run_actual_laya_gate or args.serve_fixture,
            "actual_laya_separate_explicit_gate",
        )
        if args.serve_fixture:
            import uvicorn

            uvicorn.run(
                fixture_app(args.actual_laya_base_url),
                host="0.0.0.0",
                port=8000,
                access_log=False,
                log_level="critical",
            )
            return
        gate = ModelGate(args.base_url)
        wait_for_health(gate.http)
        result = (
            gate.restart()
            if args.verify_restart
            else gate.run_actual(args.actual_laya_base_url)
            if args.run_actual_laya_gate
            else gate.run()
        )
        print(json.dumps(result, sort_keys=True))
    except Exception as error:  # noqa: BLE001 -- fixed safe diagnostics, never provider bodies
        print(
            json.dumps(
                {
                    "milestone": 8,
                    "failed_check": str(error)
                    if isinstance(error, GateFailure)
                    else "gate_execution_failed",
                }
            )
        )
        raise SystemExit(1) from None
    finally:
        if gate:
            gate.http.close()


if __name__ == "__main__":
    main()
