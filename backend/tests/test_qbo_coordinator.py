"""Actual coordinator leases/indexes/schedules; synthetic transports only."""

import base64
import hashlib
import hmac
import json
import os
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from test_qbo import APP, FIXTURES, NOW, fixture, query
from test_qbo import connected as connected

from app.audit import ImmutableAuditError, audit
from app.connectors.qbo import QBOError, sync_qbo_source
from app.connectors.qbo_auth import QBOAuthorizationError, active_session, webhook_signals
from app.connectors.qbo_common import (
    AppConfig,
    load_source_credentials,
    save_app_config,
    save_source_credentials,
)
from app.csl.service import refresh_csl
from app.models import (
    AuditLog,
    OAuthState,
    OperationAttempt,
    OperationLease,
    Party,
    ScreeningRun,
    Source,
    UserSession,
    utcnow,
)
from app.scheduler import service
from app.scheduler.leases import lease_key, operation_session, protected_operation
from app.scheduler.runtime import SchedulerRuntime
from app.security import digest


@pytest.fixture
def oauth_state(client, admin, connected):
    with client.app.state.session_factory() as db:
        session_id = db.scalar(
            select(UserSession.id).where(UserSession.user_id == admin["user"]["id"])
        )
        state = OAuthState(
            state_hash=digest("public-synthetic-coordinator-state"),
            source_id=connected,
            user_id=admin["user"]["id"],
            session_id=session_id,
            app_generation=1,
            source_generation=1,
            redirect_uri="https://fixture.example.test/api/v1/connectors/qbo/oauth/callback",
            expires_at=utcnow() + timedelta(minutes=10),
        )
        db.add(state)
        db.commit()
        return state.id


@pytest.mark.parametrize(
    "field",
    [
        "state_hash",
        "redirect_uri",
        "app_generation",
        "source_generation",
        "session_id",
        "expires_at",
    ],
)
def test_oauth_orm_inputs_frozen_and_consumption_is_one_use(client, admin, oauth_state, field):
    with client.app.state.session_factory() as db:
        row = db.get(OAuthState, oauth_state)
        original = getattr(row, field)
        changed = (
            (original + timedelta(seconds=1))
            if field == "expires_at"
            else original + 1
            if isinstance(original, int)
            else "changed-fixture-value"
        )
        setattr(row, field, changed)
        with pytest.raises(ImmutableAuditError):
            db.commit()
        db.rollback()
        row = db.get(OAuthState, oauth_state)
        assert getattr(row, field) == original
        row.consumed_at = utcnow()
        db.commit()
        first = row.consumed_at
        for replacement in (None, first + timedelta(seconds=1)):
            row.consumed_at = replacement
            with pytest.raises(ImmutableAuditError, match="consumed once"):
                db.commit()
            db.rollback()
        assert db.get(OAuthState, oauth_state).consumed_at == first
        db.delete(row)
        with pytest.raises(ImmutableAuditError, match="cannot be deleted"):
            db.commit()
        db.rollback()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE oauth_states SET app_generation=app_generation+1 WHERE id=:id",
        "UPDATE oauth_states SET session_id='different-session' WHERE id=:id",
        "UPDATE oauth_states SET redirect_uri='https://other.example.test' WHERE id=:id",
        "DELETE FROM oauth_states WHERE id=:id",
        "INSERT OR REPLACE INTO oauth_states SELECT * FROM oauth_states WHERE id=:id",
    ],
)
def test_oauth_raw_sql_cannot_update_delete_or_replace_provenance(
    client, admin, oauth_state, statement
):
    with client.app.state.session_factory() as db:
        with pytest.raises(IntegrityError):
            db.execute(text(statement), {"id": oauth_state})
            db.commit()
        db.rollback()
        assert db.get(OAuthState, oauth_state).app_generation == 1


