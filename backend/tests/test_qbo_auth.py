"""Synthetic verified-contract OAuth/webhook tests; never authenticates with Intuit."""

import base64
import hashlib
import hmac
import json
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import func, select, text

from app.connectors.qbo_common import load_source_credentials
from app.models import AuditLog, OperationAttempt, Party, Source, User
from app.models.oauth import OAuthState
from app.scheduler.leases import protected_operation
from app.security import digest

APP_ID = "synthetic-app-id"
APP_SECRET = "synthetic-private-client-secret"
VERIFIER = "synthetic-webhook-verifier"
TOKEN = {
    "token_type": "bearer",
    "access_token": "synthetic-access-token-value",
    "refresh_token": "synthetic-refresh-token-value",
    "expires_in": 3600,
    "x_refresh_token_expires_in": 8640000,
}


def write(client, admin, method, path, body):
    return getattr(client, method)(
        "/api/v1" + path, json=body, headers={"x-csrf-token": admin["csrf_token"]}
    )


@pytest.fixture
def qbo_setup(client, admin):
    assert (
        write(
            client,
            admin,
            "patch",
            "/settings/notifications",
            {"public_base_url": "https://screening.example.test"},
        ).status_code
        == 200
    )
    result = write(
        client,
        admin,
        "patch",
        "/settings/connectors/qbo",
        {
            "environment": "sandbox",
            "client_id": APP_ID,
            "client_secret": APP_SECRET,
            "verifier_token": VERIFIER,
        },
    )
    assert result.status_code == 200
    with client.app.state.session_factory() as db:
        source = Source(
            kind="qbo",
            label="Synthetic QuickBooks",
            config={
                "environment": "sandbox",
                "connection_generation": 0,
                "connection_status": "configured",
                "webhooks_enabled": False,
            },
        )
        db.add(source)
        db.commit()
        source_id = source.id
    calls = []

    def fixture(request):
        # Taking a writer lock proves OAuth HTTP is outside a writer transaction.
        with client.app.state.session_factory() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            db.rollback()
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json=TOKEN)

    client.app.state.qbo_transport = httpx.MockTransport(fixture)
    return source_id, calls


def start(client, admin, source_id):
    response = write(client, admin, "post", f"/sources/{source_id}/qbo/oauth/start", {})
    assert response.status_code == 200
    query = parse_qs(urlsplit(response.json()["authorization_url"]).query)
    return response.json(), query["state"][0]


def callback(client, state, realm="12345", **extra):
    return client.get(
        "/api/v1/connectors/qbo/oauth/callback",
        params={"state": state, "code": "synthetic-one-use-code", "realmId": realm, **extra},
        follow_redirects=False,
    )


def test_app_settings_encryption_preserve_clear_and_private_outputs(client, admin, qbo_setup):
    result = client.get("/api/v1/settings/connectors/qbo")
    payload = result.json()
    assert payload["sandbox"]["configured"] and not payload["production"]["configured"]
    assert payload["redirect_uri"].endswith("/api/v1/connectors/qbo/oauth/callback")
    assert payload["webhook_urls"]["sandbox"].endswith("/webhooks/qbo/sandbox")
    assert all(value not in result.text for value in (APP_ID, APP_SECRET, VERIFIER))
    with client.app.state.session_factory() as db:
        from app.models import Setting

        row = db.scalar(select(Setting).where(Setting.key == "qbo_app_sandbox"))
        assert row.encrypted and APP_SECRET not in row.value
        serialized = json.dumps([row.after for row in db.scalars(select(AuditLog))], default=str)
        assert all(value not in serialized for value in (APP_ID, APP_SECRET, VERIFIER))
    result = write(
        client,
        admin,
        "patch",
        "/settings/connectors/qbo",
        {"environment": "sandbox", "verifier_token": "new-synthetic-verifier"},
    )
    assert result.json()["sandbox"]["client_secret_configured"]
    result = write(
        client,
        admin,
        "patch",
        "/settings/connectors/qbo",
        {"environment": "sandbox", "clear_credentials": True},
    )
    assert not result.json()["sandbox"]["configured"]
    result = write(
        client,
        admin,
        "patch",
        "/settings/connectors/qbo",
        {"environment": "sandbox", "client_secret": "private-never-echo", "unknown": True},
    )
    assert result.status_code == 422 and "private-never-echo" not in result.text


