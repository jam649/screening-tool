"""BC ownership, durable lifecycle, permanent identity and callback privacy."""

import asyncio

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

from app.audit import ImmutableAuditError, audit, redact_credentials
from app.models import AuditLog, OperationAttempt, Source
from app.oauth_privacy import OAuthCallbackPrivacyMiddleware
from app.scheduler import service
from app.scheduler.leases import lease_key, protected_operation, validate_parameters
from app.scheduler.runtime import SchedulerRuntime
from app.security import encrypt_json

TENANT = "11111111-1111-1111-1111-111111111111"
COMPANY = "22222222-2222-2222-2222-222222222222"


def values(**changes):
    return {
        "tenant_id": TENANT,
        "bc_environment": "Sandbox_US",
        "company_id": COMPANY,
        "connection_status": "connected",
        "connection_generation": 1,
        "webhooks_enabled": False,
        **changes,
    }


@pytest.fixture
def bound_source(client, admin):
    with client.app.state.session_factory() as db:
        row = Source(kind="bc", label="Synthetic BC company", config=values())
        db.add(row)
        db.commit()
        return row.id


def test_bc_create_checks_uuid_environment_and_keeps_private_identity(client, admin):
    headers = {"x-csrf-token": admin["csrf_token"]}
    body = {
        "kind": "bc",
        "label": "Synthetic company",
        "tenant_id": TENANT,
        "bc_environment": "Sandbox_US",
    }
    response = client.post("/api/v1/sources", json=body, headers=headers)
    assert response.status_code == 201
    assert response.json()["bc_environment"] == "Sandbox_US"
    assert response.json()["environment"] is None
    assert response.json()["connection_status"] == "configured"
    assert "tenant_id" not in response.json()
    with client.app.state.session_factory() as db:
        config = db.get(Source, response.json()["id"]).config
        assert config["tenant_id"] == TENANT and not config.get("company_id")
    for change in (
        {"tenant_id": "tenant.example.test"},
        {"tenant_id": "../other"},
        {"bc_environment": "../other"},
        {"bc_environment": "A" * 30},
        {"bc_environment": "1Sandbox"},
        {"bc_environment": "Sandböx"},
        {"environment": "sandbox"},
        {"kind": "csv"},
        {"kind": "qbo"},
    ):
        assert (
            client.post("/api/v1/sources", json={**body, **change}, headers=headers).status_code
            == 422
        )


@pytest.mark.parametrize("key", ["tenant_id", "bc_environment", "company_id"])
def test_bound_identity_frozen_by_orm_with_label_status_editable(client, bound_source, key):
    with client.app.state.session_factory() as db:
        row = db.get(Source, bound_source)
        row.config = {**row.config, key: "changed"}
        with pytest.raises(ImmutableAuditError, match="permanent"):
            db.commit()
        db.rollback()
        row = db.get(Source, bound_source)
        row.label = "Renamed synthetic company"
        row.config = {**row.config, "connection_status": "disconnected"}
        db.commit()
        assert row.config[key] == values()[key]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE sources SET config=json_set(config,'$.company_id','changed') WHERE id=:id",
        "UPDATE sources SET config=json_remove(config,'$.tenant_id') WHERE id=:id",
        "UPDATE sources SET kind='csv' WHERE id=:id",
        "UPDATE sources SET id='66666666-6666-6666-6666-666666666666' WHERE id=:id",
        "DELETE FROM sources WHERE id=:id",
        "INSERT OR REPLACE INTO sources SELECT * FROM sources WHERE id=:id",
    ],
)
def test_bound_identity_frozen_by_sql_and_cannot_be_deleted_replaced(
    client, bound_source, statement
):
    with client.app.state.session_factory() as db:
        with pytest.raises(IntegrityError):
            db.execute(text(statement), {"id": bound_source})
            db.commit()
        db.rollback()
        assert db.get(Source, bound_source).config["company_id"] == COMPANY


def test_unique_company_binding_survives_disconnect_with_environment_isolation(
    client, bound_source
):
    with client.app.state.session_factory() as db:
        row = db.get(Source, bound_source)
        row.config = {**row.config, "connection_status": "disconnected"}
        db.commit()
        db.add(Source(kind="bc", label="Duplicate", config=values()))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        db.add(Source(kind="bc", label="Case alias", config=values(bc_environment="sandbox_us")))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        db.add(
            Source(kind="bc", label="Other environment", config=values(bc_environment="Production"))
        )
        db.commit()
        assert db.scalar(select(func.count()).select_from(Source)) == 2


