"""Synthetic BC API/security contracts; no live Entra or ERP requests."""

import json
from collections import Counter
from copy import deepcopy
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import select, text

from app.connectors.bc_common import (
    load_app_config,
    load_source_credentials,
    save_source_credentials,
    utc_time,
)
from app.models import AuditLog, OperationAttempt, Setting, Source, User, utcnow
from app.scheduler.service import dispatch_attempt

APP_ID = "11111111-1111-4111-8111-111111111111"
TENANT = "22222222-2222-4222-8222-222222222222"
COMPANY = "33333333-3333-4333-8333-333333333333"
OTHER_COMPANY = "44444444-4444-4444-8444-444444444444"
RECORD = "55555555-5555-4555-8555-555555555555"
SECRET = " synthetic-client-secret+&value "
ENVIRONMENT = "Sandbox_1"


def write(client, admin, method, path, body=None):
    return getattr(client, method)(
        "/api/v1" + path,
        json={} if body is None else body,
        headers={"x-csrf-token": admin["csrf_token"]},
    )


class Vendor:
    def __init__(self, client):
        self.client = client
        self.calls = Counter()
        self.rows = {}
        self.hidden = False
        self.hidden_entities = set()
        self.no_etag_entities = set()
        self.reject_create = {}
        self.creations = Counter()
        self.no_etag = False
        self.timeout_create = False
        self.conflict_renew = False
        self.reject_delete = False
        self.challenge_count = 0
        self.on_company = None
        self.on_post = None
        self.source_id = None

    def request(self, request):
        # Every vendor request proves another connection can take the writer.
        with self.client.app.state.session_factory() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            db.rollback()
        assert request.url.scheme == "https"
        assert request.url.host in {"login.microsoftonline.com", "api.businesscentral.dynamics.com"}
        path = request.url.path
        self.calls[(request.method, path.rsplit("/", 1)[-1].split("(")[0])] += 1
        if request.url.host == "login.microsoftonline.com":
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["client_credentials"]
            assert form["client_id"] == [APP_ID] and form["client_secret"] == [SECRET]
            return httpx.Response(
                200,
                json={
                    "token_type": "Bearer",
                    "access_token": "synthetic-bc-access",
                    "expires_in": 3600,
                },
            )
        assert path.startswith(f"/v2.0/{TENANT}/{ENVIRONMENT}/api/v2.0/")
        if path.endswith("/companies"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": COMPANY,
                            "displayName": "Synthetic Company",
                            "privateDetail": "omit-this-detail",
                        },
                        {"id": OTHER_COMPANY, "displayName": "Other Synthetic Company"},
                    ]
                },
            )
        if "/companies(" in path:
            if self.on_company:
                self.on_company()
            selected = path.split("/companies(", 1)[1].split(")", 1)[0]
            return httpx.Response(200, json={"id": selected, "displayName": "Synthetic Company"})
        assert "/subscriptions" in path
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "value": []
                    if self.hidden
                    else deepcopy(
                        [
                            row
                            for row in self.rows.values()
                            if row["resource"].rsplit("/", 1)[-1] not in self.hidden_entities
                        ]
                    )
                },
            )
        if request.method == "POST":
            body = json.loads(request.content)
            entity = body["resource"].rsplit("/", 1)[-1]
            self.creations[entity] += 1
            if entity in self.reject_create:
                return httpx.Response(
                    self.reject_create[entity],
                    json={"error": {"message": "private rejected detail"}},
                )
            challenge = self.client.post(
                urlsplit(body["notificationUrl"]).path,
                params={"validationToken": "synthetic challenge + decoded"},
                headers={"cookie": ""},
                content=b"",
            )
            assert (
                challenge.status_code == 200 and challenge.text == "synthetic challenge + decoded"
            )
            assert challenge.headers["content-type"].startswith("text/plain")
            self.challenge_count += 1
            entity = body["resource"].rsplit("/", 1)[-1]
            key = f"opaque'{entity}-{self.calls[('POST', 'subscriptions')]}"
            row = {
                **body,
                "subscriptionId": key,
                "expirationDateTime": (utcnow() + timedelta(days=3)).isoformat(),
            }
            if not self.no_etag and entity not in self.no_etag_entities:
                row["@odata.etag"] = 'W/"fixture-1"'
            self.rows[key] = row
            if self.on_post:
                self.on_post()
            if self.timeout_create:
                self.timeout_create = False
                raise httpx.ReadTimeout("synthetic private timeout detail", request=request)
            return httpx.Response(201, json=deepcopy(row))
        key = path.split("/subscriptions(", 1)[1][1:-2].replace("''", "'")
        row = self.rows.get(key)
        if row is None:
            return httpx.Response(404, json={"error": {"message": "private missing detail"}})
        if request.method == "PATCH":
            if self.conflict_renew:
                self.conflict_renew = False
                row["@odata.etag"] = 'W/"fixture-2"'
                return httpx.Response(412, json={"error": {"message": "private conflict detail"}})
            assert request.headers["if-match"] == row["@odata.etag"]
            body = json.loads(request.content)
            challenge = self.client.post(
                urlsplit(body["notificationUrl"]).path,
                params={"validationToken": "renewal challenge"},
                headers={"cookie": ""},
            )
            assert challenge.status_code == 200 and challenge.text == "renewal challenge"
            self.challenge_count += 1
            row.update(body)
            row["expirationDateTime"] = (utcnow() + timedelta(days=3)).isoformat()
            row["@odata.etag"] = 'W/"fixture-renewed"'
            return httpx.Response(200, json=deepcopy(row))
        assert request.method == "DELETE"
        if self.reject_delete:
            return httpx.Response(503, json={"error": {"message": "private cleanup detail"}})
        assert request.headers["if-match"] == row["@odata.etag"]
        del self.rows[key]
        return httpx.Response(204)