def test_oauth_success_digest_frozen_redirect_encrypted_tokens_and_replay(client, admin, qbo_setup):
    source_id, calls = qbo_setup
    _, state = start(client, admin, source_id)
    with client.app.state.session_factory() as db:
        row = db.scalar(select(OAuthState))
        assert row.state_hash == digest(state) and row.state_hash != state
        assert (
            row.redirect_uri
            == "https://screening.example.test/api/v1/connectors/qbo/oauth/callback"
        )
    # Changing public origin does not alter the registered URI frozen into state.
    assert (
        write(
            client,
            admin,
            "patch",
            "/settings/notifications",
            {"public_base_url": "https://changed.example.test"},
        ).status_code
        == 200
    )
    response = callback(client, state)
    assert response.status_code == 303 and "connection=connected" in response.headers["location"]
    assert response.headers["referrer-policy"] == "no-referrer"
    assert all(
        value not in response.headers["location"]
        for value in (state, "synthetic-one-use-code", TOKEN["access_token"])
    )
    with client.app.state.session_factory() as db:
        source = db.get(Source, source_id)
        assert (
            source.config["realm_id"] == "12345"
            and source.config["connection_status"] == "connected"
        )
        assert TOKEN["access_token"] not in source.credentials_enc
        credentials = load_source_credentials(source, client.app.state.config)
        assert (
            credentials["refresh_token"] == TOKEN["refresh_token"]
            and credentials["source_generation"] == 1
        )
        assert db.scalar(select(OAuthState.consumed_at)) is not None
        attempt = db.scalar(
            select(OperationAttempt).where(OperationAttempt.operation == "source_sync")
        )
        assert attempt.status == "queued" and attempt.parameters["mode"] == "full"
        serialized = json.dumps([row.after for row in db.scalars(select(AuditLog))], default=str)
        assert all(
            value not in serialized
            for value in (
                state,
                TOKEN["access_token"],
                TOKEN["refresh_token"],
                "synthetic-one-use-code",
            )
        )
    assert "invalid_oauth_state" in callback(client, state).headers["location"]
    assert len(calls) == 1


@pytest.mark.parametrize(
    "reason",
    [
        "expired",
        "wrong_session",
        "app_changed",
        "source_changed",
        "deactivated",
        "provider_declined",
    ],
)
def test_oauth_failures_never_connect_or_exchange_unapproved_state(
    client, admin, qbo_setup, reason
):
    source_id, calls = qbo_setup
    _, state = start(client, admin, source_id)
    if reason == "expired":
        # Frozen state expiry cannot be rewritten: use the application clock instead.
        with client.app.state.session_factory() as db:
            expires = db.scalar(select(OAuthState.expires_at))
        from unittest.mock import patch

        with patch("app.connectors.qbo_auth.utcnow", return_value=expires + timedelta(seconds=1)):
            response = callback(client, state)
    elif reason == "wrong_session":
        client.cookies.clear()
        login = client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.com", "password": "test password with enough length"},
        )
        assert login.status_code == 200
        response = callback(client, state)
    elif reason == "app_changed":
        assert (
            write(
                client,
                admin,
                "patch",
                "/settings/connectors/qbo",
                {"environment": "sandbox", "client_secret": "replaced-synthetic-secret"},
            ).status_code
            == 200
        )
        response = callback(client, state)
    elif reason == "source_changed":
        with client.app.state.session_factory() as db:
            source = db.get(Source, source_id)
            source.config = {**source.config, "connection_generation": 1}
            db.commit()
        response = callback(client, state)
    elif reason == "deactivated":
        with client.app.state.session_factory() as db:
            db.get(User, admin["user"]["id"]).active = False
            db.commit()
        response = callback(client, state)
        assert response.status_code == 401
    else:
        response = callback(client, state, error="access_denied")
        assert "authorization_declined" in response.headers["location"]
    if reason != "deactivated":
        assert response.status_code == 303 and "connection_error=" in response.headers["location"]
    assert not calls
    with client.app.state.session_factory() as db:
        assert db.get(Source, source_id).credentials_enc is None