def test_oauth_raw_sql_allows_only_first_consumption_and_blocks_hash_collision_replace(
    client, admin, oauth_state
):
    with client.app.state.session_factory() as db:
        consumed = utcnow().replace(tzinfo=None).isoformat(" ")
        db.execute(
            text("UPDATE oauth_states SET consumed_at=:consumed WHERE id=:id"),
            {"id": oauth_state, "consumed": consumed},
        )
        db.commit()
        for value in (None, "2099-01-01 00:00:00"):
            with pytest.raises(IntegrityError):
                db.execute(
                    text("UPDATE oauth_states SET consumed_at=:value WHERE id=:id"),
                    {"id": oauth_state, "value": value},
                )
            db.rollback()
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT OR REPLACE INTO oauth_states "
                    "(id,created_at,state_hash,source_id,user_id,session_id,app_generation,"
                    "source_generation,redirect_uri,expires_at,consumed_at) "
                    "SELECT :other,created_at,state_hash,source_id,user_id,session_id,"
                    "app_generation,source_generation,redirect_uri,expires_at,consumed_at "
                    "FROM oauth_states WHERE id=:id"
                ),
                {"id": oauth_state, "other": str(uuid4())},
            )
        db.rollback()
        assert db.scalar(select(func.count()).select_from(OAuthState)) == 1


def test_logout_deletes_live_session_without_erasing_or_blocking_oauth_history(
    client, admin, oauth_state
):
    with client.app.state.session_factory() as db:
        row = db.get(OAuthState, oauth_state)
        session_id, user_id = row.session_id, row.user_id
    response = client.post("/api/v1/auth/logout", headers={"x-csrf-token": admin["csrf_token"]})
    assert response.status_code == 204
    with client.app.state.session_factory() as db:
        assert db.get(UserSession, session_id) is None
        assert db.get(OAuthState, oauth_state).session_id == session_id
        with pytest.raises(QBOAuthorizationError, match="admin_session_required"):
            active_session(db, user_id, session_id)


@pytest.mark.parametrize("environment", ["sandbox", None])
def test_bound_company_unique_index_includes_default_sandbox_and_retains_disconnected_binding(
    client, admin, connected, environment
):
    with client.app.state.session_factory() as db:
        current = db.get(Source, connected)
        current.config = {**current.config, "connection_status": "disconnected"}
        db.commit()
        duplicate = {"realm_id": "12345", "connection_status": "configured"}
        if environment:
            duplicate["environment"] = environment
        db.add(Source(kind="qbo", label="Duplicate synthetic company", config=duplicate))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        production = Source(
            kind="qbo",
            label="Independent production fixture",
            config={"environment": "production", "realm_id": "12345"},
        )
        db.add(production)
        db.commit()
        assert db.scalar(select(func.count()).select_from(Source)) == 2


def test_receipt_uniqueness_rolls_back_its_queued_poll_atomically(client, admin, connected):
    factory = client.app.state.session_factory
    with factory() as db:
        audit(
            db,
            "qbo.webhook_received",
            "sources",
            connected,
            after={"event_key": "synthetic-event-key"},
        )
        db.commit()
    with factory() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        attempt = service.enqueue_source_poll(db, connected)
        attempted_id = attempt.id
        audit(
            db,
            "qbo.webhook_received",
            "sources",
            connected,
            after={"event_key": "synthetic-event-key", "attempt_id": attempted_id},
        )
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()
        assert db.get(OperationAttempt, attempted_id) is None
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "qbo.webhook_received")
            )
            == 1
        )
        assert db.get(Source, connected).cursor is None