@pytest.fixture
def bc_setup(client, admin):
    assert (
        write(
            client,
            admin,
            "patch",
            "/settings/notifications",
            {
                "public_base_url": "https://screening.example.test",
            },
        ).status_code
        == 200
    )
    assert (
        write(
            client,
            admin,
            "patch",
            "/settings/connectors/bc",
            {
                "client_id": APP_ID,
                "client_secret": SECRET,
            },
        ).status_code
        == 200
    )
    created = write(
        client,
        admin,
        "post",
        "/sources",
        {
            "kind": "bc",
            "label": "Synthetic BC",
            "tenant_id": TENANT,
            "bc_environment": ENVIRONMENT,
        },
    )
    assert created.status_code == 201
    source_id = created.json()["id"]
    vendor = Vendor(client)
    vendor.source_id = source_id
    client.app.state.bc_transport = httpx.MockTransport(vendor.request)
    return source_id, vendor


def connect(client, admin, source_id):
    response = write(
        client,
        admin,
        "post",
        f"/sources/{source_id}/bc/connect",
        {
            "company_id": COMPANY,
        },
    )
    assert response.status_code == 202
    return response.json()


def subscriptions(client, admin, source_id):
    toggle = write(
        client,
        admin,
        "patch",
        f"/sources/{source_id}/bc/connection",
        {
            "webhooks_enabled": True,
        },
    )
    assert toggle.status_code == 202
    return dispatch_attempt(
        client.app.state.session_factory,
        client.app.state.config,
        toggle.json()["attempt_id"],
    )


def retry(client, admin, source_id):
    queued = write(client, admin, "post", f"/sources/{source_id}/bc/subscriptions/reconcile")
    assert queued.status_code == 202
    return dispatch_attempt(
        client.app.state.session_factory,
        client.app.state.config,
        queued.json()["attempt_id"],
    )