def test_app_patch_waits_for_live_source_lease_and_company_binding_cannot_change(
    client, admin, qbo_setup
):
    source_id, calls = qbo_setup
    _, state = start(client, admin, source_id)
    assert "connected" in callback(client, state).headers["location"]
    with protected_operation(client.app.state.session_factory, "source_sync", source_id=source_id):
        response = write(
            client,
            admin,
            "patch",
            "/settings/connectors/qbo",
            {"environment": "sandbox", "client_secret": "rotated-synthetic-secret"},
        )
        assert response.status_code == 409
    assert (
        write(
            client,
            admin,
            "patch",
            "/settings/connectors/qbo",
            {"environment": "sandbox", "client_secret": "rotated-synthetic-secret"},
        ).status_code
        == 200
    )
    assert (
        write(
            client,
            admin,
            "patch",
            "/settings/connectors/qbo",
            {"environment": "sandbox", "client_id": "different-app"},
        ).status_code
        == 409
    )
    _, state = start(client, admin, source_id)
    assert "company_binding_mismatch" in callback(client, state, realm="54321").headers["location"]
    assert len(calls) == 1


def sign(raw, verifier=VERIFIER):
    return base64.b64encode(hmac.new(verifier.encode(), raw, hashlib.sha256).digest()).decode()


def test_signed_webhook_multi_realm_duplicate_and_busy_followup_no_payload_application(
    client, admin, qbo_setup
):
    source_id, _ = qbo_setup
    with client.app.state.session_factory() as db:
        source = db.get(Source, source_id)
        source.config = {
            **source.config,
            "connection_status": "connected",
            "realm_id": "12345",
            "webhooks_enabled": True,
        }
        second = Source(
            kind="qbo",
            label="Second fixture",
            config={
                "environment": "sandbox",
                "connection_status": "connected",
                "realm_id": "67890",
                "webhooks_enabled": True,
            },
        )
        db.add(second)
        db.commit()
        second_id = second.id
    events = [
        {
            "specversion": "1.0",
            "source": "intuit.fixture",
            "id": "event-one",
            "type": "qbo.vendor.updated.v1",
            "intuitaccountid": "12345",
            "intuitentityid": "1",
            "data": {"DisplayName": "Do not apply"},
        },
        {
            "specversion": "1.0",
            "source": "intuit.fixture",
            "id": "event-two",
            "type": "qbo.customer.deleted.v1",
            "intuitaccountid": "67890",
            "intuitentityid": "2",
        },
        {
            "specversion": "1.0",
            "source": "intuit.fixture",
            "id": "event-foreign",
            "type": "qbo.customer.created.v1",
            "intuitaccountid": "99999",
        },
    ]
    raw = json.dumps(events).encode()
    with protected_operation(client.app.state.session_factory, "source_sync", source_id=source_id):
        response = client.post(
            "/api/v1/webhooks/qbo/sandbox", content=raw, headers={"intuit-signature": sign(raw)}
        )
        assert response.status_code == 200
        response = client.post(
            "/api/v1/webhooks/qbo/sandbox", content=raw, headers={"intuit-signature": sign(raw)}
        )
        assert response.status_code == 200
        with client.app.state.session_factory() as db:
            queued = list(
                db.scalars(select(OperationAttempt).where(OperationAttempt.status == "queued"))
            )
            assert sorted(row.source_id for row in queued) == sorted([source_id, second_id])
            assert db.scalar(select(func.count()).select_from(Party)) == 0
            receipts = list(
                db.scalars(select(AuditLog).where(AuditLog.action == "qbo.webhook_received"))
            )
            assert len(receipts) == 2 and all(
                "DisplayName" not in json.dumps(row.after) for row in receipts
            )
    assert (
        client.post(
            "/api/v1/webhooks/qbo/sandbox",
            content=raw + b" ",
            headers={"intuit-signature": sign(raw)},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/webhooks/qbo/sandbox",
            content=b"invalid",
            headers={"intuit-signature": sign(b"invalid")},
        ).status_code
        == 400
    )