def test_signed_webhook_dedup_deferred_under_live_poll_then_actual_registry_and_screening(
    client, admin, connected
):
    factory, config = client.app.state.session_factory, client.app.state.config
    verifier = "synthetic-webhook-verifier"
    with factory() as db:
        save_app_config(
            db,
            config,
            AppConfig(
                "sandbox", APP.client_id, APP.client_secret, verifier_token=verifier, generation=1
            ),
        )
        source = db.get(Source, connected)
        source.config = {**source.config, "webhooks_enabled": True}
        db.commit()
        refresh_csl(db, file=FIXTURES / "csl_sample_v1.json")
        db.commit()
    event = [
        {
            "id": "coordinator-synthetic-event",
            "specversion": "1.0",
            "type": "qbo.customer.updated.v1",
            "source": "https://intuit.example.test/synthetic",
            "time": NOW.isoformat(),
            "intuitaccountid": "12345",
            "intuitentityid": "1",
            "data": {"DisplayName": "Webhook body must never become party data"},
        }
    ]
    raw = json.dumps(event).encode()
    signature = base64.b64encode(hmac.new(verifier.encode(), raw, hashlib.sha256).digest()).decode()
    calls = []

    def transport(request):
        calls.append(request)
        with factory() as other:
            other.execute(text("BEGIN IMMEDIATE"))
            other.rollback()
        entity = "Vendor" if "Vendor" in request.url.params["query"] else "Customer"
        return httpx.Response(200, json=query(entity, [fixture(entity.lower())]))

    http_transport = httpx.MockTransport(transport)
    factory.configure(
        info={**factory.kw.get("info", {}), "qbo_transport_loader": lambda: http_transport}
    )
    service.register_default_connectors()
    with protected_operation(factory, "source_sync", source_id=connected):
        for _ in range(2):
            with factory() as db:
                webhook_signals(db, config, "sandbox", raw, signature)
        with factory() as db:
            queued = db.scalar(select(OperationAttempt).where(OperationAttempt.status == "queued"))
            id = queued.id
            assert queued.parameters == {"mode": "incremental", "defer_if_busy": True}
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(AuditLog.action == "qbo.webhook_received")
                )
                == 1
            )
        deferred = service.dispatch_attempt(factory, config, id)
        assert (
            deferred["status"] == "queued" and deferred["error_code"] == "operation_already_running"
        )
        assert not calls
        with factory() as db:
            assert db.scalar(select(Party)) is None
    completed = service.dispatch_attempt(factory, config, id)
    assert completed["status"] == "success", completed
    assert completed["outcome"]["added"] == 2 and completed["outcome"]["run_ids"]
    with factory() as db:
        assert db.get(OperationAttempt, id).finished_at
        parties = list(db.scalars(select(Party)))
        assert {row.external_id for row in parties} == {"Customer:1", "Vendor:1"}
        assert all(row.name != "Webhook body must never become party data" for row in parties)
        assert all(
            db.get(ScreeningRun, run_id).finished_at for run_id in completed["outcome"]["run_ids"]
        )
        receipts = list(
            db.scalars(select(AuditLog).where(AuditLog.action == "qbo.webhook_received"))
        )
        assert receipts[0].after["attempt_id"] == id
        assert all(
            "synthetic-secret" not in json.dumps(row.parameters)
            for row in db.scalars(select(OperationAttempt))
        )
    assert len(calls) == 2