def test_global_settings_encrypted_flags_preservation_and_secret_spaces(client, admin, bc_setup):
    source_id, vendor = bc_setup
    public = client.get("/api/v1/settings/connectors/bc")
    assert public.json()["configured"] and SECRET not in public.text and APP_ID not in public.text
    assert public.json()["webhook_base_url"].endswith("/webhooks/bc")
    result = write(client, admin, "post", f"/sources/{source_id}/bc/companies")
    assert result.status_code == 200
    assert result.json() == {
        "items": [
            {"id": COMPANY, "display_name": "Synthetic Company"},
            {"id": OTHER_COMPANY, "display_name": "Other Synthetic Company"},
        ]
    }
    assert "omit-this-detail" not in result.text
    with client.app.state.session_factory() as db:
        row = db.scalar(select(Setting).where(Setting.key == "bc_app"))
        assert row.encrypted and SECRET not in row.value
        assert load_app_config(db, client.app.state.config).client_secret == SECRET
        audit_json = json.dumps([row.after for row in db.scalars(select(AuditLog))], default=str)
        assert SECRET not in audit_json and "synthetic-bc-access" not in audit_json
    assert vendor.calls[("POST", "token")] == 1


def test_cached_access_token_is_aware_and_reused(client, admin, bc_setup):
    source_id, vendor = bc_setup
    for _ in range(2):
        assert write(client, admin, "post", f"/sources/{source_id}/bc/companies").status_code == 200
    assert vendor.calls[("POST", "token")] == 1
    assert utc_time("2026-01-01T00:00:00+02:00").tzinfo is not None


def test_connect_binds_company_and_queues_full_without_returning_secrets(client, admin, bc_setup):
    source_id, _ = bc_setup
    result = connect(client, admin, source_id)
    assert result["connection"]["company_id"] == COMPANY
    assert result["connection"]["status"] == "connected"
    assert SECRET not in json.dumps(result) and "synthetic-bc-access" not in json.dumps(result)
    with client.app.state.session_factory() as db:
        attempt = db.get(OperationAttempt, result["attempt_id"])
        assert attempt.operation == "source_sync" and attempt.parameters["mode"] == "full"
        assert db.get(Source, source_id).cursor is None
    response = write(
        client, admin, "post", f"/sources/{source_id}/bc/connect", {"company_id": OTHER_COMPANY}
    )
    assert response.status_code == 409
    assert (
        write(
            client,
            admin,
            "patch",
            "/settings/connectors/bc",
            {
                "clear_credentials": True,
            },
        ).status_code
        == 409
    )


def test_secret_rotation_rebinds_existing_token_generation(client, admin, bc_setup):
    source_id, _ = bc_setup
    connect(client, admin, source_id)
    factory, config = client.app.state.session_factory, client.app.state.config
    with factory() as db:
        previous = load_source_credentials(db.get(Source, source_id), config)
    assert (
        write(
            client,
            admin,
            "patch",
            "/settings/connectors/bc",
            {
                "client_secret": "new synthetic secret",
            },
        ).status_code
        == 200
    )
    with factory() as db:
        current = load_source_credentials(db.get(Source, source_id), config)
        assert current["app_generation"] == previous["app_generation"] + 1
        assert current["access_token"] == previous["access_token"]
        assert current["source_generation"] == previous["source_generation"]


@pytest.mark.parametrize(
    "invalid",
    [
        {"client_id": "not-a-uuid"},
        {"client_secret": "secret\ncontrol"},
        {"client_secret": "secret", "clear_credentials": True},
        {"client_secret": "must-not-echo", "unknown": True},
    ],
)
def test_settings_validation_has_no_input_echo(client, admin, bc_setup, invalid):
    response = write(client, admin, "patch", "/settings/connectors/bc", invalid)
    assert response.status_code == 422
    assert "must-not-echo" not in response.text and "secret\ncontrol" not in response.text


