import json
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from app.models import OperationAttempt, OperationLease, Setting, Source, utcnow
from app.scheduler.leases import (
    OperationBusyError,
    OperationLeaseLostError,
    _process_identity,
    operation_session,
    protected_operation,
)
from app.scheduler.runtime import SchedulerRuntime
from app.scheduler.service import dispatch_attempt, enqueue_operation, run_operation
from app.security import encrypt_json


def test_overlap_shared_resource_and_sanitized_attempts(client, admin):
    factory = client.app.state.session_factory
    with protected_operation(factory, "screen_all") as execution:
        with pytest.raises(OperationBusyError):
            with protected_operation(factory, "screen_party"):
                pass
        with factory() as db:
            skipped = db.scalar(
                select(OperationAttempt).where(OperationAttempt.status == "skipped")
            )
            assert skipped.error_code == "operation_already_running"
        execution.complete({"run_ids": []})
    with factory() as db:
        assert db.scalar(select(OperationLease.owner_id)) is None
        assert db.scalar(select(OperationAttempt).where(OperationAttempt.status == "success"))


def test_fence_checked_before_business_commit(client, admin):
    factory = client.app.state.session_factory
    with pytest.raises(OperationLeaseLostError):
        with protected_operation(factory, "screen_all") as execution:
            with factory() as db:
                lease = db.scalar(select(OperationLease))
                lease.fence += 1
                db.commit()
            with operation_session(factory, execution) as db:
                db.add(Setting(key="fixture-fenced", value="must rollback"))
                db.commit()
    with factory() as db:
        assert db.scalar(select(Setting).where(Setting.key == "fixture-fenced")) is None


def test_dead_reused_pid_owner_recovery(client, admin):
    factory = client.app.state.session_factory
    import os

    with factory() as db:
        db.add(
            OperationLease(
                key="screening",
                owner_id="different-start:" + str(uuid4()),
                owner_pid=os.getpid(),
                fence=4,
                expires_at=utcnow() - timedelta(seconds=1),
            )
        )
        old = OperationAttempt(operation="screen_all", status="running")
        db.add(old)
        db.commit()
        old_id = old.id
    with protected_operation(factory, "screen_all") as execution:
        assert execution.fence == 5
        assert execution.owner_id.startswith(_process_identity(os.getpid()))
    with factory() as db:
        assert db.get(OperationAttempt, old_id).status == "interrupted"


def test_live_owner_not_stolen_when_heartbeat_expired(client, admin):
    factory = client.app.state.session_factory
    with protected_operation(factory, "screen_all"):
        with factory() as db:
            lease = db.scalar(select(OperationLease))
            lease.expires_at = utcnow() - timedelta(seconds=1)
            db.commit()
        with pytest.raises(OperationBusyError):
            with protected_operation(factory, "screen_changed"):
                pass


def test_schedules_persist_json_next_time_timezone_and_source_slots(client, admin):
    factory, config = client.app.state.session_factory, client.app.state.config
    with factory() as db:
        db.scalar(select(Setting).where(Setting.key == "timezone")).value = "America/New_York"
        db.add_all(
            [
                Source(
                    kind="qbo",
                    label="Fixture API",
                    config={
                        "connection_status": "connected",
                        "environment": "sandbox",
                        "realm_id": "12345",
                    },
                    credentials_enc=encrypt_json({"synthetic": True}, config),
                ),
                Source(kind="csv", label="Fixture CSV"),
            ]
        )
        db.commit()
    runtime = SchedulerRuntime(factory, config)
    runtime.scheduler.pause()
    try:
        jobs = runtime.scheduler.get_jobs()
        before = {job.id: job.next_run_time for job in jobs}
        assert sum(job.id.startswith("source-") for job in jobs) == 1
        assert str(runtime.scheduler.get_job("csl-daily").trigger.timezone) == "America/New_York"
        with factory() as db:
            states = list(db.scalars(text("SELECT job_state FROM apscheduler_jobs")))
            assert all(json.loads(state)["version"] == 1 for state in states)
            assert all("secret" not in state and "factory" not in state for state in states)
    finally:
        runtime.shutdown()
    runtime = SchedulerRuntime(factory, config)
    runtime.scheduler.pause()
    try:
        assert {job.id: job.next_run_time for job in runtime.scheduler.get_jobs()} == before
        with factory() as db:
            db.scalar(select(Setting).where(Setting.key == "timezone")).value = "UTC"
            db.commit()
        runtime.reconcile()
        assert str(runtime.scheduler.get_job("csl-daily").trigger.timezone) == "UTC"
    finally:
        runtime.shutdown()