def test_disconnect_stops_polling_preserves_records_and_exposes_revoke_failure(
    client, admin, qbo_setup
):
    from app.connectors.qbo_auth import disconnect_source

    source_id, _ = qbo_setup
    _, state = start(client, admin, source_id)
    callback(client, state)
    response = write(client, admin, "post", f"/sources/{source_id}/qbo/disconnect", {})
    assert (
        response.status_code == 202 and response.json()["connection"]["status"] == "disconnecting"
    )

    def rejected(request):
        return httpx.Response(503, json={"unexpected": "private-provider-body"})

    factory = client.app.state.session_factory
    with protected_operation(factory, "source_disconnect", source_id=source_id) as execution:
        from app.scheduler.leases import operation_session

        with operation_session(factory, execution) as db:
            result = disconnect_source(
                db,
                client.app.state.config,
                db.get(Source, source_id),
                transport=httpx.MockTransport(rejected),
            )
    assert result["revocation_status"] == "failed"
    with factory() as db:
        source = db.get(Source, source_id)
        assert source.config["connection_status"] == "disconnected" and source.credentials_enc
        assert source.config["realm_id"] == "12345"
    payload = client.get(f"/api/v1/sources/{source_id}/qbo/connection").json()
    assert payload["revocation_status"] == "failed" and "private-provider-body" not in json.dumps(
        payload
    )


def test_qbo_admin_roles_csrf_and_frozen_oauth_state(client, admin, qbo_setup):
    from sqlalchemy.exc import IntegrityError

    from app.audit import ImmutableAuditError

    source_id, _ = qbo_setup
    _, state = start(client, admin, source_id)
    assert client.post(f"/api/v1/sources/{source_id}/qbo/oauth/start", json={}).status_code == 403
    with client.app.state.session_factory() as db:
        row = db.scalar(select(OAuthState))
        row.redirect_uri = "https://different.example.test/callback"
        with pytest.raises(ImmutableAuditError):
            db.commit()
        db.rollback()
        with pytest.raises(IntegrityError):
            db.execute(text("DELETE FROM oauth_states"))
        db.rollback()
        db.get(User, admin["user"]["id"]).role = "reviewer"
        db.commit()
    assert client.get("/api/v1/settings/connectors/qbo").status_code == 403
    assert client.get(f"/api/v1/sources/{source_id}/qbo/connection").status_code == 403
    assert (
        write(client, admin, "post", f"/sources/{source_id}/qbo/disconnect", {}).status_code == 403
    )
    assert callback(client, state).status_code == 403


def test_oauth_rechecks_admin_after_provider_response_without_connecting(client, admin, qbo_setup):
    source_id, _ = qbo_setup
    _, state = start(client, admin, source_id)

    def offboard(request):
        with client.app.state.session_factory() as db:
            db.get(User, admin["user"]["id"]).role = "reviewer"
            db.commit()
        return httpx.Response(200, json=TOKEN)

    client.app.state.qbo_transport = httpx.MockTransport(offboard)
    response = callback(client, state)
    assert response.status_code == 303 and "admin_session_required" in response.headers["location"]
    with client.app.state.session_factory() as db:
        assert db.get(Source, source_id).credentials_enc is None
        assert db.get(Source, source_id).config["connection_status"] == "configured"