@pytest.mark.parametrize(
    "operation",
    ["bc_companies", "bc_connect", "bc_subscriptions", "source_sync", "source_disconnect"],
)
def test_bc_lifecycle_has_one_source_lease(operation):
    assert lease_key(operation, "synthetic-source") == "source-sync:synthetic-source"


def test_duplicate_bc_receipt_rolls_back_new_poll(client, bound_source):
    factory = client.app.state.session_factory
    with factory() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        first = service.enqueue_source_poll(db, bound_source, mode="full")
        audit(
            db,
            "bc.webhook_received",
            "sources",
            bound_source,
            after={"event_key": "synthetic-receipt", "attempt_id": first.id},
        )
        db.commit()
    with factory() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        second = service.enqueue_source_poll(db, bound_source, mode="incremental")
        second_id = second.id
        audit(
            db,
            "bc.webhook_received",
            "sources",
            bound_source,
            after={"event_key": "synthetic-receipt", "attempt_id": second_id},
        )
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()
        assert db.get(OperationAttempt, second_id) is None
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "bc.webhook_received")
            )
            == 1
        )


def test_busy_bc_lifecycle_stays_queued_and_equivalent_requests_coalesce(client, bound_source):
    factory, config = client.app.state.session_factory, client.app.state.config
    with protected_operation(factory, "bc_companies", source_id=bound_source):
        with factory() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            first = service.enqueue_bc_subscriptions(db, bound_source)
            assert first.id == service.enqueue_bc_subscriptions(db, bound_source).id
            attempt_id = first.id
            db.commit()
        result = service.dispatch_attempt(factory, config, attempt_id)
        assert result["status"] == "queued" and result["error_code"] == "operation_already_running"
        with factory() as db:
            row = db.get(OperationAttempt, attempt_id)
            assert row.finished_at is None and row.parameters == {"defer_if_busy": True}
    for invalid in (
        {"client_secret": "synthetic-private"},
        {"mode": "full"},
        {"defer_if_busy": "true"},
    ):
        with pytest.raises(ValueError):
            validate_parameters("bc_subscriptions", invalid)


def test_actual_renewal_jobs_persist_and_failed_cleanup_remains_scheduled(client, admin):
    factory, config = client.app.state.session_factory, client.app.state.config
    expected = set()
    with factory() as db:
        for i, (status, enabled, cleanup, credentialed, eligible) in enumerate(
            [
                ("connected", True, "not_required", True, True),
                ("connected", False, "not_required", True, False),
                ("disconnected", False, "pending", True, True),
                ("disconnected", False, "failed", True, True),
                ("disconnected", False, "completed", False, False),
                ("configured", True, "not_required", False, False),
            ]
        ):
            row = Source(
                kind="bc",
                label=f"Synthetic lifecycle {i}",
                config=values(
                    company_id=f"{i + 3:08d}-2222-2222-2222-222222222222",
                    connection_status=status,
                    webhooks_enabled=enabled,
                    cleanup_status=cleanup,
                ),
                credentials_enc=encrypt_json({"access_token": "synthetic-bearer"}, config)
                if credentialed
                else None,
            )
            db.add(row)
            db.flush()
            if eligible:
                expected.add("bc-subscriptions-" + row.id)
        db.commit()
    runtime = SchedulerRuntime(factory, config)
    try:
        jobs = {
            j.id: j for j in runtime.scheduler.get_jobs() if j.id.startswith("bc-subscriptions-")
        }
        assert set(jobs) == expected
        next_runs = {id: j.next_run_time for id, j in jobs.items()}
        assert all(j.trigger.interval.total_seconds() == 43200 for j in jobs.values())
    finally:
        runtime.shutdown()
    restored = SchedulerRuntime(factory, config)
    try:
        assert {id: restored.scheduler.get_job(id).next_run_time for id in expected} == next_runs
    finally:
        restored.shutdown()


