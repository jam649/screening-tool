#!/usr/bin/env python3
"""Isolated QBO fixture server and real HTTP gate; never authorizes live ERP calls.

The server requires a database named milestone6-fixture.db, uses the packaged
production app/scheduler/connector, and injects MockTransport only in process
memory. Its explicit /_fixture routes exist only in this script. They change
synthetic remote responses, never rewrite business records. Run on a new named
Compose volume. Preserve it and its key for restart verification. No access logs,
raw OAuth responses, token values, environment dumps or customer data are printed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import io
import json
import re
import threading
import time
import zipfile
from collections import Counter
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.api.deps import DB, AdminUser
from app.config import Config
from app.connectors.qbo_common import load_source_credentials
from app.csl.service import refresh_csl
from app.main import create_app
from app.models import CSLVersion, Party, Source
from app.security import COOKIE_NAME

ADMIN_EMAIL = "qbo-fixture-admin@example.test"
PASSWORD = "Synthetic QBO gate password2026!"
APP_ID = "fixture-client"
APP_SECRET = "fixture-secret"
VERIFIER = "fixture-verifier"
PHASES = Literal[
    "initial",
    "changed",
    "inactive",
    "reactivated",
    "malformed",
    "revoke_failed",
    "refresh_failed",
]
CHECKPOINT = Path("/data/milestone6-backend-checkpoint.json")


class PhaseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phase: PHASES


class GateFailure(RuntimeError):
    pass


def require(value, check):
    if not value:
        raise GateFailure(check)


def fingerprint(hit):
    fields = (
        "id",
        "run_id",
        "party_id",
        "csl_entry_id",
        "party_hash",
        "entry_hash",
        "party_snapshot",
        "entry_snapshot",
        "explanation",
        "det_score",
        "model_prob",
        "final_confidence",
        "bucket",
        "created_at",
    )
    body = {key: hit[key] for key in fields}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class RemoteFixture:
    def __init__(self):
        self.phase = "initial"
        self.calls = Counter()
        self.lock = threading.RLock()
        self.app = None
        self.rotation_persisted_before_data = False

    def request(self, request):
        with self.lock:
            require(request.url.scheme == "https", "fixture_fixed_https")
            require(
                request.url.host
                in {
                    "oauth.platform.intuit.com",
                    "developer.api.intuit.com",
                    "sandbox-quickbooks.api.intuit.com",
                },
                "fixture_fixed_vendor_host",
            )
            path = request.url.path
            if path == "/oauth2/v1/tokens/bearer":
                grant = parse_qs(request.content.decode()).get("grant_type", [""])[0]
                require(
                    grant in {"authorization_code", "refresh_token"},
                    "fixture_token_grant",
                )
                self.calls[grant] += 1
                if self.phase == "refresh_failed" and grant == "refresh_token":
                    return httpx.Response(400, json={"error": "invalid_grant"})
                rotated = grant == "refresh_token"
                return httpx.Response(
                    200,
                    json={
                        "token_type": "bearer",
                        "access_token": "synthetic-qbo-gate-access-rotated"
                        if rotated
                        else "synthetic-qbo-gate-access-initial",
                        "refresh_token": "synthetic-qbo-gate-refresh-rotated"
                        if rotated
                        else "synthetic-qbo-gate-refresh-initial",
                        "expires_in": 3600 if rotated else 60,
                        "x_refresh_token_expires_in": 8640000,
                    },
                )
            if path == "/v2/oauth2/tokens/revoke":
                self.calls["revoke"] += 1
                return httpx.Response(503 if self.phase == "revoke_failed" else 200, json={})
            require(
                re.fullmatch(r"/v3/company/12345/(query|cdc)", path),
                "fixture_bound_company",
            )
            endpoint = path.rsplit("/", 1)[1]
            self.calls[endpoint] += 1
            if self.phase == "refresh_failed":
                return httpx.Response(401, json={"Fault": {"type": "AuthenticationFault"}})
            # Prove token rotation was durable before its first authenticated data GET.
            with self.app.state.session_factory() as db:
                source = db.scalar(select(Source).where(Source.kind == "qbo"))
                values = load_source_credentials(source, self.app.state.config)
                require(
                    request.headers.get("authorization") == "Bearer " + values["access_token"],
                    "fixture_token_persisted_before_data",
                )
                self.rotation_persisted_before_data |= values["access_token"].endswith("rotated")
            customer = {
                "Id": "42",
                "Active": self.phase != "inactive",
                "CompanyName": "Example Manufacturing 42 Updated"
                if self.phase == "changed"
                else "Example Manufacturing 42",
                "DisplayName": "Example Manufacturing 42 Updated"
                if self.phase == "changed"
                else "Example Manufacturing 42",
                "BillAddr": {
                    "Line1": "1 Main Street",
                    "City": "Boston",
                    "Country": "US",
                },
                "MetaData": {"LastUpdatedTime": datetime.now(UTC).isoformat()},
            }
            vendor = {
                "Id": "42",
                "Active": True,
                "CompanyName": "Synthetic Fixture Logistics",
                "DisplayName": "Synthetic Fixture Logistics",
                "BillAddr": {"City": "Seattle", "Country": "US"},
                "MetaData": {"LastUpdatedTime": datetime.now(UTC).isoformat()},
            }
            if self.phase == "malformed":
                return httpx.Response(200, json={"QueryResponse": {"Customer": "incomplete"}})
            if endpoint == "query":
                query = request.url.params.get("query", "")
                entity = "Customer" if "FROM Customer " in query else "Vendor"
                require(
                    f"FROM {entity} " in query and "Active IN (true, false)" in query,
                    "fixture_full_includes_inactive",
                )
                start = re.search(r"STARTPOSITION (\d+)", query)
                require(start is not None, "fixture_full_page")
                rows = [customer if entity == "Customer" else vendor] if start[1] == "1" else []
                return httpx.Response(
                    200,
                    json={
                        "QueryResponse": {
                            entity: rows,
                            "startPosition": int(start[1]),
                            "maxResults": len(rows),
                            "totalCount": 1,
                        }
                    },
                )
            require(
                request.url.params.get("entities") == "customer,vendor",
                "fixture_cdc_entities",
            )
            require(bool(request.url.params.get("changedSince")), "fixture_cdc_watermark")
            return httpx.Response(
                200,
                json={
                    "CDCResponse": [
                        {
                            "QueryResponse": [
                                {
                                    "Customer": [customer],
                                    "startPosition": 1,
                                    "maxResults": 1,
                                    "totalCount": 2,
                                },
                                {
                                    "Vendor": [vendor],
                                    "startPosition": 1,
                                    "maxResults": 1,
                                    "totalCount": 2,
                                },
                            ]
                        }
                    ]
                },
            )


def fixture_csl_data():
    fixtures = Path("/app/fixtures")
    if not fixtures.is_dir():
        fixtures = Path(__file__).resolve().parents[2] / "fixtures"
    rows = json.loads((fixtures / "csl_sample_v1.json").read_text())
    # Explicitly synthetic control appended to the checked-in fixture;
    # this company is not represented as an actual published CSL listing.
    rows.append(
        {
            "id": "qbo-fixture-control-42",
            "source": "Entity List (EL) - Bureau of Industry and Security",
            "name": "Example Manufacturing 42",
            "type": "Entity",
            "addresses": [
                {
                    "address": "1 Main Street",
                    "city": "Boston",
                    "country": "US",
                }
            ],
            "programs": ["Synthetic fixture control"],
            "remarks": "Synthetic QBO gate listing; not a real restricted party.",
        }
    )
    return json.dumps(rows).encode()


def fixture_app():
    config = Config()
    require(
        Path(config.database_url.rsplit("/", 1)[-1]).name == "milestone6-fixture.db",
        "fixture_database_name_required",
    )
    require(
        set(config.allowed_hosts) <= {"localhost", "127.0.0.1", "::1"},
        "fixture_local_hosts_only",
    )
    fixture_csl = fixture_csl_data()
    # Keep the real CSL scheduler/recovery path local even when this retained
    # fixture volume runs overnight or restarts with a missed daily occurrence.
    # Overrides live only in this explicitly isolated fixture process.
    from app.csl import service as csl_service

    csl_service.download_bulk = lambda: fixture_csl
    csl_service.download_stamps = lambda api_key: {}
    app = create_app(config)
    remote = RemoteFixture()
    remote.app = app
    app.state.qbo_transport = httpx.MockTransport(remote.request)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application):
            with application.state.session_factory() as db:
                if db.scalar(select(CSLVersion.id)) is None:
                    refresh_csl(db, data=fixture_csl)
            yield

    app.router.lifespan_context = lifespan

    @app.post("/_fixture/qbo/phase")
    def phase(body: PhaseBody, user: AdminUser):
        with remote.lock:
            remote.phase = body.phase
        return {"phase": body.phase}

    @app.get("/_fixture/qbo/status")
    def status(user: AdminUser, db: DB, request: Request):
        with remote.lock:
            return {
                "phase": remote.phase,
                "calls": dict(remote.calls),
                "rotation_persisted_before_data": remote.rotation_persisted_before_data,
            }

    # The production SPA fallback was registered by create_app. Fixture-only
    # GET routes must precede it, or the SPA HTML would mask their actual API.
    fallback = next(
        route for route in app.router.routes if getattr(route, "path", None) == "/{path:path}"
    )
    app.router.routes.remove(fallback)
    app.router.routes.append(fallback)
    return app


class Gate:
    def __init__(self, base):
        parsed = urlsplit(base)
        require(
            parsed.scheme == "http"
            and parsed.hostname in {"localhost", "127.0.0.1"}
            and not parsed.username
            and not parsed.password
            and parsed.path in {"", "/"},
            "fixture_local_base_only",
        )
        self.http = httpx.Client(base_url=base, follow_redirects=False, timeout=30, trust_env=False)
        self.csrf = None
        self.last_call = None
        self.sensitive = [
            APP_SECRET,
            VERIFIER,
            "synthetic-qbo-gate-access-",
            "synthetic-qbo-gate-refresh-",
            "synthetic-qbo-one-use-code",
        ]
        self.evidence = {
            "milestone": 6,
            "validation": "synthetic-contract-real-application",
            "live_sandbox": "deferred",
            "checks": [],
        }

    def call(self, method, path, body=None, expected=200):
        self.last_call = {"method": method, "path": path.split("?")[0]}
        response = self.http.request(
            method,
            path,
            json=body,
            headers={"x-csrf-token": self.csrf} if self.csrf else {},
        )
        require(
            response.status_code == expected,
            "http_" + method + "_" + path.split("?")[0],
        )
        return response

    def check(self, name):
        self.evidence["checks"].append(name)

    def wait(self, attempt_id, expected="success"):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            row = self.call("GET", "/api/v1/monitoring/operations/" + attempt_id).json()
            if row["status"] not in {"queued", "running"}:
                require(row["status"] == expected, "durable_operation_" + expected)
                return row
            time.sleep(0.1)
        raise GateFailure("durable_operation_timeout")

    def phase(self, value):
        self.call("POST", "/_fixture/qbo/phase", {"phase": value})

    def wait_screening(self, outcome):
        task_id = outcome.get("pending_task_id")
        if not task_id:
            return None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            records = self.call(
                "GET",
                "/api/v1/audit-log?action=screening.task_completed"
                + "&target_table=screening_tasks&target_id="
                + task_id,
            ).json()["items"]
            if records:
                return self.call(
                    "GET", "/api/v1/screen/runs/" + records[0]["after"]["run_id"]
                ).json()
            time.sleep(0.1)
        raise GateFailure("original_source_task_completion_timeout")

    def sync(self, mode="incremental", expected="success"):
        queued = self.call(
            "POST", f"/api/v1/sources/{self.source_id}/sync", {"mode": mode}, 202
        ).json()
        require(queued["status"] == "queued", "sync_202_is_queued")
        operation = self.wait(queued["attempt_id"], expected)
        if expected == "success":
            self.wait_screening(operation["outcome"])
        return operation

    def source_tasks(self):
        records = self.call(
            "GET",
            "/api/v1/audit-log?action=screening.enqueued&target_table=screening_tasks&limit=1000",
        ).json()["items"]
        return {
            row["target_id"] for row in records if row["after"].get("source_id") == self.source_id
        }

    def party_inputs(self):
        # Compare persisted party fields, excluding changing derived history/health.
        fields = [column.key for column in Party.__mapper__.column_attrs]
        return {key: {field: row[field] for field in fields} for key, row in self.parties().items()}

    def parties(self):
        rows = self.call("GET", "/api/v1/parties?limit=100").json()["items"]
        return {row["external_id"]: row for row in rows if row["source_id"] == self.source_id}

    def connect(self):
        previous = {
            row["id"] for row in self.call("GET", "/api/v1/monitoring/status").json()["operations"]
        }
        started = self.call("POST", f"/api/v1/sources/{self.source_id}/qbo/oauth/start", {}).json()
        url = urlsplit(started["authorization_url"])
        require(
            url.scheme == "https" and url.netloc == "appcenter.intuit.com",
            "fixed_authorize_url",
        )
        state = parse_qs(url.query)["state"][0]
        self.sensitive.append(state)
        response = self.http.get(
            "/api/v1/connectors/qbo/oauth/callback",
            params={
                "state": state,
                "code": "synthetic-qbo-one-use-code",
                "realmId": "12345",
            },
        )
        require(
            response.status_code == 303 and "connection=connected" in response.headers["location"],
            "oauth_callback_connected",
        )
        require(response.headers["referrer-policy"] == "no-referrer", "callback_no_referrer")
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            rows = self.call("GET", "/api/v1/monitoring/status").json()["operations"]
            attempts = [
                row
                for row in rows
                if row["operation"] == "source_sync"
                and row["source_id"] == self.source_id
                and row["id"] not in previous
            ]
            if attempts:
                applied = self.wait(attempts[0]["id"])
                self.wait_screening(applied["outcome"])
                break
            time.sleep(0.1)
        else:
            raise GateFailure("new_callback_full_sync_attempt_missing")
        require(
            self.call("GET", f"/api/v1/sources/{self.source_id}").json()["record_count"] == 2,
            "initial_full_two_records",
        )

    def authenticate(self, create=False):
        if create:
            payload = {
                "email": ADMIN_EMAIL,
                "display_name": "Fixture QBO Admin",
                "password": PASSWORD,
                "disclaimer_acknowledged": True,
            }
            session = self.call("POST", "/api/v1/setup", payload, 201).json()
        else:
            session = self.call(
                "POST",
                "/api/v1/auth/login",
                {
                    "email": ADMIN_EMAIL,
                    "password": PASSWORD,
                },
            ).json()
        self.csrf = session["csrf_token"]
        # The production cookie remains Secure. This local operator HTTP client
        # deliberately supplies its in-memory session header, as the M5 CLI gate does.
        token = self.http.cookies.get(COOKIE_NAME)
        require(token, "fixture_session_cookie_received")
        self.http.headers["cookie"] = f"{COOKIE_NAME}={token}"

    def run(self):
        fresh = self.call("GET", "/api/v1/setup").json()["needs_setup"]
        self.authenticate(create=fresh)
        users = self.call("GET", "/api/v1/users").json()["items"]
        require(
            len(users) == 1
            and users[0]["email"] == ADMIN_EMAIL
            and users[0]["role"] == "admin"
            and users[0]["active"],
            "fixture_admin_only",
        )
        # A setup-only interrupted gate can resume without deleting anything.
        # Any existing source/book is a different run and must be preserved.
        require(self.call("GET", "/api/v1/sources").json() == [], "fresh_fixture_book_required")
        self.call(
            "PATCH",
            "/api/v1/settings/notifications",
            {"public_base_url": "https://screening.example.test"},
        )
        self.call(
            "PATCH",
            "/api/v1/settings/connectors/qbo",
            {
                "environment": "sandbox",
                "client_id": APP_ID,
                "client_secret": APP_SECRET,
                "verifier_token": VERIFIER,
            },
        )
        source = self.call(
            "POST",
            "/api/v1/sources",
            {
                "kind": "qbo",
                "label": "Synthetic QBO runtime gate",
                "environment": "sandbox",
            },
            201,
        ).json()
        self.source_id = source["id"]
        require(source["next_sync_at"] is None, "configured_source_has_no_poll_schedule")
        self.connect()
        original = self.parties()
        require(set(original) == {"Customer:42", "Vendor:42"}, "entity_ids_are_namespaced")
        self.customer_id = original["Customer:42"]["id"]
        status = self.call("GET", "/_fixture/qbo/status").json()
        require(
            status["calls"].get("refresh_token") == 1 and status["rotation_persisted_before_data"],
            "accepted_rotation_persisted_before_data",
        )
        self.check("oauth_single_use_real_registry_full_import_and_rotation")
        queue = self.call("GET", "/api/v1/queue").json()["items"]
        hit = next(
            row
            for row in queue
            if row["party_id"] == self.customer_id
            and row["entry_name"] == "Example Manufacturing 42"
        )
        original_hit = self.call("GET", "/api/v1/hits/" + hit["id"]).json()
        golden = fingerprint(original_hit)
        decision = self.call(
            "POST",
            "/api/v1/hits/" + hit["id"] + "/dispositions",
            {
                "kind": "false_positive",
                "note": "Synthetic QBO gate human decision.",
            },
            201,
        ).json()["disposition"]
        before_tasks, before_inputs = self.source_tasks(), self.party_inputs()
        unchanged = self.sync("full")["outcome"]
        require(
            unchanged["unchanged"] == 2
            and unchanged["added"] == unchanged["changed"] == unchanged["deleted"] == 0
            and unchanged.get("pending_task_id") is None
            and self.source_tasks() == before_tasks
            and self.party_inputs() == before_inputs,
            "unchanged_hash_creates_no_task_or_party_writes",
        )
        self.check("human_false_positive_and_unchanged_hash_no_write_scope")
        self.phase("changed")
        changed = self.sync()["outcome"]
        require(
            changed["changed"] == 1 and self.wait_screening(changed)["parties_screened"] == 1,
            "cdc_scoped_change",
        )
        require(
            self.parties()["Customer:42"]["id"] == self.customer_id,
            "cdc_identity_preserved",
        )
        detail = self.call("GET", "/api/v1/parties/" + self.customer_id).json()
        require(decision["id"] in json.dumps(detail), "original_human_provenance_retained")
        require(
            fingerprint(self.call("GET", "/api/v1/hits/" + hit["id"]).json()) == golden,
            "original_hit_immutable_after_cdc_change",
        )
        self.check("cdc_edit_scoped_screening_and_original_evidence")
        self.phase("malformed")
        before = self.call("GET", "/api/v1/sources/" + self.source_id).json()
        book = {
            key: {field: value for field, value in row.items() if field != "source"}
            for key, row in self.parties().items()
        }
        self.sync(expected="failed")
        after = self.call("GET", "/api/v1/sources/" + self.source_id).json()
        require(
            book
            == {
                key: {field: value for field, value in row.items() if field != "source"}
                for key, row in self.parties().items()
            }
            and before["last_synced_at"] == after["last_synced_at"],
            "malformed_response_atomic_book_and_watermark",
        )
        self.check("partial_malformed_failure_preserves_book")
        self.phase("inactive")
        self.sync()
        require(
            self.call("GET", "/api/v1/sources/" + self.source_id).json()["record_count"] == 1,
            "inactive_customer_removed_from_active_book",
        )
        self.phase("reactivated")
        self.sync()
        require(
            self.call("GET", "/api/v1/sources/" + self.source_id).json()["record_count"] == 2
            and self.parties()["Customer:42"]["id"] == self.customer_id,
            "reactivated_identity",
        )
        self.check("inactivation_reactivation_preserve_party_identity")
        self.call(
            "PATCH",
            f"/api/v1/sources/{self.source_id}/qbo/connection",
            {"webhooks_enabled": True},
        )
        raw = json.dumps(
            [
                {
                    "specversion": "1.0",
                    "source": "intuit.fixture",
                    "id": "runtime-fixture-event",
                    "type": "qbo.customer.updated.v1",
                    "intuitaccountid": "12345",
                }
            ]
        ).encode()
        signature = base64.b64encode(
            hmac.new(VERIFIER.encode(), raw, hashlib.sha256).digest()
        ).decode()
        for _ in range(2):
            response = self.http.post(
                "/api/v1/webhooks/qbo/sandbox",
                content=raw,
                headers={"intuit-signature": signature},
            )
            require(response.status_code == 200, "signed_webhook_accepted")
        receipts = self.call(
            "GET", "/api/v1/audit-log?action=qbo.webhook_received&target_id=" + self.source_id
        ).json()["items"]
        require(len(receipts) == 1, "signed_duplicate_one_durable_receipt")
        delivered = self.wait(receipts[0]["after"]["attempt_id"])
        self.wait_screening(delivered["outcome"])
        self.check("signed_duplicate_cloudevents_real_http")
        self.phase("refresh_failed")
        self.sync(expected="failed")
        connection = self.call("GET", f"/api/v1/sources/{self.source_id}/qbo/connection").json()
        require(
            connection["status"] == "reconnect_required",
            "invalid_grant_requires_reconnect",
        )
        require(
            self.call("GET", "/api/v1/sources/" + self.source_id).json()["next_sync_at"] is None,
            "lost_grant_stops_poll_schedule",
        )
        self.phase("initial")
        self.connect()
        self.check("invalid_grant_stops_polling_and_explicit_reconnect")
        self.phase("revoke_failed")
        queued = self.call(
            "POST", f"/api/v1/sources/{self.source_id}/qbo/disconnect", {}, 202
        ).json()
        failed = self.wait(queued["attempt_id"], "failed")
        require(
            failed["outcome"]["revocation_status"] == "failed",
            "revoke_failure_truthful",
        )
        require(
            self.call("GET", "/api/v1/sources/" + self.source_id).json()["next_sync_at"] is None,
            "disconnect_stops_polling",
        )
        self.phase("initial")
        queued = self.call(
            "POST", f"/api/v1/sources/{self.source_id}/qbo/disconnect", {}, 202
        ).json()
        self.wait(queued["attempt_id"])
        require(len(self.parties()) == 2, "disconnect_preserves_book")
        self.check("failed_revocation_retry_and_retained_history")
        archive = self.call("GET", "/api/v1/exports/full.zip")
        with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
            for name in bundle.namelist():
                content = bundle.read(name)
                require(
                    all(value.encode() not in content for value in self.sensitive),
                    "export_excludes_credentials_and_oauth_input",
                )
        self.check("real_audit_zip_excludes_secrets_state_code")
        monitoring = self.call("GET", "/api/v1/monitoring/status").json()
        checkpoint = {
            "source_id": self.source_id,
            "party_ids": sorted(row["id"] for row in self.parties().values()),
            "original_hit_id": hit["id"],
            "original_hit_fingerprint": golden,
            "original_disposition_id": decision["id"],
            "schedule_ids": sorted(row["id"] for row in monitoring["schedules"]),
            "source_status": "disconnected",
            "safe_evidence": self.evidence,
        }
        require(not CHECKPOINT.exists(), "restart_checkpoint_never_overwritten")
        CHECKPOINT.write_text(json.dumps(checkpoint, sort_keys=True))
        CHECKPOINT.chmod(0o600)
        self.evidence["record_count"] = 2
        self.evidence["operation_count"] = len(monitoring["operations"])
        return self.evidence

    def restart(self):
        saved = json.loads(CHECKPOINT.read_text())
        self.authenticate()
        self.source_id = saved["source_id"]
        require(
            sorted(row["id"] for row in self.parties().values()) == saved["party_ids"],
            "restart_party_ids_preserved",
        )
        require(
            fingerprint(self.call("GET", "/api/v1/hits/" + saved["original_hit_id"]).json())
            == saved["original_hit_fingerprint"],
            "restart_original_hit_preserved",
        )
        connection = self.call("GET", f"/api/v1/sources/{self.source_id}/qbo/connection").json()
        require(
            connection["status"] == "disconnected" and connection["revocation_status"] == "revoked",
            "restart_disconnected_and_revoked",
        )
        schedules = self.call("GET", "/api/v1/monitoring/status").json()["schedules"]
        require(
            sorted(row["id"] for row in schedules) == saved["schedule_ids"],
            "restart_actual_schedules_preserved",
        )
        return {
            "milestone": 6,
            "restart": "passed",
            "preserved_parties": len(saved["party_ids"]),
            "original_evidence": "unchanged",
            "live_sandbox": "deferred",
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--serve-fixture", action="store_true")
    modes.add_argument("--run-fixture-gate", action="store_true")
    modes.add_argument("--verify-restart", action="store_true")
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()
    if args.serve_fixture:
        import uvicorn

        uvicorn.run(
            fixture_app(),
            host="0.0.0.0",
            port=8000,
            access_log=False,
            log_level="warning",
        )
        return
    gate = Gate(args.base_url)
    try:
        result = gate.restart() if args.verify_restart else gate.run()
        print(json.dumps(result, sort_keys=True))
    except Exception as exc:  # noqa: BLE001 -- never print private request tracebacks
        # Keep request/provider/cookie/environment data outside all tool output.
        print(
            json.dumps(
                {
                    "milestone": 6,
                    "failed_check": str(exc)
                    if isinstance(exc, GateFailure)
                    else type(exc).__name__,
                    "last_call": gate.last_call,
                }
            )
        )
        raise SystemExit(1) from None
    finally:
        gate.http.close()


if __name__ == "__main__":
    main()