@pytest.mark.parametrize("owner", ["dead", "reused_pid"])
def test_dead_owner_takeover_preserves_full_request_before_incremental_claim(
    client, admin, connected, owner
):
    factory, config = client.app.state.session_factory, client.app.state.config
    actor_id = admin["user"]["id"]
    with factory() as db:
        source = db.get(Source, connected)
        # An incremental request could use CDC, so full queries later prove that
        # recovery retained the interrupted command rather than the new mode.
        source.cursor = NOW.isoformat()
        previous = OperationAttempt(
            operation="source_sync",
            source_id=connected,
            actor_user_id=actor_id,
            parameters={"mode": "full"},
            status="running",
            started_at=utcnow() - timedelta(minutes=1),
        )
        lease = OperationLease(
            key=lease_key("source_sync", connected),
            owner_id="previous-process-identity:synthetic-owner",
            owner_pid=99999999 if owner == "dead" else os.getpid(),
            # Future expiry must not cause a stopped/reused owner to keep work.
            expires_at=utcnow() + timedelta(minutes=5),
            fence=3,
        )
        db.add_all([previous, lease])
        db.commit()
        previous_id = previous.id
        refresh_csl(db, file=FIXTURES / "csl_sample_v1.json")
        db.commit()

    calls = []

    def transport(request):
        calls.append(request)
        assert request.url.path.endswith("/query")
        statement = request.url.params["query"]
        assert "WHERE Active IN (true, false)" in statement
        with factory() as other:
            other.execute(text("BEGIN IMMEDIATE"))
            other.rollback()
        entity = "Vendor" if "Vendor" in statement else "Customer"
        return httpx.Response(200, json=query(entity, [fixture(entity.lower())]))

    http_transport = httpx.MockTransport(transport)
    factory.configure(
        info={**factory.kw.get("info", {}), "qbo_transport_loader": lambda: http_transport}
    )
    service.register_default_connectors()
    with protected_operation(
        factory,
        "source_sync",
        source_id=connected,
        actor_user_id=actor_id,
        parameters={"mode": "incremental"},
    ) as incoming:
        with factory() as db:
            queued = list(
                db.scalars(
                    select(OperationAttempt).where(
                        OperationAttempt.source_id == connected,
                        OperationAttempt.status == "queued",
                    )
                )
            )
            assert len(queued) == 1
            replacement = queued[0]
            replacement_id = replacement.id
            assert replacement.operation == "source_sync"
            assert replacement.parameters == {"mode": "full"}
            assert replacement.actor_user_id == actor_id
            assert db.get(OperationAttempt, incoming.attempt_id).parameters == {
                "mode": "incremental"
            }
            previous = db.get(OperationAttempt, previous_id)
            assert previous.status == "interrupted" and previous.error_code == "owner_stopped"
            original = (
                previous.created_at,
                previous.started_at,
                previous.finished_at,
                previous.parameters,
                previous.actor_user_id,
                previous.source_id,
                previous.status,
                previous.error_code,
                previous.outcome,
            )
            recovery = db.scalar(
                select(AuditLog).where(
                    AuditLog.action == "monitoring.recovery_queued",
                    AuditLog.target_id == replacement_id,
                )
            )
            assert recovery.after["previous_attempt_id"] == previous_id
            interruption = db.scalar(
                select(AuditLog).where(
                    AuditLog.action == "monitoring.interrupted",
                    AuditLog.target_id == previous_id,
                )
            )
            assert interruption.after["retry_attempt_id"] == replacement_id
        assert service.recover_interrupted(factory) == []
        assert calls == []

    completed = service.dispatch_attempt(factory, config, replacement_id)
    assert completed["status"] == "success", completed
    assert completed["outcome"]["added"] == 2 and completed["outcome"]["run_ids"]
    assert len(calls) == 2
    with factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "monitoring.recovery_queued")
            )
            == 1
        )
        previous = db.get(OperationAttempt, previous_id)
        assert (
            previous.created_at,
            previous.started_at,
            previous.finished_at,
            previous.parameters,
            previous.actor_user_id,
            previous.source_id,
            previous.status,
            previous.error_code,
            previous.outcome,
        ) == original
        assert db.get(OperationAttempt, replacement_id).parameters == {"mode": "full"}
        previous.parameters = {"mode": "incremental"}
        with pytest.raises(ImmutableAuditError):
            db.commit()
        db.rollback()
    with factory() as db:
        with pytest.raises(IntegrityError):
            db.execute(
                text("UPDATE operation_attempts SET parameters=:parameters WHERE id=:id"),
                {"id": previous_id, "parameters": json.dumps({"mode": "incremental"})},
            )
        db.rollback()
        with pytest.raises(IntegrityError):
            db.execute(
                text("UPDATE audit_log SET after=:after WHERE id=:id"),
                {
                    "id": recovery.id,
                    "after": json.dumps({"previous_attempt_id": incoming.attempt_id}),
                },
            )
        db.rollback()