def test_provider_failure_never_reflects_private_body_and_logout_keeps_state(
    client, admin, qbo_setup
):
    source_id, _ = qbo_setup
    _, state = start(client, admin, source_id)
    client.app.state.qbo_transport = httpx.MockTransport(
        lambda request: httpx.Response(
            400,
            json={"error": "invalid_grant", "error_description": "private-provider-secret-value"},
        )
    )
    response = callback(client, state)
    assert "oauth_failed" in response.headers["location"]
    assert "private-provider-secret-value" not in response.text + response.headers["location"]
    _, state = start(client, admin, source_id)
    assert write(client, admin, "post", "/auth/logout", {}).status_code == 204
    assert callback(client, state).status_code == 401
    with client.app.state.session_factory() as db:
        assert len(list(db.scalars(select(OAuthState)))) == 2
        assert db.get(Source, source_id).credentials_enc is None


def test_cloud_event_identity_includes_source_and_realm(client, admin, qbo_setup):
    source_id, _ = qbo_setup
    with client.app.state.session_factory() as db:
        source = db.get(Source, source_id)
        source.config = {
            **source.config,
            "realm_id": "12345",
            "connection_status": "connected",
            "webhooks_enabled": True,
        }
        db.commit()
    events = [
        {
            "specversion": "1.0",
            "source": source,
            "id": "same-event-id",
            "type": "qbo.customer.updated.v1",
            "intuitaccountid": "12345",
        }
        for source in ("intuit.first", "intuit.second")
    ]
    raw = json.dumps(events).encode()
    assert (
        client.post(
            "/api/v1/webhooks/qbo/sandbox", content=raw, headers={"intuit-signature": sign(raw)}
        ).status_code
        == 200
    )
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "qbo.webhook_received")
            )
            == 2
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(OperationAttempt)
                .where(OperationAttempt.status == "queued")
            )
            == 1
        )


def test_connected_app_rotation_preserves_tokens_and_polls_but_invalidates_pending_state(
    client, admin, qbo_setup
):
    from app.connectors.qbo import sync_qbo_source
    from app.connectors.qbo_common import load_app_config
    from app.scheduler.leases import operation_session

    source_id, calls = qbo_setup
    _, state = start(client, admin, source_id)
    callback(client, state)
    _, pending = start(client, admin, source_id)
    factory, config = client.app.state.session_factory, client.app.state.config
    with factory() as db:
        original = load_source_credentials(db.get(Source, source_id), config)
    for body in (
        {"verifier_token": "rotated-verifier"},
        {"client_secret": "rotated-client-secret"},
    ):
        assert (
            write(
                client,
                admin,
                "patch",
                "/settings/connectors/qbo",
                {"environment": "sandbox", **body},
            ).status_code
            == 200
        )
        with factory() as db:
            current = load_source_credentials(db.get(Source, source_id), config)
            assert current["app_generation"] == load_app_config(db, config, "sandbox").generation
            assert {key: value for key, value in current.items() if key != "app_generation"} == {
                key: value for key, value in original.items() if key != "app_generation"
            }

        def empty_query(request):
            entity = "Customer" if "Customer" in request.url.params["query"] else "Vendor"
            return httpx.Response(200, json={"QueryResponse": {entity: []}})

        with protected_operation(factory, "source_sync", source_id=source_id) as execution:
            with operation_session(factory, execution) as db:
                result = sync_qbo_source(
                    db,
                    config,
                    db.get(Source, source_id),
                    mode="full",
                    transport=httpx.MockTransport(empty_query),
                )
        assert result["added"] == result["changed"] == 0
    assert "oauth_configuration_changed" in callback(client, pending).headers["location"]
    assert len(calls) == 1