def test_queued_actor_rechecked_and_failures_safe(client, admin):
    from app.models import User

    factory, config = client.app.state.session_factory, client.app.state.config
    queued = enqueue_operation(factory, config, "screen_all", actor_user_id=admin["user"]["id"])
    # A failed command cannot leak an exception message or persist a partial run.
    result = dispatch_attempt(factory, config, queued["attempt_id"])
    assert result["status"] == "failed" and result["error_code"] == "operation_failed"
    assert result["outcome"] == {}
    queued = enqueue_operation(factory, config, "screen_all", actor_user_id=admin["user"]["id"])
    with factory() as db:
        db.get(User, admin["user"]["id"]).active = False
        db.commit()
    result = dispatch_attempt(factory, config, queued["attempt_id"])
    assert result["status"] == "failed" and result["error_code"] == "actor_no_longer_admin"


def test_monitoring_api_roles_and_schedule_validation(client, admin, monkeypatch):
    headers = {"x-csrf-token": admin["csrf_token"]}
    assert client.get("/api/v1/settings/monitoring").json()["csl_fetch_time"] == "05:00"
    for body in (
        {"csl_fetch_time": "25:00"},
        {"source_poll_minutes": 0},
        {"source_poll_minutes": True},
        {"retention_weekday": "Monday"},
        {"password": "never echo value"},
    ):
        assert (
            client.patch("/api/v1/settings/monitoring", json=body, headers=headers).status_code
            == 422
        )
    assert (
        client.patch(
            "/api/v1/settings/monitoring", json={"csl_fetch_time": "06:15"}, headers=headers
        ).json()["csl_fetch_time"]
        == "06:15"
    )
    assert (
        client.patch("/api/v1/settings/monitoring", json={"csl_fetch_time": "06:15"}).status_code
        == 403
    )
    from app.models import User

    with client.app.state.session_factory() as db:
        db.get(User, admin["user"]["id"]).role = "reviewer"
        db.commit()
    assert client.get("/api/v1/settings/monitoring").status_code == 403
    assert client.get("/api/v1/settings/notifications").status_code == 403
    assert (
        client.post(
            "/api/v1/notifications/test", json={"recipient": "one@example.test"}, headers=headers
        ).status_code
        == 403
    )
    assert client.get("/api/v1/monitoring/status").status_code == 200
    assert client.get("/api/v1/notifications/status").status_code == 200


def test_recovery_requeues_dead_owner_preserving_inputs(client, admin):
    from app.scheduler.service import recover_interrupted

    factory = client.app.state.session_factory
    with factory() as db:
        lease = OperationLease(
            key="screening",
            owner_id="dead:nonce",
            owner_pid=99999999,
            expires_at=utcnow() + timedelta(minutes=5),
            fence=3,
        )
        attempt = OperationAttempt(
            operation="screen_all",
            status="running",
            actor_user_id=admin["user"]["id"],
            parameters={},
        )
        db.add_all([lease, attempt])
        db.commit()
        old_id = attempt.id
    ids = recover_interrupted(factory)
    assert len(ids) == 1
    with factory() as db:
        assert db.get(OperationAttempt, old_id).status == "interrupted"
        replacement = db.get(OperationAttempt, ids[0])
        assert replacement.status == "queued" and replacement.actor_user_id == admin["user"]["id"]
        assert replacement.operation == "screen_all" and replacement.parameters == {}
    assert recover_interrupted(factory) == []
    with protected_operation(factory, "screen_all", attempt_id=ids[0]):
        pass


