"""Shared persistent overlap protection for scheduler, API and CLI operations."""

import hashlib
import os
import subprocess
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import event, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.audit import audit
from app.models import User, utcnow
from app.models.monitoring import OperationAttempt, OperationLease

LEASE_SECONDS = 300
HEARTBEAT_SECONDS = 20
_ACTIVE: set[str] = set()
_ACTIVE_LOCK = threading.Lock()


class OperationBusyError(RuntimeError):
    pass


class OperationAuthorityError(ValueError):
    pass


class OperationLeaseLostError(RuntimeError):
    pass


def lease_key(operation: str, source_id=None):
    if operation in {"screen_all", "screen_party", "screen_changed"}:
        return "screening"
    if operation in {
        "source_sync",
        "source_import",
        "source_disconnect",
        "qbo_oauth_start",
        "qbo_oauth_callback",
        "bc_companies",
        "bc_connect",
        "bc_subscriptions",
    }:
        if not source_id:
            raise ValueError("Choose a source")
        return f"source-sync:{source_id}"
    return operation.replace("_", "-")


def _process_identity(pid):
    try:
        proc = __import__("pathlib").Path(f"/proc/{pid}/stat")
        if proc.exists():
            value = proc.read_text().rsplit(")", 1)[1].split()[19]
            boot = __import__("pathlib").Path("/proc/sys/kernel/random/boot_id")
            if boot.exists():
                value += ":" + boot.read_text().strip()
        else:
            value = subprocess.check_output(
                ["ps", "-o", "lstart=", "-p", str(pid)], text=True, stderr=subprocess.DEVNULL
            ).strip()
        return hashlib.sha256(value.encode()).hexdigest()[:16] if value else None
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def _owner_alive(row):
    if row.owner_pid is None:
        return False
    if row.owner_pid == os.getpid():
        with _ACTIVE_LOCK:
            return row.owner_id in _ACTIVE
    identity = _process_identity(row.owner_pid)
    if not identity or not row.owner_id or row.owner_id.split(":", 1)[0] != identity:
        return False
    try:
        os.kill(row.owner_pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def attempt_payload(row: OperationAttempt):
    return {
        "id": row.id,
        "attempt_id": row.id,
        "operation": row.operation,
        "source_id": row.source_id,
        "party_id": row.party_id,
        "status": row.status,
        "created_at": row.created_at,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "error_code": row.error_code,
        "outcome": row.outcome,
    }


def validate_parameters(operation, parameters):
    # Durable commands contain only fixed, nonsecret execution options.
    if set(parameters) - {
        "csl_file",
        "refresh",
        "batch_id",
        "recipient",
        "force_retry",
        "mode",
        "defer_if_busy",
    }:
        raise ValueError("Unsupported operation parameters")
    if "mode" in parameters and (
        operation != "source_sync"
        or not isinstance(parameters["mode"], str)
        or parameters["mode"] not in {"incremental", "full"}
    ):
        raise ValueError("Choose incremental or full sync")
    if "defer_if_busy" in parameters and (
        operation not in {"source_sync", "source_disconnect", "bc_subscriptions"}
        or type(parameters["defer_if_busy"]) is not bool
    ):
        raise ValueError("Unsupported overlap behavior")


def request_attempt(
    factory, operation, *, source_id=None, party_id=None, parameters=None, actor_user_id=None
):
    # Only fixed command options belong here. Never allow credential/body payloads.
    parameters = parameters or {}
    validate_parameters(operation, parameters)
    with factory() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        if actor_user_id:
            actor = db.get(User, actor_user_id)
            if actor is None or not actor.active or actor.role != "admin":
                raise OperationAuthorityError("Active Admin role required")
        db.info["actor_user_id"] = actor_user_id
        row = OperationAttempt(
            operation=operation,
            source_id=source_id,
            party_id=party_id,
            parameters=parameters,
            actor_user_id=actor_user_id,
        )
        db.add(row)
        db.flush()
        audit(
            db,
            "monitoring.requested",
            "operation_attempts",
            row.id,
            after={"operation": operation, "source_id": source_id, "party_id": party_id},
        )
        db.commit()
        return row.id


@dataclass
class OperationExecution:
    factory: Callable[[], Session]
    attempt_id: str
    key: str
    owner_id: str
    fence: int
    outcome: dict = field(default_factory=dict)
    lost: threading.Event = field(default_factory=threading.Event)

    def complete(self, outcome=None):
        self.outcome = outcome or {}

    def check_fence(self, db):
        row = db.scalar(
            select(OperationLease)
            .where(OperationLease.key == self.key)
            .execution_options(populate_existing=True)
        )
        if row is None or row.owner_id != self.owner_id or row.fence != self.fence:
            self.lost.set()
            raise OperationLeaseLostError(
                "Operation ownership changed; completion was not recorded"
            )


def _heartbeat(execution, stop):
    while not stop.wait(HEARTBEAT_SECONDS):
        try:
            with execution.factory() as db:
                db.execute(text("BEGIN IMMEDIATE"))
                execution.check_fence(db)
                row = db.scalar(select(OperationLease).where(OperationLease.key == execution.key))
                row.heartbeat_at = utcnow()
                row.expires_at = row.heartbeat_at + timedelta(seconds=LEASE_SECONDS)
                db.commit()
        except OperationalError:
            # A screening writer can hold SQLite longer than the heartbeat interval.
            # A live owner must not be stolen solely because that write blocks us.
            continue
        except OperationLeaseLostError:
            return


@contextmanager
def protected_operation(
    factory,
    operation,
    *,
    source_id=None,
    party_id=None,
    actor_user_id=None,
    parameters=None,
    attempt_id=None,
):
    if attempt_id is None:
        attempt_id = request_attempt(
            factory,
            operation,
            source_id=source_id,
            party_id=party_id,
            parameters=parameters,
            actor_user_id=actor_user_id,
        )
    key = lease_key(operation, source_id)
    owner_id = f"{_process_identity(os.getpid())}:{uuid4()}"
    execution = None
    try:
        with factory() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            attempt = db.get(OperationAttempt, attempt_id)
            if attempt is None or attempt.status != "queued":
                raise OperationBusyError("This operation was already claimed")
            if attempt.actor_user_id:
                actor = db.get(User, attempt.actor_user_id)
                if actor is None or not actor.active or actor.role != "admin":
                    attempt.status, attempt.error_code = "failed", "actor_no_longer_admin"
                    attempt.finished_at = utcnow()
                    audit(
                        db,
                        "monitoring.failed",
                        "operation_attempts",
                        attempt.id,
                        after={"error_code": attempt.error_code},
                    )
                    db.commit()
                    raise OperationAuthorityError("Active Admin role required")
            db.info["actor_user_id"] = attempt.actor_user_id
            row = db.scalar(select(OperationLease).where(OperationLease.key == key))
            if row and row.owner_id and _owner_alive(row):
                deferred = attempt.parameters.get("defer_if_busy") is True
                # Receipts and their follow-up polls commit together. A live source
                # owner must not discard that later poll merely because it overlaps.
                if not deferred:
                    attempt.status = "skipped"
                    attempt.finished_at = utcnow()
                first_overlap = attempt.error_code != "operation_already_running"
                attempt.error_code = "operation_already_running"
                if first_overlap:
                    audit(
                        db,
                        "monitoring.overlap_deferred" if deferred else "monitoring.overlap_skipped",
                        "operation_attempts",
                        attempt.id,
                        after={"operation": operation, "resource": key},
                    )
                db.commit()
                raise OperationBusyError("This operation is already running")
            if row is None:
                row = OperationLease(key=key)
                db.add(row)
                db.flush()
            else:
                for previous in db.scalars(
                    select(OperationAttempt).where(OperationAttempt.status == "running")
                ):
                    if lease_key(previous.operation, previous.source_id) == key:
                        previous.status, previous.error_code = "interrupted", "owner_stopped"
                        previous.finished_at = utcnow()
                        # A competing new claim can precede the recovery job.
                        # Preserve the interrupted request's exact durable intent;
                        # an incoming incremental poll cannot replace a lost full poll.
                        from app.scheduler.service import OPERATIONS

                        replacement = None
                        if previous.operation in OPERATIONS:
                            replacement = OperationAttempt(
                                operation=previous.operation,
                                source_id=previous.source_id,
                                party_id=previous.party_id,
                                actor_user_id=previous.actor_user_id,
                                parameters=dict(previous.parameters),
                            )
                            db.add(replacement)
                            db.flush()
                            audit(
                                db,
                                "monitoring.recovery_queued",
                                "operation_attempts",
                                replacement.id,
                                after={
                                    "previous_attempt_id": previous.id,
                                    "operation": previous.operation,
                                },
                            )
                        audit(
                            db,
                            "monitoring.interrupted",
                            "operation_attempts",
                            previous.id,
                            after={
                                "error_code": "owner_stopped",
                                "retry_attempt_id": replacement.id if replacement else None,
                            },
                        )
            row.owner_id, row.owner_pid = owner_id, os.getpid()
            row.fence += 1
            row.heartbeat_at = utcnow()
            row.expires_at = row.heartbeat_at + timedelta(seconds=LEASE_SECONDS)
            attempt.status, attempt.started_at, attempt.error_code = "running", utcnow(), None
            audit(
                db,
                "monitoring.started",
                "operation_attempts",
                attempt.id,
                after={"operation": operation, "resource": key, "fence": row.fence},
            )
            with _ACTIVE_LOCK:
                _ACTIVE.add(owner_id)
            execution = OperationExecution(factory, attempt_id, key, owner_id, row.fence)
            db.commit()
        with _ACTIVE_LOCK:
            _ACTIVE.add(owner_id)
        stop = threading.Event()
        heartbeat = threading.Thread(target=_heartbeat, args=(execution, stop), daemon=True)
        heartbeat.start()
        try:
            yield execution
            if execution.lost.is_set():
                raise OperationLeaseLostError("Operation ownership changed")
            with factory() as db:
                db.execute(text("BEGIN IMMEDIATE"))
                execution.check_fence(db)
                attempt = db.get(OperationAttempt, attempt_id)
                attempt.status, attempt.finished_at, attempt.outcome = (
                    "success",
                    utcnow(),
                    execution.outcome,
                )
                db.info["actor_user_id"] = attempt.actor_user_id
                audit(
                    db,
                    "monitoring.succeeded",
                    "operation_attempts",
                    attempt.id,
                    after={"operation": operation, "outcome": execution.outcome},
                )
                db.commit()
        except Exception:
            with factory() as db:
                db.execute(text("BEGIN IMMEDIATE"))
                attempt = db.get(OperationAttempt, attempt_id)
                if attempt and attempt.status == "running":
                    attempt.status, attempt.finished_at, attempt.error_code = (
                        "failed",
                        utcnow(),
                        "operation_failed",
                    )
                    attempt.outcome = execution.outcome
                    db.info["actor_user_id"] = attempt.actor_user_id
                    audit(
                        db,
                        "monitoring.failed",
                        "operation_attempts",
                        attempt.id,
                        after={
                            "operation": operation,
                            "error_code": attempt.error_code,
                            "outcome": execution.outcome,
                        },
                    )
                    db.commit()
            raise
        finally:
            stop.set()
            heartbeat.join(timeout=1)
    except OperationalError:
        raise OperationBusyError("The database is busy; retry the operation") from None
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.discard(owner_id)
        if execution is not None:
            try:
                with factory() as db:
                    db.execute(text("BEGIN IMMEDIATE"))
                    row = db.scalar(select(OperationLease).where(OperationLease.key == key))
                    if row and row.owner_id == owner_id and row.fence == execution.fence:
                        row.owner_id, row.owner_pid, row.expires_at = None, None, None
                        db.commit()
            except OperationalError:
                # The persisted expiry plus dead-owner recovery remains available.
                pass


@event.listens_for(Session, "before_commit")
def verify_operation_commit(db):
    execution = db.info.get("operation_execution")
    if execution is not None:
        execution.check_fence(db)


def operation_session(factory, execution, actor_user_id=None):
    db = factory()
    db.info["operation_execution"] = execution
    db.info["actor_user_id"] = actor_user_id
    return db


def operation_is_busy(db, operation, source_id=None):
    row = db.scalar(
        select(OperationLease).where(OperationLease.key == lease_key(operation, source_id))
    )
    return bool(row and row.owner_id and _owner_alive(row))