def test_failed_rejected_grant_cleanup_is_encrypted_system_retry_without_connect(
    client, admin, qbo_setup
):
    from app.connectors.qbo_auth import disconnect_source
    from app.scheduler.leases import operation_session

    source_id, _ = qbo_setup
    _, state = start(client, admin, source_id)
    calls = []

    def reject_after_exchange(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/bearer"):
            with client.app.state.session_factory() as db:
                db.get(User, admin["user"]["id"]).role = "reviewer"
                db.commit()
            return httpx.Response(200, json=TOKEN)
        return httpx.Response(503, json={"private": "must-not-appear"})

    client.app.state.qbo_transport = httpx.MockTransport(reject_after_exchange)
    response = callback(client, state)
    assert "admin_session_required" in response.headers["location"]
    factory, config = client.app.state.session_factory, client.app.state.config
    with factory() as db:
        source = db.get(Source, source_id)
        assert source.config["connection_status"] == "configured" and source.cursor is None
        values = load_source_credentials(source, config)
        assert "access_token" not in values and len(values["pending_revocations"]) == 1
        assert values["pending_revocations"][0]["token"] == TOKEN["refresh_token"]
        assert (
            TOKEN["refresh_token"] not in source.credentials_enc
            and APP_SECRET not in source.credentials_enc
        )
        attempt = db.scalar(
            select(OperationAttempt).where(
                OperationAttempt.operation == "source_disconnect",
                OperationAttempt.status == "queued",
            )
        )
        assert attempt.actor_user_id is None
        event = db.scalar(select(AuditLog).where(AuditLog.action == "qbo.rejected_grant_cleanup"))
        assert event.actor_user_id is None and event.after["revocation_status"] == "failed"
        assert all(
            value not in json.dumps(event.after) for value in (APP_SECRET, TOKEN["refresh_token"])
        )
        attempt_id = attempt.id
    assert len(calls) == 2
    with protected_operation(
        factory, "source_disconnect", source_id=source_id, attempt_id=attempt_id
    ) as execution:
        with operation_session(factory, execution) as db:
            result = disconnect_source(
                db,
                config,
                db.get(Source, source_id),
                transport=httpx.MockTransport(lambda request: httpx.Response(200)),
            )
    assert result["revocation_status"] == "revoked" and result["connection_status"] == "configured"
    with factory() as db:
        source = db.get(Source, source_id)
        assert source.credentials_enc is None and source.config["connection_status"] == "configured"
        assert source.config["connection_generation"] == 0 and source.cursor is None
        assert db.scalar(select(func.count()).select_from(Party)) == 0


def test_disconnect_revokes_exact_snapshot_and_never_clears_newer_credentials(
    client, admin, qbo_setup
):
    from app.connectors.qbo_auth import QBOAuthorizationError, disconnect_source
    from app.connectors.qbo_common import save_source_credentials
    from app.scheduler.leases import operation_session

    source_id, _ = qbo_setup
    _, state = start(client, admin, source_id)
    callback(client, state)
    write(client, admin, "post", f"/sources/{source_id}/qbo/disconnect", {})
    factory, config = client.app.state.session_factory, client.app.state.config

    def changed_during_revoke(request):
        with factory() as db:
            source = db.get(Source, source_id)
            values = load_source_credentials(source, config)
            save_source_credentials(
                db, source, config, {**values, "refresh_token": "newer-synthetic-refresh-token"}
            )
            db.commit()
        return httpx.Response(200)

    with pytest.raises(QBOAuthorizationError, match="connection_changed_during_revocation"):
        with protected_operation(factory, "source_disconnect", source_id=source_id) as execution:
            with operation_session(factory, execution) as db:
                disconnect_source(
                    db,
                    config,
                    db.get(Source, source_id),
                    transport=httpx.MockTransport(changed_during_revoke),
                )
    with factory() as db:
        source = db.get(Source, source_id)
        assert (
            load_source_credentials(source, config)["refresh_token"]
            == "newer-synthetic-refresh-token"
        )
        assert source.config["revocation_status"] == "pending"


def test_cancelled_inflight_callback_queues_latest_grant_revocation(client, admin, qbo_setup):
    source_id, _ = qbo_setup
    _, state = start(client, admin, source_id)

    def cancelled_exchange(request):
        response = write(client, admin, "post", f"/sources/{source_id}/qbo/disconnect", {})
        assert response.status_code == 202
        return httpx.Response(200, json=TOKEN)

    client.app.state.qbo_transport = httpx.MockTransport(cancelled_exchange)
    response = callback(client, state)
    assert "authorization_cancelled" in response.headers["location"]
    with client.app.state.session_factory() as db:
        source = db.get(Source, source_id)
        assert source.config["connection_status"] == "disconnecting"
        assert (
            load_source_credentials(source, client.app.state.config)["refresh_token"]
            == TOKEN["refresh_token"]
        )
        assert db.scalar(
            select(OperationAttempt.id).where(
                OperationAttempt.operation == "source_disconnect",
                OperationAttempt.status == "queued",
            )
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(OperationAttempt)
                .where(OperationAttempt.operation == "source_sync")
            )
            == 0
        )