def test_validation_token_scrubbed_before_url_construction_even_on_error():
    seen, sent = [], []

    async def downstream(scope, receive, send):
        seen.append(
            (str(Request(scope).url), scope["_bc_webhook_params"], scope["_bc_webhook_query_valid"])
        )
        await send({"type": "http.response.start", "status": 400, "headers": []})
        await send({"type": "http.response.body", "body": b"invalid"})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    async def exercise():
        app = OAuthCallbackPrivacyMiddleware(downstream)
        for query in (
            b"validationToken=synthetic%20private",
            b"validationToken=one&validationToken=two",
            b"validationToken=%ZZ",
        ):
            await app(
                {
                    "type": "http",
                    "method": "POST",
                    "scheme": "https",
                    "path": "/api/v1/webhooks/bc/source",
                    "query_string": query,
                    "headers": [(b"host", b"example.test")],
                    "server": ("example.test", 443),
                },
                receive,
                send,
            )

    asyncio.run(exercise())
    assert all("?" not in url for url, _, _ in seen)
    assert seen[0][1] == {"validationToken": "synthetic private"} and seen[0][2]
    assert all(params == {} and not valid for _, params, valid in seen[1:])
    for response in sent[::2]:
        assert (b"referrer-policy", b"no-referrer") in response["headers"]
        assert (b"cache-control", b"no-store") in response["headers"]
    assert redact_credentials({"clientState": "private", "client_state": "private"}) == {
        "clientState": "[redacted]",
        "client_state": "[redacted]",
    }


def test_public_address_change_atomically_queues_bc_rebind_then_http_disables_webhooks(
    client, admin, bound_source
):
    headers = {"x-csrf-token": admin["csrf_token"]}
    factory, config = client.app.state.session_factory, client.app.state.config
    with factory() as db:
        source = db.get(Source, bound_source)
        source.credentials_enc = encrypt_json({"access_token": "synthetic-bearer"}, config)
        source.config = {**source.config, "webhooks_enabled": True}
        db.commit()
        ciphertext = source.credentials_enc
    response = client.patch(
        "/api/v1/settings/notifications",
        headers=headers,
        json={"public_base_url": "https://new.example.test"},
    )
    assert response.status_code == 200
    with factory() as db:
        queued = list(
            db.scalars(
                select(OperationAttempt).where(OperationAttempt.operation == "bc_subscriptions")
            )
        )
        assert len(queued) == 1 and queued[0].status == "queued"
        assert queued[0].actor_user_id is None and queued[0].parameters == {"defer_if_busy": True}
        source = db.get(Source, bound_source)
        assert source.config["webhooks_enabled"] and source.credentials_enc == ciphertext
    response = client.patch(
        "/api/v1/settings/notifications",
        headers=headers,
        json={"public_base_url": "http://localhost:8000"},
    )
    assert response.status_code == 200
    with factory() as db:
        source = db.get(Source, bound_source)
        assert source.config["connection_status"] == "connected"
        assert source.config["webhooks_enabled"] is False
        assert source.config["cleanup_status"] == "pending" and source.credentials_enc == ciphertext
        assert (
            db.scalar(
                select(func.count())
                .select_from(OperationAttempt)
                .where(OperationAttempt.operation == "bc_subscriptions")
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "bc.webhook_address_changed")
            )
            == 2
        )


def test_public_address_change_rolls_back_settings_and_source_when_queue_cannot_commit(
    client, admin, bound_source, monkeypatch
):
    from app.api import notifications
    from app.models import Setting

    headers = {"x-csrf-token": admin["csrf_token"]}
    factory, config = client.app.state.session_factory, client.app.state.config
    with factory() as db:
        source = db.get(Source, bound_source)
        source.credentials_enc = encrypt_json({"access_token": "synthetic-bearer"}, config)
        source.config = {**source.config, "webhooks_enabled": True}
        db.commit()

    def rejected(*args, **kwargs):
        raise RuntimeError("Synthetic durable queue failure")

    monkeypatch.setattr(notifications, "enqueue_bc_subscriptions", rejected)
    with pytest.raises(RuntimeError, match="Synthetic durable queue failure"):
        client.patch(
            "/api/v1/settings/notifications",
            headers=headers,
            json={"public_base_url": "http://localhost:8000"},
        )
    with factory() as db:
        assert db.scalar(select(Setting).where(Setting.key == "public_base_url")) is None
        assert db.get(Source, bound_source).config["webhooks_enabled"] is True
        assert db.scalar(select(func.count()).select_from(OperationAttempt)) == 0


def test_bound_bc_source_id_is_frozen_even_before_party_import(client, bound_source):
    with client.app.state.session_factory() as db:
        source = db.get(Source, bound_source)
        source.id = "66666666-6666-6666-6666-666666666666"
        with pytest.raises(ImmutableAuditError, match="permanent"):
            db.commit()
        db.rollback()
        assert db.get(Source, bound_source).config["company_id"] == COMPANY
