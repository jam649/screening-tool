"""Fixed durable commands shared by schedules, HTTP actions and the operator CLI."""

from pathlib import Path

from sqlalchemy import select

from app.audit import audit
from app.models import OperationAttempt, OperationLease, Party, Source, User, utcnow
from app.scheduler.leases import (
    OperationAuthorityError,
    OperationBusyError,
    _owner_alive,
    attempt_payload,
    lease_key,
    operation_session,
    protected_operation,
    request_attempt,
)

OPERATIONS = {
    "csl_refresh",
    "screen_all",
    "screen_party",
    "screen_changed",
    "source_sync",
    "monitor_cycle",
    "morning_report",
    "alert_dispatch",
    "retention_apply",
}
SOURCE_HANDLERS: dict = {}


def register_source_handler(kind, handler):
    if kind not in {"qbo", "bc"}:
        raise ValueError("Unsupported connector")
    SOURCE_HANDLERS[kind] = handler


def enqueue_operation(
    factory,
    config,
    operation,
    *,
    source_id=None,
    party_id=None,
    csl_file=None,
    actor_user_id=None,
    parameters=None,
):
    if operation not in OPERATIONS:
        raise ValueError("Unsupported operation")
    failure = None
    with factory() as db:
        if operation == "screen_party" and (not party_id or db.get(Party, party_id) is None):
            failure = "party_not_found"
        elif operation == "source_sync" and (not source_id or db.get(Source, source_id) is None):
            failure = "source_not_found"
    if failure:
        from sqlalchemy import text

        with factory() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            if actor_user_id:
                actor = db.get(User, actor_user_id)
                if actor is None or not actor.active or actor.role != "admin":
                    raise OperationAuthorityError("Active Admin role required")
            row = OperationAttempt(
                operation=operation,
                actor_user_id=actor_user_id,
                status="failed",
                error_code=failure,
                finished_at=utcnow(),
            )
            db.add(row)
            db.flush()
            db.info["actor_user_id"] = actor_user_id
            audit(
                db,
                "monitoring.failed",
                "operation_attempts",
                row.id,
                after={"operation": operation, "error_code": failure},
            )
            db.commit()
            return {"attempt_id": row.id, "operation": operation, "status": "failed"}
    values = dict(parameters or {})
    if csl_file is not None:
        values["csl_file"] = str(csl_file)
    attempt_id = request_attempt(
        factory,
        operation,
        source_id=source_id,
        party_id=party_id,
        actor_user_id=actor_user_id,
        parameters=values,
    )
    return {"attempt_id": attempt_id, "operation": operation, "status": "queued"}


def _screen_pending(factory, config, actor_user_id=None):
    return run_operation(factory, config, "screen_changed", actor_user_id=actor_user_id)


def _execute(factory, config, attempt, execution):
    operation, parameters = attempt.operation, attempt.parameters
    actor = attempt.actor_user_id
    if operation in {"screen_all", "screen_party", "screen_changed"}:
        from app.screening.engine import (
            ScreeningTasksFailedError,
            process_pending_tasks,
            run_screening,
        )

        with operation_session(factory, execution, actor) as db:
            if operation == "screen_changed":
                try:
                    runs = process_pending_tasks(db)
                except ScreeningTasksFailedError as failure:
                    execution.complete(
                        {
                            "run_ids": [run.id for run in failure.completed_runs],
                            "failed_task_ids": [row["task_id"] for row in failure.failures],
                        }
                    )
                    raise
            else:
                runs = [
                    run_screening(
                        db, party_ids=[attempt.party_id] if operation == "screen_party" else None
                    )
                ]
            return {"run_ids": [run.id for run in runs]}
    if operation == "csl_refresh":
        from app.csl.service import configured_api_key, refresh_csl

        with operation_session(factory, execution, actor) as db:
            result = refresh_csl(
                db,
                file=Path(parameters["csl_file"]) if parameters.get("csl_file") else None,
                api_key=configured_api_key(db, config),
            )
            outcome = {
                "fetch_id": result.fetch.id,
                "version_id": result.version.id,
                "changed": result.changed,
                "pending_task_id": result.pending_task_id,
                "closed_hits": result.closed_hits,
            }
        # The source snapshot commits before the separately leased delta consumer.
        execution.complete(outcome)
        screening = _screen_pending(factory, config, actor)
        outcome.update(
            {
                "run_ids": screening.get("outcome", {}).get("run_ids", []),
                "failed_task_ids": screening.get("outcome", {}).get("failed_task_ids", []),
                "screening_attempt_id": screening["attempt_id"],
                "screening_status": screening["status"],
            }
        )
        execution.complete(outcome)
        if screening["status"] == "failed":
            raise RuntimeError("Pending screening failed after source application")
        return outcome
    if operation == "source_sync":
        with operation_session(factory, execution, actor) as db:
            source = db.get(Source, attempt.source_id)
            if source is None:
                raise ValueError("Source not found")
            handler = SOURCE_HANDLERS.get(source.kind)
            if handler is None:
                raise ValueError("This connector does not support scheduled sync yet")
            result = handler(db, config, source)
            safe = {
                key: value
                for key, value in result.items()
                if key
                in {"added", "changed", "deleted", "unchanged", "record_count", "pending_task_id"}
            }
            outcome = {"source_id": source.id, **safe}
        execution.complete(outcome)
        screening = _screen_pending(factory, config, actor)
        outcome.update(
            {
                "run_ids": screening.get("outcome", {}).get("run_ids", []),
                "failed_task_ids": screening.get("outcome", {}).get("failed_task_ids", []),
                "screening_attempt_id": screening["attempt_id"],
                "screening_status": screening["status"],
            }
        )
        execution.complete(outcome)
        if screening["status"] == "failed":
            raise RuntimeError("Pending screening failed after source application")
        return outcome
    if operation == "monitor_cycle":
        outcomes = []
        if parameters.get("refresh", True):
            outcomes.append(
                run_operation(
                    factory,
                    config,
                    "csl_refresh",
                    actor_user_id=actor,
                    csl_file=parameters.get("csl_file"),
                )
            )
        with factory() as db:
            source_ids = list(db.scalars(select(Source.id).where(Source.kind.in_(["qbo", "bc"]))))
        outcomes.extend(
            run_operation(factory, config, "source_sync", source_id=sid, actor_user_id=actor)
            for sid in source_ids
        )
        outcomes.append(run_operation(factory, config, "screen_changed", actor_user_id=actor))
        outcome = {
            "attempt_ids": [row["attempt_id"] for row in outcomes],
            "failed_operations": sum(row["status"] == "failed" for row in outcomes),
        }
        execution.complete(outcome)
        if outcome["failed_operations"]:
            raise RuntimeError("Monitoring step failed")
        return outcome
    if operation == "morning_report":
        from sqlalchemy import text

        from app.notifications.service import dispatch_outbox
        from app.reports.morning import enqueue_morning_report

        with operation_session(factory, execution, actor) as db:
            db.execute(text("BEGIN IMMEDIATE"))
            batch = enqueue_morning_report(db, config)
            batch_id = batch.id
            db.commit()
        return {
            "batch_id": batch_id,
            "delivery": dispatch_outbox(factory, config, batch_id=batch_id, force=True),
        }
    if operation == "alert_dispatch":
        from app.notifications.service import dispatch_outbox

        return {
            "delivery": dispatch_outbox(
                factory,
                config,
                batch_id=parameters.get("batch_id"),
                retry_uncertain=parameters.get("force_retry", False),
                force=bool(parameters.get("batch_id")),
                execution=execution,
            )
        }
    if operation == "retention_apply":
        from app.retention.service import apply_retention

        with operation_session(factory, execution, actor) as db:
            retention_result = apply_retention(db)
            # No customer identifiers or timestamps are needed in operation summaries.
            return {key: retention_result[key] for key in ("enabled", "hits", "dispositions")}
    raise ValueError("Unsupported operation")