def test_rejected_grant_cleanup_preserves_valid_connection_cursor_and_tokens(
    client, admin, qbo_setup
):
    from app.connectors.qbo_auth import cleanup_rejected_grant, disconnect_source
    from app.connectors.qbo_common import load_app_config
    from app.scheduler.leases import operation_session

    source_id, _ = qbo_setup
    _, state = start(client, admin, source_id)
    callback(client, state)
    factory, config = client.app.state.session_factory, client.app.state.config
    with factory() as db:
        source = db.get(Source, source_id)
        source.cursor = "2026-10-14T00:00:00+00:00"
        db.commit()
        original = load_source_credentials(source, config)
    with protected_operation(factory, "qbo_oauth_callback", source_id=source_id) as execution:
        with operation_session(factory, execution) as db:
            app = load_app_config(db, config, "sandbox")
            cleanup_rejected_grant(
                db,
                config,
                source_id,
                app,
                {**TOKEN, "refresh_token": "rejected-separate-grant"},
                transport=httpx.MockTransport(lambda request: httpx.Response(503)),
            )
    with factory() as db:
        source = db.get(Source, source_id)
        assert source.config["connection_status"] == "connected"
        current = load_source_credentials(source, config)
        assert {
            key: value for key, value in current.items() if key != "pending_revocations"
        } == original
        assert len(current["pending_revocations"]) == 1
    with protected_operation(factory, "source_disconnect", source_id=source_id) as execution:
        with operation_session(factory, execution) as db:
            result = disconnect_source(
                db,
                config,
                db.get(Source, source_id),
                transport=httpx.MockTransport(lambda request: httpx.Response(200)),
            )
    assert result["connection_status"] == "connected" and result["revocation_status"] == "revoked"
    with factory() as db:
        source = db.get(Source, source_id)
        assert load_source_credentials(source, config) == original
        assert (
            source.cursor == "2026-10-14T00:00:00+00:00"
            and source.config["connection_generation"] == 1
        )


def test_previous_disconnect_cancels_pending_flow_before_new_exchange(client, admin, qbo_setup):
    from app.connectors.qbo_auth import disconnect_source
    from app.scheduler.leases import operation_session

    source_id, calls = qbo_setup
    _, state = start(client, admin, source_id)
    callback(client, state)
    _, pending_state = start(client, admin, source_id)
    write(client, admin, "post", f"/sources/{source_id}/qbo/disconnect", {})
    factory = client.app.state.session_factory
    with protected_operation(factory, "source_disconnect", source_id=source_id) as execution:
        with operation_session(factory, execution) as db:
            result = disconnect_source(
                db,
                client.app.state.config,
                db.get(Source, source_id),
                transport=httpx.MockTransport(lambda request: httpx.Response(503)),
            )
    assert result["revocation_status"] == "failed"
    assert "authorization_cancelled" in callback(client, pending_state).headers["location"]
    assert len(calls) == 1