def test_actual_scheduler_only_connected_sources_and_source_api_exact_next_run(
    client, admin, connected
):
    factory, config = client.app.state.session_factory, client.app.state.config
    with factory() as db:
        db.add_all(
            [
                Source(
                    kind="qbo", label="Not connected fixture", config={"environment": "sandbox"}
                ),
                Source(
                    kind="qbo",
                    label="Disconnected fixture",
                    config={"environment": "sandbox", "connection_status": "disconnected"},
                    credentials_enc="retained-nonworking-fixture-envelope",
                ),
                Source(kind="csv", label="CSV remains manual fixture"),
            ]
        )
        db.commit()
    runtime = SchedulerRuntime(factory, config)
    runtime.scheduler.pause()
    previous_runtime = client.app.state.scheduler
    client.app.state.scheduler = runtime
    try:
        jobs = {job.id: job for job in runtime.scheduler.get_jobs()}
        source_jobs = {id for id in jobs if id.startswith("source-")}
        assert source_jobs == {f"source-{connected}"}
        expected = jobs[f"source-{connected}"].next_run_time
        actual = client.get(f"/api/v1/sources/{connected}").json()
        assert actual["next_sync_at"] == expected.isoformat()
        runtime.reconcile()
        assert runtime.scheduler.get_job(f"source-{connected}").next_run_time == expected
        with factory() as db:
            source = db.get(Source, connected)
            source.config = {**source.config, "connection_status": "disconnecting"}
            db.commit()
        runtime.reconcile()
        assert runtime.scheduler.get_job(f"source-{connected}") is None
        assert client.get(f"/api/v1/sources/{connected}").json()["next_sync_at"] is None
    finally:
        client.app.state.scheduler = previous_runtime
        runtime.shutdown()


def test_source_disconnect_shares_poll_and_oauth_resource_and_defers_queued_work(
    client, admin, connected
):
    factory, config = client.app.state.session_factory, client.app.state.config
    for operation in ("source_disconnect", "qbo_oauth_start", "qbo_oauth_callback"):
        assert lease_key(operation, connected) == lease_key("source_sync", connected)
    with protected_operation(factory, "source_sync", source_id=connected):
        with factory() as db:
            row = service.enqueue_source_disconnect(
                db, connected, actor_user_id=admin["user"]["id"]
            )
            db.commit()
            id = row.id
        result = service.dispatch_attempt(factory, config, id)
        assert result["status"] == "queued" and result["error_code"] == "operation_already_running"
        with factory() as db:
            assert db.get(Source, connected).credentials_enc
            assert db.get(OperationAttempt, id).started_at is None


def test_invalid_mode_rejected_at_enqueue_and_persisted_dispatch_without_http(
    client, admin, connected
):
    factory, config = client.app.state.session_factory, client.app.state.config
    with pytest.raises(ValueError, match="Choose incremental or full sync"):
        service.enqueue_operation(
            factory, config, "source_sync", source_id=connected, parameters={"mode": "unknown-mode"}
        )
    calls = []
    factory.configure(
        info={
            **factory.kw.get("info", {}),
            "qbo_transport_loader": lambda: httpx.MockTransport(
                lambda request: calls.append(request)
            ),
        }
    )
    with factory() as db:
        assert db.scalar(select(OperationAttempt)) is None
        stale = OperationAttempt(
            operation="source_sync", source_id=connected, parameters={"mode": "unknown-mode"}
        )
        db.add(stale)
        db.commit()
        id = stale.id
    result = service.dispatch_attempt(factory, config, id)
    assert result["status"] == "failed" and result["error_code"] == "operation_failed"
    assert not calls
    with factory() as db:
        assert db.scalar(select(Party)) is None and db.get(Source, connected).cursor is None


def test_default_connector_registration_preserves_explicit_existing_handler(monkeypatch):
    from app.connectors.qbo import sync_qbo_source

    monkeypatch.delitem(service.SOURCE_HANDLERS, "qbo", raising=False)
    service.register_default_connectors()
    assert service.SOURCE_HANDLERS["qbo"] is sync_qbo_source

    def explicitly_registered(db, config, source):
        raise AssertionError("Registration must not execute a source handler")

    monkeypatch.setitem(service.SOURCE_HANDLERS, "qbo", explicitly_registered)
    service.register_default_connectors()
    assert service.SOURCE_HANDLERS["qbo"] is explicitly_registered