def dispatch_attempt(factory, config, attempt_id):
    with factory() as db:
        attempt = db.get(OperationAttempt, attempt_id)
        if attempt is None:
            raise ValueError("Operation not found")
    try:
        with protected_operation(
            factory,
            attempt.operation,
            source_id=attempt.source_id,
            party_id=attempt.party_id,
            attempt_id=attempt.id,
        ) as execution:
            execution.complete(_execute(factory, config, attempt, execution))
    except (OperationBusyError, OperationAuthorityError):
        pass
    except Exception:
        # The durable attempt has a safe failure code; never expose transport or input text.
        pass
    with factory() as db:
        row = db.get(OperationAttempt, attempt_id)
        payload = attempt_payload(row)
    # Delivery happens only after screening/import/refresh transactions and leases end.
    if attempt.operation in {
        "screen_all",
        "screen_party",
        "screen_changed",
        "csl_refresh",
        "source_sync",
        "monitor_cycle",
    }:
        from app.notifications.service import dispatch_outbox

        dispatch_outbox(factory, config)
    return payload


def run_operation(factory, config, operation, **kwargs):
    queued = enqueue_operation(factory, config, operation, **kwargs)
    return dispatch_attempt(factory, config, queued["attempt_id"])


def recover_interrupted(factory):
    from sqlalchemy import text

    recovered = []
    with factory() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        for row in list(
            db.scalars(select(OperationAttempt).where(OperationAttempt.status == "running"))
        ):
            key = lease_key(row.operation, row.source_id)
            lease = db.scalar(select(OperationLease).where(OperationLease.key == key))
            if lease and lease.owner_id and _owner_alive(lease):
                continue
            row.status, row.finished_at, row.error_code = "interrupted", utcnow(), "owner_stopped"
            replacement = None
            if row.operation in OPERATIONS:
                replacement = OperationAttempt(
                    operation=row.operation,
                    source_id=row.source_id,
                    party_id=row.party_id,
                    actor_user_id=row.actor_user_id,
                    parameters=dict(row.parameters),
                )
                db.add(replacement)
                db.flush()
                recovered.append(replacement.id)
            if replacement:
                audit(
                    db,
                    "monitoring.recovery_queued",
                    "operation_attempts",
                    replacement.id,
                    after={"previous_attempt_id": row.id, "operation": row.operation},
                )
            audit(
                db,
                "monitoring.interrupted",
                "operation_attempts",
                row.id,
                after={
                    "error_code": "owner_stopped",
                    "retry_attempt_id": replacement.id if replacement else None,
                },
            )
        db.commit()
    return recovered


def dispatch_pending(factory, config):
    recover_interrupted(factory)
    with factory() as db:
        ids = list(
            db.scalars(
                select(OperationAttempt.id)
                .where(OperationAttempt.status == "queued")
                .order_by(OperationAttempt.created_at)
            )
        )
    return [dispatch_attempt(factory, config, attempt_id) for attempt_id in ids]