def test_duplicate_company_rejected_before_exchange_even_legacy_default_environment(
    client, admin, qbo_setup
):
    source_id, calls = qbo_setup
    with client.app.state.session_factory() as db:
        duplicate = Source(kind="qbo", label="Existing realm", config={"realm_id": "12345"})
        db.add(duplicate)
        db.commit()
    _, state = start(client, admin, source_id)
    assert "company_already_connected" in callback(client, state).headers["location"]
    assert not calls


def test_never_connected_disconnect_is_truthfully_not_required(client, admin, qbo_setup):
    from app.connectors.qbo_auth import disconnect_source
    from app.scheduler.leases import operation_session

    source_id, calls = qbo_setup
    write(client, admin, "post", f"/sources/{source_id}/qbo/disconnect", {})
    factory = client.app.state.session_factory
    with protected_operation(factory, "source_disconnect", source_id=source_id) as execution:
        with operation_session(factory, execution) as db:
            result = disconnect_source(
                db,
                client.app.state.config,
                db.get(Source, source_id),
                transport=client.app.state.qbo_transport,
            )
    assert (
        result["revocation_status"] == "not_required"
        and result["connection_status"] == "disconnected"
    )
    assert not calls


@pytest.mark.parametrize("revoke_status", [200, 503])
def test_reconnect_replaces_prior_grant_revocation_flags(client, admin, qbo_setup, revoke_status):
    from app.connectors.qbo_common import REVOKE_URL, TOKEN_URL
    from app.scheduler.service import dispatch_attempt

    source_id, _ = qbo_setup
    factory, config = client.app.state.session_factory, client.app.state.config
    fresh_token = {
        **TOKEN,
        "access_token": "synthetic-reconnected-access-token",
        "refresh_token": "synthetic-reconnected-refresh-token",
    }
    exchanged = 0
    revoked = []

    def vendor(request):
        nonlocal exchanged
        with factory() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            db.rollback()
        if str(request.url) == REVOKE_URL:
            revoked.append(json.loads(request.content)["token"])
            return httpx.Response(revoke_status, json={})
        assert str(request.url) == TOKEN_URL
        exchanged += 1
        return httpx.Response(200, json=TOKEN if exchanged == 1 else fresh_token)

    client.app.state.qbo_transport = httpx.MockTransport(vendor)
    _, initial_state = start(client, admin, source_id)
    initial = callback(client, initial_state)
    assert initial.status_code == 303 and "connection=connected" in initial.headers["location"]
    disconnect = write(client, admin, "post", f"/sources/{source_id}/qbo/disconnect", {})
    assert disconnect.status_code == 202
    result = dispatch_attempt(factory, config, disconnect.json()["attempt_id"])
    prior_status = "revoked" if revoke_status == 200 else "failed"
    assert result["status"] == ("success" if revoke_status == 200 else "failed")
    previous = client.get(f"/api/v1/sources/{source_id}/qbo/connection").json()
    assert previous["status"] == "disconnected" and previous["revocation_status"] == prior_status
    assert revoked == [TOKEN["refresh_token"]]

    _, new_state = start(client, admin, source_id)
    accepted = callback(client, new_state)
    assert accepted.status_code == 303 and "connection=connected" in accepted.headers["location"]
    current = client.get(f"/api/v1/sources/{source_id}/qbo/connection").json()
    assert current["status"] == "connected" and current["realm_id"] == "12345"
    assert current["revocation_status"] is None and current["revocation_error_code"] is None
    with factory() as db:
        source = db.get(Source, source_id)
        assert "disconnect_requested_at" not in source.config
        credentials = load_source_credentials(source, config)
        assert credentials["access_token"] == fresh_token["access_token"]
        assert credentials["refresh_token"] == fresh_token["refresh_token"]
        assert not credentials.get("pending_revocations")
        history = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "qbo.disconnected", AuditLog.target_id == source_id
            )
        )
        assert history.after["revocation_status"] == prior_status
    assert exchanged == 2