def test_daily_schedule_dst_and_coalesced_catchup(client, admin, monkeypatch):
    from datetime import datetime
    from threading import Event
    from zoneinfo import ZoneInfo

    from apscheduler.triggers.cron import CronTrigger

    timezone = ZoneInfo("America/New_York")
    trigger = CronTrigger(hour=7, minute=0, timezone=timezone)
    for year, month, day, expected in [(2026, 3, 7, 23), (2026, 10, 31, 25)]:
        first = trigger.get_next_fire_time(None, datetime(year, month, day, 0, tzinfo=timezone))
        second = trigger.get_next_fire_time(first, first + timedelta(seconds=1))
        assert (second.timestamp() - first.timestamp()) / 3600 == expected
        assert first.hour == second.hour == 7
    factory, config = client.app.state.session_factory, client.app.state.config
    runtime = SchedulerRuntime(factory, config)
    runtime.scheduler.pause()
    try:
        job = runtime.scheduler.get_job("outbox-recovery")
        assert job.coalesce and job.max_instances == 1 and job.misfire_grace_time == 86400
        missed_time = utcnow() - timedelta(minutes=2)
    finally:
        runtime.shutdown()
    with factory() as db:
        raw = db.scalar(text("SELECT job_state FROM apscheduler_jobs WHERE id='outbox-recovery'"))
        state = json.loads(raw)
        state["next_run_time"] = missed_time.isoformat()
        db.execute(
            text(
                "UPDATE apscheduler_jobs SET next_run_time=:when, job_state=:state "
                "WHERE id='outbox-recovery'"
            ),
            {"when": missed_time.timestamp(), "state": json.dumps(state)},
        )
        db.commit()
    occurred = Event()

    def command(factory, config, operation, **kwargs):
        if operation == "alert_dispatch":
            occurred.set()
        return {}

    monkeypatch.setattr("app.scheduler.runtime.run_operation", command)
    runtime = SchedulerRuntime(factory, config)
    try:
        assert occurred.wait(3), "Stored missed occurrence should resume on restart"
        assert runtime.scheduler.get_job("outbox-recovery").next_run_time > utcnow()
    finally:
        runtime.shutdown()


def test_invalid_dispatch_scopes_are_durable_safe_failures(client, admin):
    factory, config = client.app.state.session_factory, client.app.state.config
    for operation, kwargs, error in [
        ("screen_party", {"party_id": str(uuid4())}, "party_not_found"),
        ("screen_party", {}, "party_not_found"),
        ("source_sync", {"source_id": str(uuid4())}, "source_not_found"),
    ]:
        result = run_operation(
            factory, config, operation, actor_user_id=admin["user"]["id"], **kwargs
        )
        assert result["status"] == "failed" and result["error_code"] == error
        with factory() as db:
            assert db.get(OperationAttempt, result["attempt_id"]).status == "failed"


def test_common_csl_command_processes_delta_and_repeat_idempotently(client, admin):
    from pathlib import Path

    from app.models import Party, ScreeningRun
    from app.screening.normalize import party_record_hash

    fixture = Path(__file__).resolve().parents[2] / "fixtures" / "csl_sample_v1.json"
    data = json.loads(fixture.read_text())
    entry = data["results"][0] if isinstance(data, dict) else data[0]
    factory, config = client.app.state.session_factory, client.app.state.config
    with factory() as db:
        source = Source(kind="csv", label="Command fixture")
        db.add(source)
        db.flush()
        fields = {"name": entry["name"], "party_type": "company"}
        db.add(
            Party(
                source_id=source.id,
                external_id="command",
                raw={},
                **fields,
                record_hash=party_record_hash(fields),
            )
        )
        db.commit()
    result = run_operation(
        factory, config, "csl_refresh", csl_file=fixture, actor_user_id=admin["user"]["id"]
    )
    assert result["status"] == "success" and result["outcome"]["changed"] is True
    assert len(result["outcome"]["run_ids"]) == 1
    with factory() as db:
        run = db.get(ScreeningRun, result["outcome"]["run_ids"][0])
        assert run.finished_at and run.parties_screened == 1
    result = run_operation(
        factory, config, "csl_refresh", csl_file=fixture, actor_user_id=admin["user"]["id"]
    )
    assert result["status"] == "success" and result["outcome"]["changed"] is False
    assert result["outcome"]["run_ids"] == []


def test_cached_lease_identity_does_not_bypass_fence(client, admin):
    factory = client.app.state.session_factory
    with pytest.raises(OperationLeaseLostError):
        with protected_operation(factory, "screen_all") as execution:
            with operation_session(factory, execution) as db:
                execution.check_fence(db)
                db.rollback()
                with factory() as other:
                    other.scalar(select(OperationLease)).fence += 1
                    other.commit()
                db.add(Setting(key="cached-fence-fixture", value="must rollback"))
                db.commit()
    with factory() as db:
        assert db.scalar(select(Setting).where(Setting.key == "cached-fence-fixture")) is None