def test_accepted_rotation_keeps_new_private_orphan_cleanup_then_real_worker_preserves_connection(
    client, admin, connected
):
    factory, config = client.app.state.session_factory, client.app.state.config
    orphan = {
        "environment": "sandbox",
        "client_id": "synthetic-orphan-app",
        "client_secret": "synthetic-orphan-secret",
        "token": "synthetic-orphan-token",
    }
    cleanup_ids, revoked = [], []
    with factory() as db:
        source = db.get(Source, connected)
        values = load_source_credentials(source, config)
        values["access_expires_at"] = NOW.isoformat()
        save_source_credentials(db, source, config, values)
        db.commit()

    def transport(request):
        if request.url.path.endswith("bearer"):
            with factory() as other:
                other.execute(text("BEGIN IMMEDIATE"))
                source = other.get(Source, connected)
                values = load_source_credentials(source, config)
                assert "pending_revocations" not in values
                save_source_credentials(
                    other, source, config, {**values, "pending_revocations": [orphan]}
                )
                cleanup = service.enqueue_source_disconnect(other, connected, actor_user_id=None)
                cleanup_ids.append(cleanup.id)
                other.commit()
            return httpx.Response(
                200, json=fixture("tokens") | {"refresh_token": "accepted-with-orphan-backlog"}
            )
        if request.url.path.endswith("revoke"):
            revoked.append(json.loads(request.content)["token"])
            return httpx.Response(200, json={})
        if "Vendor" in request.url.params["query"]:
            return httpx.Response(200, json={"QueryResponse": {"Vendor": "invalid-page"}})
        return httpx.Response(200, json=query("Customer", [fixture("customer")]))

    http_transport = httpx.MockTransport(transport)
    factory.configure(
        info={**factory.kw.get("info", {}), "qbo_transport_loader": lambda: http_transport}
    )
    service.register_default_connectors()
    with pytest.raises(QBOError):
        with protected_operation(
            factory, "source_sync", source_id=connected, actor_user_id=admin["user"]["id"]
        ) as execution:
            with operation_session(factory, execution, admin["user"]["id"]) as db:
                sync_qbo_source(
                    db, config, db.get(Source, connected), mode="full", transport=http_transport
                )
    with factory() as db:
        source = db.get(Source, connected)
        values = load_source_credentials(source, config)
        assert values["refresh_token"] == "accepted-with-orphan-backlog"
        assert values["pending_revocations"] == [orphan]
        assert source.cursor is None and db.scalar(select(Party)) is None
        assert (
            orphan["token"] not in source.credentials_enc
            and orphan["client_secret"] not in source.credentials_enc
        )
        rotation = db.scalar(select(AuditLog).where(AuditLog.action == "qbo.tokens_rotated"))
        assert (
            rotation.actor_user_id is None
            and rotation.after["operation_attempt_id"] == execution.attempt_id
        )
        assert db.get(OperationAttempt, cleanup_ids[0]).status == "queued"
    cleaned = service.dispatch_attempt(factory, config, cleanup_ids[0])
    assert cleaned["status"] == "success" and revoked == [orphan["token"]]
    with factory() as db:
        source = db.get(Source, connected)
        values = load_source_credentials(source, config)
        assert (
            source.config["connection_status"] == "connected"
            and source.config["connection_generation"] == 1
        )
        assert values["refresh_token"] == "accepted-with-orphan-backlog" and not values.get(
            "pending_revocations"
        )
        recorded = json.dumps(
            [(row.before, row.after) for row in db.scalars(select(AuditLog))], default=str
        )
        assert orphan["client_secret"] not in recorded and orphan["token"] not in recorded