def test_late_admin_revocation_cannot_activate_company(client, admin, bc_setup):
    source_id, vendor = bc_setup

    def offboard():
        with client.app.state.session_factory() as db:
            db.get(User, admin["user"]["id"]).role = "reviewer"
            db.commit()

    vendor.on_company = offboard
    response = write(
        client, admin, "post", f"/sources/{source_id}/bc/connect", {"company_id": COMPANY}
    )
    assert response.status_code == 403
    with client.app.state.session_factory() as db:
        source = db.get(Source, source_id)
        assert (
            not source.config.get("company_id")
            and source.config["connection_status"] == "configured"
        )
        assert source.cursor is None


def test_bc_admin_csrf_and_reviewer_controls(client, admin, bc_setup):
    source_id, _ = bc_setup
    assert client.post(f"/api/v1/sources/{source_id}/bc/companies", json={}).status_code == 403
    with client.app.state.session_factory() as db:
        db.get(User, admin["user"]["id"]).role = "reviewer"
        db.commit()
    assert client.get("/api/v1/settings/connectors/bc").status_code == 403
    assert client.get(f"/api/v1/sources/{source_id}/bc/connection").status_code == 403
    assert write(client, admin, "post", f"/sources/{source_id}/bc/disconnect").status_code == 403


@pytest.mark.parametrize("operation", ["companies", "connect"])
def test_explicit_admin_operation_reacquires_rejected_unexpired_token(
    client, admin, bc_setup, operation
):
    source_id, vendor = bc_setup
    connect(client, admin, source_id)
    with client.app.state.session_factory() as db:
        source = db.get(Source, source_id)
        values = load_source_credentials(source, client.app.state.config)
        values["access_token"] = "synthetic-rejected-unexpired"
        save_source_credentials(db, source, client.app.state.config, values)
        db.commit()
    rejected = []

    def transport(request):
        if request.headers.get("authorization") == "Bearer synthetic-rejected-unexpired":
            rejected.append(request.url.path)
            return httpx.Response(401, json={"error": {"message": "private unauthorized detail"}})
        return vendor.request(request)

    client.app.state.bc_transport = httpx.MockTransport(transport)
    response = write(
        client,
        admin,
        "post",
        f"/sources/{source_id}/bc/{operation}",
        {"company_id": COMPANY} if operation == "connect" else {},
    )
    assert response.status_code == (202 if operation == "connect" else 200)
    assert len(rejected) == 1 and vendor.calls[("POST", "token")] == 2
    with client.app.state.session_factory() as db:
        assert (
            load_source_credentials(db.get(Source, source_id), client.app.state.config)[
                "access_token"
            ]
            == "synthetic-bc-access"
        )


def test_anonymous_handshake_uses_thread_pool_for_source_lookup(
    client, admin, bc_setup, monkeypatch
):
    import app.api.bc as api

    source_id, _ = bc_setup
    original = api.run_in_threadpool
    lookups = []

    async def dispatched(function, *args, **kwargs):
        if function is api.source_row:
            lookups.append(True)
        return await original(function, *args, **kwargs)

    monkeypatch.setattr(api, "run_in_threadpool", dispatched)
    response = client.post(
        f"/api/v1/webhooks/bc/{source_id}",
        params={"validationToken": "threaded challenge"},
        headers={"cookie": ""},
    )
    assert response.status_code == 200 and response.text == "threaded challenge"
    assert lookups == [True]


def test_explicit_activation_retries_unauthorized_only_once(client, admin, bc_setup):
    source_id, vendor = bc_setup

    def transport(request):
        if "/companies(" in request.url.path:
            return httpx.Response(401, json={"error": {"message": "private permission detail"}})
        return vendor.request(request)

    client.app.state.bc_transport = httpx.MockTransport(transport)
    response = write(
        client, admin, "post", f"/sources/{source_id}/bc/connect", {"company_id": COMPANY}
    )
    assert response.status_code == 422 and "private permission detail" not in response.text
    assert vendor.calls[("POST", "token")] == 2
    with client.app.state.session_factory() as db:
        assert not db.get(Source, source_id).config.get("company_id")
