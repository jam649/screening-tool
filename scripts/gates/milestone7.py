#!/usr/bin/env python3
"""Isolated BC synthetic provider, real HTTP gates and retained-volume verifier.

Run only on a fresh named fixture volume. The production application, scheduler,
connector and anonymous webhook route are real; fixed Microsoft calls use an
in-memory transport. Handshake/delivery callbacks cross actual loopback TCP.
Provider state is encrypted in the fixture volume, never published or printed.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sqlite3
import threading
import time
import zipfile
from collections import Counter
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from milestone6 import Gate, GateFailure, fingerprint, fixture_csl_data, require
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text

from app.api.deps import AdminUser
from app.config import Config
from app.connectors.bc_common import load_source_credentials
from app.csl.service import refresh_csl
from app.main import create_app
from app.models import CSLVersion, Source, User
from app.security import COOKIE_NAME, decrypt_json, encrypt_json

TENANT = "11111111-1111-1111-1111-111111111111"
COMPANY = "22222222-2222-2222-2222-222222222222"
OTHER_COMPANY = "44444444-4444-4444-4444-444444444444"
ENTITY = "33333333-3333-3333-3333-333333333333"
APP_ID = "55555555-5555-5555-5555-555555555555"
APP_SECRET = "synthetic-bc-app-secret"
ADMIN_EMAIL = "bc-fixture-admin@example.test"
PASSWORD = "Synthetic BC gate password2026!"
ENVIRONMENT = "Sandbox_US"
BASE = f"https://api.businesscentral.dynamics.com/v2.0/{TENANT}/{ENVIRONMENT}/api/v2.0"
CHECKPOINT = Path("/data/milestone7-backend-checkpoint.json")
REMOTE_STATE = Path("/data/milestone7-synthetic-provider.enc")
PHASES = Literal[
    "initial",
    "changed",
    "type_changed",
    "deleted",
    "restored",
    "malformed",
    "cleanup_failed",
    "create_timeout",
    "renew_conflict",
    "renew_missing",
]


class PhaseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phase: PHASES


class NotifyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    change_type: Literal["updated", "deleted", "collection"] = "updated"
    invalid_batch: bool = False
    duplicate: bool = True


class RemoteFixture:
    def __init__(self, config):
        self.config, self.app = config, None
        self.phase, self.once = "initial", False
        self.expiry_forced = False
        self.lock, self.calls = threading.RLock(), Counter()
        self.handshakes = 0
        self.writer_checks = 0
        self.subscriptions, self.serial = {}, 0
        if REMOTE_STATE.exists():
            state = decrypt_json(REMOTE_STATE.read_text(), config)
            self.subscriptions, self.serial = state["subscriptions"], state["serial"]

    def save(self):
        REMOTE_STATE.write_text(
            encrypt_json(
                {"subscriptions": self.subscriptions, "serial": self.serial},
                self.config,
            )
        )
        REMOTE_STATE.chmod(0o600)

    def callback(self, url, *, payload=None, validation=False):
        parsed = urlsplit(url)
        require(
            parsed.scheme == "https" and parsed.hostname == "screening.example.test",
            "fixture_https_callback_identity",
        )
        started = time.monotonic()
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=4.5) as client:
            if validation:
                token = "synthetic BC validation + opaque"
                response = client.post(
                    "http://127.0.0.1:8000" + parsed.path,
                    params={"validationToken": token},
                )
                require(
                    response.status_code == 200 and response.text == token,
                    "real_tcp_handshake_echo",
                )
                require(
                    time.monotonic() - started < 5,
                    "real_tcp_handshake_under_five_seconds",
                )
                require(
                    response.headers.get("content-type", "").startswith("text/plain"),
                    "handshake_plain_text",
                )
                require(
                    response.headers.get("referrer-policy") == "no-referrer"
                    and response.headers.get("cache-control") == "no-store",
                    "handshake_private_headers",
                )
                self.handshakes += 1
            else:
                response = client.post("http://127.0.0.1:8000" + parsed.path, json=payload)
        return response.status_code

    def request(self, request):
        with self.lock:
            require(request.url.scheme == "https", "fixed_https_provider")
            with self.app.state.session_factory() as db:
                db.execute(text("BEGIN IMMEDIATE"))
                db.rollback()
                self.writer_checks += 1
            if request.url.host == "login.microsoftonline.com":
                require(
                    str(request.url)
                    == f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token",
                    "fixed_tenant_token_endpoint",
                )
                form = parse_qs(request.content.decode())
                require(
                    form.get("grant_type") == ["client_credentials"]
                    and form.get("scope") == ["https://api.businesscentral.dynamics.com/.default"],
                    "actual_client_credentials_contract",
                )
                self.calls["token"] += 1
                return httpx.Response(
                    200,
                    json={
                        "token_type": "Bearer",
                        "access_token": "synthetic-bc-runtime-bearer",
                        "expires_in": 3600,
                    },
                )
            require(
                str(request.url).startswith(BASE + "/")
                and request.url.host == "api.businesscentral.dynamics.com",
                "fixed_tenant_environment_api",
            )
            require(
                request.headers.get("authorization") == "Bearer synthetic-bc-runtime-bearer",
                "opaque_bearer_header",
            )
            path = request.url.path
            self.calls[request.method] += 1
            if path.endswith("/companies"):
                require(
                    request.headers.get("prefer") == "odata.maxpagesize=300",
                    "company_page_prefer",
                )
                page = OTHER_COMPANY if request.url.params.get("$skiptoken") else COMPANY
                payload = {
                    "value": [
                        {
                            "id": page,
                            "displayName": "Synthetic second company"
                            if page == OTHER_COMPANY
                            else "Synthetic BC company",
                        }
                    ]
                }
                if page == COMPANY:
                    payload["@odata.nextLink"] = (
                        BASE + "/companies?$skiptoken=synthetic-company-continuation"
                    )
                return httpx.Response(200, json=payload)
            if path.endswith(f"/companies({COMPANY})"):
                return httpx.Response(
                    200, json={"id": COMPANY, "displayName": "Synthetic BC company"}
                )
            if "/subscriptions" in path:
                return self.subscription(request)
            resource = path.rsplit("/", 1)[-1]
            require(
                resource in {"customers", "vendors"} and f"/companies({COMPANY})/" in path,
                "exact_company_entity_path",
            )
            require(
                request.headers.get("prefer") == "odata.maxpagesize=300"
                and "$top" not in request.url.params,
                "actual_paging_contract",
            )
            self.calls[resource] += 1
            if self.phase == "malformed" and resource == "vendors":
                return httpx.Response(200, json={"value": "partial-invalid-page"})
            now = datetime.now(UTC).isoformat()
            rows = [
                {
                    "id": ENTITY,
                    "displayName": "Example Manufacturing 42 Updated"
                    if self.phase in {"changed", "type_changed"} and resource == "customers"
                    else "Example Manufacturing 42"
                    if resource == "customers"
                    else "Synthetic Fixture Logistics",
                    "type": "Person" if self.phase == "type_changed" else "Company",
                    "addressLine1": "1 Main Street" if resource == "customers" else "",
                    "city": "Boston" if resource == "customers" else "Seattle",
                    "state": "MA" if resource == "customers" else "WA",
                    "country": "US",
                    "postalCode": "02110",
                    "blocked": "All",
                    "lastModifiedDateTime": now,
                }
            ]
            if request.url.params.get("$skiptoken") or (
                self.phase == "deleted" and resource == "customers"
            ):
                rows = []
            payload = {
                "value": rows,
                "@odata.count": 0 if self.phase == "deleted" and resource == "customers" else 1,
            }
            if resource == "customers" and rows:
                payload["@odata.nextLink"] = (
                    BASE + f"/companies({COMPANY})/customers?$skiptoken=synthetic%2Fopaque%2B%3D"
                )
            return httpx.Response(200, json=payload)

    def subscription(self, request):
        method, path = request.method, unquote(request.url.path)
        self.calls["subscriptions_" + method] += 1
        if method == "GET":
            if self.phase in {"renew_conflict", "renew_missing"} and not self.expiry_forced:
                for row in self.subscriptions.values():
                    row["expirationDateTime"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
                self.expiry_forced = True
                self.save()
            return httpx.Response(200, json={"value": list(self.subscriptions.values())})
        if method == "POST":
            body = json.loads(request.content)
            resource = body["resource"].rsplit("/", 1)[-1]
            self.calls["subscriptions_POST_" + resource] += 1
            source_id = urlsplit(body["notificationUrl"]).path.rsplit("/", 1)[-1]
            with self.app.state.session_factory() as db:
                source = db.get(Source, source_id)
                require(source is not None, "prepost_source_exists")
                values = load_source_credentials(source, self.config)
                admin_id = db.scalar(select(User.id).where(User.email == ADMIN_EMAIL))
                intents = [
                    row
                    for row in values.get("pending_cleanup", [])
                    if row.get("resource") == resource
                    and row.get("client_state") == body["clientState"]
                    and row.get("resource_path") == body["resource"]
                    and row.get("notification_url") == body["notificationUrl"]
                    and row.get("subscription_id") is None
                ]
                require(
                    len(intents) == 1
                    and intents[0].get("purpose") == "activate"
                    and intents[0].get("issuer_user_id") == admin_id
                    and intents[0].get("tenant_id") == TENANT
                    and intents[0].get("environment") == ENVIRONMENT
                    and intents[0].get("company_id") == COMPANY
                    and intents[0].get("client_id") == APP_ID
                    and intents[0].get("client_secret") == APP_SECRET
                    and intents[0].get("source_generation")
                    == source.config["connection_generation"]
                    and body["clientState"] not in source.credentials_enc,
                    "actual_encrypted_owned_intent_committed_before_post",
                )
                self.calls["prepost_custody_" + resource] += 1
            self.callback(body["notificationUrl"], validation=True)
            self.serial += 1
            id = f"synthetic-{self.serial}-o'connor"
            row = {
                **body,
                "subscriptionId": id,
                "expirationDateTime": (datetime.now(UTC) + timedelta(days=3)).isoformat(),
                "@odata.etag": f'W/"synthetic-{self.serial}-1"',
            }
            self.subscriptions[id] = row
            self.save()
            if self.phase == "create_timeout" and not self.once:
                self.once = True
                raise httpx.ReadTimeout("Synthetic uncertain accepted create", request=request)
            return httpx.Response(201, json=row, headers={"ETag": row["@odata.etag"]})
        found = re.search(r"/subscriptions\('(.+)'\)$", path)
        require(found is not None, "quoted_opaque_subscription_key")
        id = found[1].replace("''", "'")
        row = self.subscriptions.get(id)
        if row is None:
            return httpx.Response(404, json={"error": {"code": "SyntheticMissing"}})
        if self.phase == "renew_missing" and method == "PATCH" and not self.once:
            self.once = True
            del self.subscriptions[id]
            self.save()
            return httpx.Response(404, json={"error": {"code": "SyntheticMissing"}})
        if self.phase == "renew_conflict" and method == "PATCH" and not self.once:
            self.once = True
            row["@odata.etag"] = 'W/"synthetic-conflict"'
            self.save()
            return httpx.Response(412, json={"error": {"code": "SyntheticConflict"}})
        require(
            request.headers.get("if-match") == row["@odata.etag"],
            "actual_etag_if_match",
        )
        if method == "DELETE":
            require(not request.content, "delete_has_no_body")
            if self.phase == "cleanup_failed":
                return httpx.Response(503, json={"error": {"code": "SyntheticUnavailable"}})
            del self.subscriptions[id]
            self.save()
            return httpx.Response(204)
        require(method == "PATCH", "fixed_subscription_method")
        body = json.loads(request.content)
        self.callback(body["notificationUrl"], validation=True)
        row.update(body)
        row["expirationDateTime"] = (datetime.now(UTC) + timedelta(days=3)).isoformat()
        row["@odata.etag"] = f'W/"synthetic-renew-{self.handshakes}"'
        self.save()
        return httpx.Response(200, json=row, headers={"ETag": row["@odata.etag"]})


def fixture_app():
    config = Config()
    require(
        config.database_url.rsplit("/", 1)[-1] == "milestone7-fixture.db",
        "fixture_database_name_required",
    )
    require(
        set(config.allowed_hosts) <= {"localhost", "127.0.0.1", "::1"},
        "fixture_local_hosts_only",
    )
    rows = json.loads(fixture_csl_data())
    rows[-1].update(
        id="bc-fixture-control-42",
        remarks="Synthetic BC gate control; not a real restricted party.",
    )
    data = json.dumps(rows).encode()
    from app.csl import service as csl_service

    csl_service.download_bulk = lambda: data
    csl_service.download_stamps = lambda api_key: {}
    app = create_app(config)
    remote = RemoteFixture(config)
    remote.app = app
    app.state.bc_transport = httpx.MockTransport(remote.request)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application):
            with application.state.session_factory() as db:
                if db.scalar(select(CSLVersion.id)) is None:
                    refresh_csl(db, data=data)
            yield

    app.router.lifespan_context = lifespan

    @app.post("/_fixture/bc/phase")
    def phase(body: PhaseBody, user: AdminUser):
        with remote.lock:
            remote.phase, remote.once = body.phase, False
            remote.expiry_forced = False
        return {"phase": body.phase}

    @app.get("/_fixture/bc/status")
    def status(user: AdminUser):
        with remote.lock:
            return {
                "phase": remote.phase,
                "calls": dict(remote.calls),
                "handshakes": remote.handshakes,
                "writer_checks": remote.writer_checks,
                "subscription_count": len(remote.subscriptions),
            }

    @app.post("/_fixture/bc/notify")
    def notify(body: NotifyBody, user: AdminUser):
        with remote.lock:
            row = next(
                r for r in remote.subscriptions.values() if r["resource"].endswith("/customers")
            )
            event = {
                "subscriptionId": row["subscriptionId"],
                "clientState": row["clientState"],
                "resource": row["resource"]
                + (f"({ENTITY})" if body.change_type != "collection" else "?$filter=synthetic"),
                "changeType": body.change_type,
                "lastModifiedDateTime": datetime.now(UTC).isoformat(),
                "expirationDateTime": row["expirationDateTime"],
            }
            events = [event]
            if body.invalid_batch:
                events.append({**deepcopy(event), "clientState": "synthetic-invalid-client-state"})
            statuses = [remote.callback(row["notificationUrl"], payload={"value": events})]
            if body.duplicate:
                statuses.append(remote.callback(row["notificationUrl"], payload={"value": events}))
            return {"delivery_statuses": statuses}

    fallback = next(r for r in app.router.routes if getattr(r, "path", None) == "/{path:path}")
    app.router.routes.remove(fallback)
    app.router.routes.append(fallback)
    return app


def wait_for_health(http, *, seconds=60, clock=time.monotonic, pause=time.sleep):
    deadline = clock() + seconds
    while clock() < deadline:
        try:
            response = http.get("/api/v1/health", timeout=min(2, max(0.1, deadline - clock())))
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        pause(min(0.2, max(0, deadline - clock())))
    raise GateFailure("fixture_api_health_timeout")


@contextmanager
def readonly_database(config):
    require(
        config.database_url == "sqlite:////data/milestone7-fixture.db",
        "readonly_fixture_database_only",
    )
    with sqlite3.connect("file:/data/milestone7-fixture.db?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        yield db


def private_values(value):
    """Collect known secret values in memory only, excluding public source identity."""
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"client_id", "client_secret", "access_token", "client_state", "clientState"}:
                if isinstance(item, str) and item:
                    found.add(item)
            found.update(private_values(item))
    elif isinstance(value, list):
        for item in value:
            found.update(private_values(item))
    return found


def verify_redaction(value, *, in_audit_snapshot=False, path=()):
    """Private keys may survive solely as exact markers in original audit snapshots."""
    if isinstance(value, dict):
        is_audit = "action" in value and "target_table" in value
        for key, item in value.items():
            private = key in {
                "client_id",
                "client_secret",
                "access_token",
                "client_state",
                "clientState",
                "refresh_token",
                "credentials_enc",
                "cursor",
                "state",
            } or key.endswith("_enc")
            geography = key == "state" and (
                (
                    {"displayName", "id", "country", "lastModifiedDateTime"} <= value.keys()
                    and item in {"MA", "WA"}
                )
                or (len(path) >= 2 and path[-2:] == ("addresses", "[]"))
            )
            private = private and not geography
            if private:
                require(
                    in_audit_snapshot and (item is None or item == "[redacted]"),
                    "zip_private_fields_only_exact_audit_markers",
                )
            if key == "last_error":
                require(item is None or item == "[redacted]", "zip_health_detail_redacted")
            verify_redaction(
                item,
                in_audit_snapshot=in_audit_snapshot or (is_audit and key in {"before", "after"}),
                path=(*path, key),
            )
    elif isinstance(value, list):
        for item in value:
            verify_redaction(item, in_audit_snapshot=in_audit_snapshot, path=(*path, "[]"))


def fixed_resource_matches(value, entity):
    """Only the two documented spellings of this exact bound resource qualify."""
    if entity not in {"customers", "vendors"} or not isinstance(value, str):
        return False
    expected = f"api/v2.0/companies({COMPANY})/{entity}"
    return value in {expected, "/" + expected}


def verify_subscription_custody(private, remote, *, source_id, admin_id):
    """A remotely accepted grant must remain pending or have exact trusted adoption."""
    require(
        private.get("tenant_id") == TENANT and private.get("client_id") == APP_ID,
        "uncertain_custody_current_app_tenant_ownership",
    )
    for entity in ("customers", "vendors"):
        url = f"https://screening.example.test/api/v1/webhooks/bc/{source_id}"
        accepted = [
            row for row in remote.values() if fixed_resource_matches(row.get("resource"), entity)
        ]
        require(len(accepted) == 1, "uncertain_custody_one_remote_grant_per_resource")
        accepted = accepted[0]
        require(
            accepted.get("notificationUrl") == url and accepted.get("clientState"),
            "uncertain_custody_exact_remote_url_state",
        )
        active = [
            row
            for row in private.get("subscriptions", [])
            if row.get("resource") == entity
            and fixed_resource_matches(row.get("resource_path"), entity)
            and row.get("notification_url") == url
            and row.get("client_state") == accepted["clientState"]
            and row.get("subscription_id") == accepted["subscriptionId"]
        ]
        pending = [
            row
            for row in private.get("pending_cleanup", [])
            if row.get("resource") == entity
            and fixed_resource_matches(row.get("resource_path"), entity)
            and row.get("notification_url") == url
            and row.get("client_state") == accepted["clientState"]
            and row.get("subscription_id") in {None, accepted["subscriptionId"]}
            and row.get("purpose") == "activate"
            and row.get("issuer_user_id") == admin_id
            and row.get("tenant_id") == TENANT
            and row.get("environment") == ENVIRONMENT
            and row.get("company_id") == COMPANY
            and row.get("client_id") == APP_ID
            and row.get("client_secret") == APP_SECRET
            and row.get("source_generation") == private.get("source_generation")
            and row.get("app_generation") == private.get("app_generation")
        ]
        require(
            len(active) + len(pending) == 1,
            "uncertain_accepted_grant_exact_pending_or_adopted_custody",
        )


class BCGate(Gate):
    def __init__(self, base):
        super().__init__(base)
        self.http.headers["origin"] = base.rstrip("/")
        self.sensitive = {APP_SECRET, APP_ID, "synthetic-bc-runtime-bearer"}
        self.operation_ids, self.run_ids = set(), set()
        self.evidence = {
            "milestone": 7,
            "validation": "synthetic-contract-real-application",
            "live_sandbox": "deferred",
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
                    {"display_name": "Fixture BC Admin", "disclaimer_acknowledged": True}
                    if create
                    else {}
                ),
            },
            201 if create else 200,
        ).json()
        self.admin_id, self.csrf = session["user"]["id"], session["csrf_token"]
        token = self.http.cookies.get(COOKIE_NAME)
        require(token, "fixture_secure_cookie_received")
        self.http.headers["cookie"] = f"{COOKIE_NAME}={token}"

    def phase(self, value):
        self.call("POST", "/_fixture/bc/phase", {"phase": value})

    def wait(self, attempt_id, expected="success"):
        row = super().wait(attempt_id, expected)
        self.operation_ids.add(attempt_id)
        self.run_ids.update(row.get("outcome", {}).get("run_ids", []))
        return row

    def wait_screening(self, outcome):
        run = super().wait_screening(outcome)
        if run:
            self.run_ids.add(run["id"])
        return run

    def connection(self):
        return self.call("GET", f"/api/v1/sources/{self.source_id}/bc/connection").json()

    def provider(self):
        return self.call("GET", "/_fixture/bc/status").json()

    def reconcile(self, expected="success"):
        queued = self.call(
            "POST", f"/api/v1/sources/{self.source_id}/bc/subscriptions/reconcile", {}, 202
        ).json()
        return self.wait(queued["attempt_id"], expected)

    def receipts(self):
        return self.call(
            "GET",
            f"/api/v1/audit-log?action=bc.webhook_received&target_id={self.source_id}&limit=1000",
        ).json()["items"]

    def source_boundary(self):
        with readonly_database(Config()) as db:
            row = db.execute(
                "SELECT cursor,last_synced_at,config FROM sources WHERE id=?", (self.source_id,)
            ).fetchone()
            require(row is not None, "source_boundary_exists")
            return (
                row["cursor"],
                row["last_synced_at"],
                json.loads(row["config"]).get("last_full_sync_at"),
            )

    def remember_private(self):
        config = Config()
        with readonly_database(config) as db:
            row = db.execute(
                "SELECT credentials_enc FROM sources WHERE id=?", (self.source_id,)
            ).fetchone()
            require(row is not None, "private_source_exists")
            values = load_source_credentials(SimpleNamespace(credentials_enc=row[0]), config)
            self.sensitive.update(private_values(values))
            setting = db.execute("SELECT value FROM settings WHERE key='bc_app'").fetchone()
            require(setting is not None, "private_global_app_exists")
            self.sensitive.update(private_values(decrypt_json(setting[0], config)))
        return values

    def export_privacy(self):
        response = self.call("GET", "/api/v1/exports/full.zip")
        require(
            hashlib.sha256(response.content).hexdigest() == response.headers.get("x-export-sha256"),
            "actual_zip_header_checksum",
        )
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            names = set(archive.namelist())
            require(set(manifest["files"]) == names - {"manifest.json"}, "zip_manifest_complete")
            for name in names:
                content = archive.read(name)
                require(
                    all(secret.encode() not in content for secret in self.sensitive),
                    "zip_no_private_values",
                )
                if name in manifest["files"]:
                    proof = manifest["files"][name]
                    require(
                        proof["sha256"] == hashlib.sha256(content).hexdigest()
                        and proof["bytes"] == len(content),
                        "zip_every_member_checksum",
                    )
                if name.endswith(".json"):
                    verify_redaction(json.loads(content))
                elif name.endswith(".jsonl"):
                    for line in content.splitlines():
                        verify_redaction(json.loads(line))
                if name == "sources.jsonl":
                    require(
                        all(
                            "credentials_enc" not in json.loads(line)
                            and "cursor" not in json.loads(line)
                            for line in content.splitlines()
                        ),
                        "zip_no_private_source_fields",
                    )
            require(f"parties/{self.customer_id}.json" in names, "zip_original_party_file_present")
        audits = self.call("GET", "/api/v1/audit-log?action=export.created&limit=1000").json()[
            "items"
        ]
        require(
            any(
                row["actor_user_id"] == self.admin_id
                and row["after"].get("sha256") == response.headers["x-export-sha256"]
                for row in audits
            ),
            "zip_actual_actor_and_checksum_audit",
        )
        self.check("actual_full_zip_private_cleanup_and_member_checksums")

    def notify(self, *, change_type="updated", invalid_batch=False):
        before = {row["id"] for row in self.receipts()}
        delivered = self.call(
            "POST",
            "/_fixture/bc/notify",
            {"change_type": change_type, "invalid_batch": invalid_batch, "duplicate": True},
        ).json()
        require(
            delivered["delivery_statuses"] == ([400, 400] if invalid_batch else [200, 200]),
            "actual_tcp_notification_statuses",
        )
        rows = [row for row in self.receipts() if row["id"] not in before]
        require(
            len(rows) == (0 if invalid_batch else 1), "mixed_state_atomic_or_duplicate_one_receipt"
        )
        if rows:
            operation = self.wait(rows[0]["after"]["attempt_id"])
            self.wait_screening(operation["outcome"])
            return operation
        return None

    def human_proof(self, original_hit):
        queue = self.call("GET", "/api/v1/queue").json()["items"]
        hit = next(
            row
            for row in queue
            if row["party_id"] == self.customer_id
            and row["entry_name"] == "Example Manufacturing 42"
        )
        original = self.call("GET", "/api/v1/hits/" + hit["id"]).json()
        decision = self.call(
            "POST",
            "/api/v1/hits/" + hit["id"] + "/dispositions",
            {"kind": "false_positive", "note": "Synthetic BC gate original human decision."},
            201,
        ).json()["disposition"]
        require(
            decision["actor_user_id"] == self.admin_id
            and decision["note"] == "Synthetic BC gate original human decision.",
            "actual_original_human_actor_note",
        )
        records = self.call("GET", "/api/v1/audit-log?action=review.disposition&limit=1000").json()[
            "items"
        ]
        group = [row for row in records if row["after"].get("queue_hit_id") == hit["id"]]
        require(
            group
            and all(
                row["actor_user_id"] == self.admin_id
                and row["after"]["party_hash"] == original["party_hash"]
                and row["after"]["note"] == decision["note"]
                for row in group
            ),
            "actual_original_human_audit_group",
        )
        original_hit.update(
            id=hit["id"],
            fingerprint=fingerprint(original),
            disposition_id=decision["id"],
            audit_ids=sorted(row["id"] for row in group),
            audit_fingerprint=hashlib.sha256(
                json.dumps(group, sort_keys=True).encode()
            ).hexdigest(),
        )
        return {row["target_id"] for row in group}

    def run(self):
        require(not CHECKPOINT.exists(), "checkpoint_never_overwritten")
        # Check the explicit local fixture installation before setup/settings writes.
        with readonly_database(Config()) as db:
            require(
                db.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0,
                "fresh_local_fixture_sources",
            )
            require(
                db.execute(
                    "SELECT id FROM csl_entries WHERE csl_id='bc-fixture-control-42'"
                ).fetchone()
                is not None,
                "synthetic_bc_listing_control_required",
            )
            users = list(db.execute("SELECT email,role,active FROM users"))
            require(
                not users
                or (
                    len(users) == 1
                    and users[0]["email"] == ADMIN_EMAIL
                    and users[0]["role"] == "admin"
                    and users[0]["active"]
                ),
                "local_dummy_admin_only",
            )
        self.authenticate(create=self.call("GET", "/api/v1/setup").json()["needs_setup"])
        users = self.call("GET", "/api/v1/users").json()["items"]
        require(
            len(users) == 1
            and users[0]["email"] == ADMIN_EMAIL
            and users[0]["active"]
            and users[0]["role"] == "admin",
            "fresh_dummy_admin_only",
        )
        require(self.call("GET", "/api/v1/sources").json() == [], "fresh_sources_required")
        flags = self.call("GET", "/api/v1/settings/connectors/bc").json()
        require(not flags["configured"], "fresh_global_app_required")
        flags = self.call(
            "PATCH",
            "/api/v1/settings/connectors/bc",
            {"client_id": APP_ID, "client_secret": APP_SECRET},
        ).json()
        require(
            flags["configured"]
            and APP_ID not in json.dumps(flags)
            and APP_SECRET not in json.dumps(flags),
            "global_app_write_only_flags",
        )
        self.call(
            "PATCH",
            "/api/v1/settings/notifications",
            {"public_base_url": "https://screening.example.test"},
        )
        source = self.call(
            "POST",
            "/api/v1/sources",
            {
                "kind": "bc",
                "label": "Synthetic BC runtime gate",
                "tenant_id": TENANT,
                "bc_environment": ENVIRONMENT,
            },
            201,
        ).json()
        self.source_id = source["id"]
        require(
            source["next_sync_at"] is None and self.connection()["company_id"] is None,
            "unbound_source_not_scheduled",
        )
        before = self.provider()["calls"].get("GET", 0)
        companies = self.call("POST", f"/api/v1/sources/{self.source_id}/bc/companies", {}).json()[
            "items"
        ]
        require(
            {row["id"] for row in companies} == {COMPANY, OTHER_COMPANY}
            and self.provider()["calls"].get("GET", 0) == before + 2
            and self.connection()["company_id"] is None,
            "two_page_discovery_no_binding",
        )
        connected = self.call(
            "POST", f"/api/v1/sources/{self.source_id}/bc/connect", {"company_id": COMPANY}, 202
        ).json()
        operation = self.wait(connected["attempt_id"])
        initial_run = self.wait_screening(operation["outcome"])
        require(
            initial_run is not None and initial_run["parties_screened"] == 2,
            "connect_exact_source_task_two_parties",
        )
        original = self.parties()
        customer_key, vendor_key = "Customer:" + ENTITY, "Vendor:" + ENTITY
        require(
            set(original) == {customer_key, vendor_key}
            and all(
                row["deleted_at"] is None and row["raw"]["blocked"] == "All"
                for row in original.values()
            ),
            "namespaced_guid_and_posting_blocks_active",
        )
        self.customer_id = original[customer_key]["id"]
        self.check("write_only_app_unbound_discovery_connect_full_actual_task")
        proof = {}
        human_ids = self.human_proof(proof)
        tasks, inputs = self.source_tasks(), self.party_inputs()
        unchanged = self.sync("full")["outcome"]
        require(
            unchanged["unchanged"] == 2
            and unchanged["changed"] == unchanged["added"] == unchanged["deleted"] == 0
            and unchanged.get("pending_task_id") is None
            and tasks == self.source_tasks()
            and inputs == self.party_inputs(),
            "unchanged_full_no_task_or_party_field_writes",
        )
        self.phase("changed")
        changed = self.sync()["outcome"]
        require(
            changed["changed"] == 1 and self.wait_screening(changed)["parties_screened"] == 1,
            "incremental_changed_exact_party_scope",
        )
        before_type = self.parties()[customer_key]
        self.phase("type_changed")
        typed = self.sync()["outcome"]
        typed_run = self.wait_screening(typed)
        after_type = self.parties()[customer_key]
        require(
            typed["changed"] == 1
            and typed_run["parties_screened"] == 1
            and typed_run["settings_snapshot"]["hash_rule_version"] == 2
            and after_type["party_type"] == "individual"
            and after_type["name"] == before_type["name"]
            and after_type["record_hash"] != before_type["record_hash"],
            "type_only_v2_hash_change_scoped",
        )
        detail = self.call("GET", "/api/v1/parties/" + self.customer_id).json()
        require(
            any(
                hit["explanation"].get("prior_disposition_id") in human_ids
                for event in detail["events"]
                if event["run"]["id"] == typed_run["id"]
                for hit in event["hits"]
            ),
            "reopened_pair_actual_original_human_authority",
        )
        require(
            fingerprint(self.call("GET", "/api/v1/hits/" + proof["id"]).json())
            == proof["fingerprint"],
            "original_hit_fingerprint_unchanged",
        )
        self.check("original_human_group_unchanged_hash_and_type_reopen")
        self.phase("malformed")
        book, boundary = self.party_inputs(), self.source_boundary()
        self.sync("full", "failed")
        require(
            self.party_inputs() == book and self.source_boundary() == boundary,
            "malformed_full_preserves_book_cursor_last_sync",
        )
        self.check("partial_full_atomic_with_actual_persisted_watermarks")

        self.phase("create_timeout")
        enable = self.call(
            "PATCH",
            f"/api/v1/sources/{self.source_id}/bc/connection",
            {"webhooks_enabled": True},
            202,
        ).json()
        self.wait(enable["attempt_id"], "failed")
        private = self.remember_private()
        remote = decrypt_json(REMOTE_STATE.read_text(), Config())["subscriptions"]
        self.sensitive.update(private_values(remote))
        verify_subscription_custody(
            private, remote, source_id=self.source_id, admin_id=self.admin_id
        )
        requests = self.call(
            "GET",
            f"/api/v1/audit-log?action=bc.subscription_requested&target_id={self.source_id}&limit=1000",
        ).json()["items"]
        issuance = self.call(
            "GET",
            f"/api/v1/audit-log?action=bc.webhooks_changed&target_id={self.source_id}&limit=1000",
        ).json()["items"]
        status = self.provider()
        require(
            len(requests) == 2
            and {row["after"]["resource"] for row in requests} == {"customers", "vendors"}
            and all(row["actor_user_id"] is None for row in requests)
            and any(
                row["actor_user_id"] == self.admin_id
                and row["after"]["attempt_id"] == enable["attempt_id"]
                for row in issuance
            )
            and status["subscription_count"] == 2
            and all(
                status["calls"].get("subscriptions_POST_" + entity) == 1
                and status["calls"].get("prepost_custody_" + entity) == 1
                for entity in ("customers", "vendors")
            )
            and status["handshakes"] == 2,
            "uncertain_create_prepost_ledger_and_pending_or_trusted_custody",
        )
        self.phase("initial")
        self.reconcile()
        require(
            self.connection()["subscription_status"] == "active"
            and self.provider()["subscription_count"] == 2
            and self.provider()["calls"].get("subscriptions_POST_customers") == 1
            and self.provider()["calls"].get("subscriptions_POST_vendors") == 1
            and self.provider()["handshakes"] == 2,
            "trusted_adoption_no_second_customer_create_two_tcp_challenges",
        )
        self.remember_private()
        for phase in ("renew_conflict", "renew_missing"):
            self.phase(phase)
            self.reconcile()
            require(
                self.provider()["subscription_count"] == 2
                and self.connection()["subscription_status"] == "active",
                "renewal_recovery_two_actual_remote_subscriptions",
            )
        require(self.provider()["handshakes"] >= 6, "actual_tcp_create_and_renewal_challenges")
        self.check("uncertain_create_adoption_conflict_missing_renewal_actual_tcp")

        self.phase("initial")
        self.notify(invalid_batch=True)
        self.notify()
        self.check("mixed_client_state_atomic_rejection_and_duplicate_receipt")
        self.phase("deleted")
        absent = self.sync()["outcome"]
        require(
            absent["deleted"] == 0 and absent["record_count"] == 2,
            "incremental_absence_never_deletes",
        )
        removed = self.notify(change_type="deleted")["outcome"]
        require(
            removed["deleted"] == 1 and removed["record_count"] == 1,
            "deleted_signal_full_reconciliation",
        )
        self.phase("restored")
        restored = self.sync("full")["outcome"]
        require(
            restored["changed"] == 1 and self.parties()[customer_key]["id"] == self.customer_id,
            "full_restoration_original_party_identity",
        )
        self.check("hard_delete_full_signal_and_same_identity_restoration")

        self.phase("cleanup_failed")
        queued = self.call(
            "POST", f"/api/v1/sources/{self.source_id}/bc/disconnect", {}, 202
        ).json()
        failed = self.wait(queued["attempt_id"], "failed")
        private = self.remember_private()
        monitoring = self.call("GET", "/api/v1/monitoring/status").json()
        schedules = {row["id"]: row for row in monitoring["schedules"]}
        require(
            failed["outcome"]["cleanup_status"] == "failed"
            and private.get("pending_cleanup")
            and self.call("GET", f"/api/v1/sources/{self.source_id}").json()["next_sync_at"] is None
            and "source-" + self.source_id not in schedules
            and "bc-subscriptions-" + self.source_id in schedules,
            "failed_cleanup_stops_poll_but_keeps_lifecycle_schedule",
        )
        self.export_privacy()
        self.phase("initial")
        queued = self.call(
            "POST", f"/api/v1/sources/{self.source_id}/bc/disconnect", {}, 202
        ).json()
        self.wait(queued["attempt_id"])
        require(
            self.provider()["subscription_count"] == 0
            and self.connection()["status"] == "disconnected"
            and not self.connection()["access_token_configured"]
            and len(self.parties()) == 2,
            "cleanup_retry_remote_empty_private_credentials_cleared_history_preserved",
        )
        self.check("failed_cleanup_private_obligations_export_and_successful_retry")
        require(
            fingerprint(self.call("GET", "/api/v1/hits/" + proof["id"]).json())
            == proof["fingerprint"],
            "finished_original_hit_immutable",
        )
        monitoring = self.call("GET", "/api/v1/monitoring/status").json()
        require(
            "source-" + self.source_id not in {row["id"] for row in monitoring["schedules"]}
            and "bc-subscriptions-" + self.source_id
            not in {row["id"] for row in monitoring["schedules"]},
            "completed_disconnect_has_no_source_schedules",
        )
        saved = {
            "passed": True,
            "source_id": self.source_id,
            "party_ids": sorted(row["id"] for row in self.parties().values()),
            "original_hit_id": proof["id"],
            "original_hit_fingerprint": proof["fingerprint"],
            "original_disposition_id": proof["disposition_id"],
            "human_audit_ids": proof["audit_ids"],
            "human_audit_fingerprint": proof["audit_fingerprint"],
            "operation_ids": sorted(self.operation_ids),
            "run_ids": sorted(self.run_ids),
            "schedule_ids": sorted(row["id"] for row in monitoring["schedules"]),
            "source_status": "disconnected",
        }
        with CHECKPOINT.open("x") as output:
            json.dump(saved, output, sort_keys=True)
        CHECKPOINT.chmod(0o600)
        self.evidence.update(
            passed=True,
            party_count=2,
            operation_count=len(self.operation_ids),
            run_count=len(self.run_ids),
            tcp_handshakes=self.provider()["handshakes"],
            writer_checks=self.provider()["writer_checks"],
        )
        return self.evidence

    def restart(self):
        require(CHECKPOINT.exists(), "passed_checkpoint_required")
        saved = json.loads(CHECKPOINT.read_text())
        require(saved.get("passed") is True, "passed_checkpoint_required")
        self.authenticate()
        self.source_id = saved["source_id"]
        require(
            sorted(row["id"] for row in self.parties().values()) == saved["party_ids"],
            "restart_party_ids_preserved",
        )
        original = self.call("GET", "/api/v1/hits/" + saved["original_hit_id"]).json()
        require(
            fingerprint(original) == saved["original_hit_fingerprint"]
            and original["status"] == "dispositioned",
            "restart_original_hit_and_completed_human_status",
        )
        connection = self.connection()
        require(
            connection["status"] == "disconnected"
            and not connection["access_token_configured"]
            and connection["cleanup_status"] in {"completed", "not_required"}
            and self.provider()["subscription_count"] == 0,
            "restart_disconnect_remote_cleanup_preserved",
        )
        records = self.call("GET", "/api/v1/audit-log?action=review.disposition&limit=1000").json()[
            "items"
        ]
        group = [row for row in records if row["id"] in saved["human_audit_ids"]]
        require(
            len(group) == len(saved["human_audit_ids"])
            and hashlib.sha256(json.dumps(group, sort_keys=True).encode()).hexdigest()
            == saved["human_audit_fingerprint"],
            "restart_original_human_audit_exact",
        )
        with readonly_database(Config()) as db:
            for table, ids in (
                ("operation_attempts", saved["operation_ids"]),
                ("screening_runs", saved["run_ids"]),
                ("dispositions", [saved["original_disposition_id"]]),
            ):
                for id in ids:
                    require(
                        db.execute(f"SELECT id FROM {table} WHERE id=?", (id,)).fetchone()
                        is not None,
                        "restart_all_saved_ids_persisted",
                    )
        schedules = self.call("GET", "/api/v1/monitoring/status").json()["schedules"]
        require(
            sorted(row["id"] for row in schedules) == saved["schedule_ids"],
            "restart_actual_schedule_ids_preserved",
        )
        return {
            "milestone": 7,
            "restart": "passed",
            "preserved_parties": len(saved["party_ids"]),
            "preserved_operations": len(saved["operation_ids"]),
            "preserved_runs": len(saved["run_ids"]),
            "original_evidence": "unchanged",
            "live_sandbox": "deferred",
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--serve-fixture", action="store_true")
    modes.add_argument("--run-fixture-gate", action="store_true")
    modes.add_argument("--verify-restart", action="store_true")
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args(argv)
    gate = None
    try:
        if args.serve_fixture:
            import uvicorn

            uvicorn.run(
                fixture_app(), host="0.0.0.0", port=8000, access_log=False, log_level="critical"
            )
            return
        gate = BCGate(args.base_url)
        wait_for_health(gate.http)
        result = gate.restart() if args.verify_restart else gate.run()
        print(json.dumps(result, sort_keys=True))
    except Exception as error:  # noqa: BLE001 -- never print private input/tracebacks
        print(
            json.dumps(
                {
                    "milestone": 7,
                    "failed_check": str(error)
                    if isinstance(error, GateFailure)
                    else type(error).__name__,
                    "last_call": gate.last_call if gate else None,
                }
            )
        )
        raise SystemExit(1) from None
    finally:
        if gate:
            gate.http.close()


if __name__ == "__main__":
    main()
