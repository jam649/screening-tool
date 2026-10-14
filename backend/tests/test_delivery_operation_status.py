"""Caller status must preserve a committed report when dispatch cannot finish."""

from contextlib import contextmanager

import pytest
from sqlalchemy import select, text
from typer.testing import CliRunner

from app import cli as operator
from app.models import AuditLog, NotificationBatch, NotificationDelivery, OperationAttempt
from app.reports.morning import enqueue_morning_report
from app.scheduler.service import run_operation


def delivery_result(*, failed):
    counts = {"sent": 0, "failed": 0, "uncertain": 0, "cancelled": 0}
    if failed:
        counts["error_code"] = "dispatcher_failed"
    return counts


def configure_report(client, admin):
    response = client.patch(
        "/api/v1/settings/notifications",
        json={
            "smtp_host": "127.0.0.1",
            "smtp_port": 2525,
            "smtp_tls": "none",
            "smtp_from": "screening@example.test",
            "report_recipients": ["recipient@example.test"],
            "public_base_url": "https://screening.example.test",
        },
        headers={"x-csrf-token": admin["csrf_token"]},
    )
    assert response.status_code == 200


def freeze_report(client):
    with client.app.state.session_factory() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        batch = enqueue_morning_report(db, client.app.state.config)
        db.commit()
        delivery = db.scalar(
            select(NotificationDelivery).where(NotificationDelivery.batch_id == batch.id)
        )
        return batch.id, (
            batch.payload_enc,
            batch.dedupe_key,
            batch.window_start,
            batch.window_end,
            delivery.id,
            delivery.recipient,
            delivery.message_id,
        )


@pytest.mark.parametrize("operation", ["morning_report", "alert_dispatch"])
def test_dispatch_store_failure_keeps_frozen_report_and_durable_outcome(
    client, admin, monkeypatch, operation
):
    configure_report(client, admin)
    # Morning must create and commit its own batch before attempting delivery;
    # alert dispatch starts with an already committed real report.
    frozen_report = [freeze_report(client)] if operation == "alert_dispatch" else []
    calls = []
    failing = True

    def dispatch(*args, **kwargs):
        # The dispatcher deliberately returns this safe contract for a store or
        # fence failure. Recipient counters can all be zero even on failure.
        calls.append(kwargs)
        if not frozen_report:
            with client.app.state.session_factory() as db:
                # An independent writer proves the enqueue transaction committed
                # before dispatch, and observes the newly frozen report.
                db.execute(text("BEGIN IMMEDIATE"))
                batch = db.get(NotificationBatch, kwargs["batch_id"])
                delivery = db.scalar(
                    select(NotificationDelivery).where(NotificationDelivery.batch_id == batch.id)
                )
                frozen_report.append(
                    (
                        batch.id,
                        (
                            batch.payload_enc,
                            batch.dedupe_key,
                            batch.window_start,
                            batch.window_end,
                            delivery.id,
                            delivery.recipient,
                            delivery.message_id,
                        ),
                    )
                )
                db.rollback()
        return delivery_result(failed=failing)

    monkeypatch.setattr("app.notifications.service.dispatch_outbox", dispatch)
    factory, config = client.app.state.session_factory, client.app.state.config
    result = run_operation(factory, config, operation, actor_user_id=admin["user"]["id"])
    assert len(calls) == 1
    batch_id, frozen = frozen_report[0]
    assert result["status"] == "failed"
    assert result["error_code"] == "operation_failed"
    assert result["outcome"]["delivery"] == delivery_result(failed=True)
    if operation == "morning_report":
        assert result["outcome"]["batch_id"] == batch_id
        assert calls[0]["batch_id"] == batch_id
    with factory() as db:
        attempt = db.get(OperationAttempt, result["attempt_id"])
        assert attempt.status == "failed" and attempt.finished_at is not None
        assert attempt.outcome == result["outcome"]
        failure = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "monitoring.failed", AuditLog.target_id == attempt.id
            )
        )
        assert failure.after["outcome"] == result["outcome"]

    # A later healthy empty dispatch succeeds and reuses the existing report.
    failing = False
    retry = run_operation(factory, config, operation, actor_user_id=admin["user"]["id"])
    assert retry["status"] == "success" and retry["error_code"] is None
    assert retry["outcome"]["delivery"] == delivery_result(failed=False)
    with factory() as db:
        assert list(db.scalars(select(NotificationBatch.id))) == [batch_id]
        batch = db.get(NotificationBatch, batch_id)
        delivery = db.scalar(
            select(NotificationDelivery).where(NotificationDelivery.batch_id == batch_id)
        )
        assert (
            batch.payload_enc,
            batch.dedupe_key,
            batch.window_start,
            batch.window_end,
            delivery.id,
            delivery.recipient,
            delivery.message_id,
        ) == frozen
        assert batch.status == "pending" and delivery.status == "pending"
        assert delivery.attempts == 0


@pytest.mark.parametrize("command", ["report", "alert"])
@pytest.mark.parametrize("failed", [True, False])
def test_cli_checks_dispatch_error_even_with_zero_recipient_counts(monkeypatch, command, failed):
    @contextmanager
    def database():
        yield None, None

    monkeypatch.setattr(operator, "operator_database", database)
    monkeypatch.setattr(
        operator,
        "run_operation",
        lambda *args, **kwargs: {
            "attempt_id": "fixture-attempt",
            "operation": "morning_report" if command == "report" else "alert_dispatch",
            "status": "success",
            "outcome": {"delivery": delivery_result(failed=failed)},
        },
    )
    # Exercise the CLI's delivery check independently of require_success: older
    # successful wrapper results can still contain a failed dispatcher outcome.
    result = CliRunner().invoke(operator.cli, [command, "send"])
    assert result.exit_code == (1 if failed else 0)
    assert ("dispatcher_failed" in result.output) is failed
