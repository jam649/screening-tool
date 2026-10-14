"""Authenticated durable operation and schedule administration."""

from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from sqlalchemy import select

from app.api.deps import DB, AdminUser, CurrentUser
from app.api.review import begin_write
from app.api.users import validate_admin
from app.audit import audit
from app.models import OperationAttempt, Setting
from app.notifications.settings import validate_time
from app.scheduler.leases import attempt_payload
from app.scheduler.runtime import current_runtime, monitoring_settings
from app.scheduler.service import dispatch_attempt, enqueue_operation

router = APIRouter()


class MonitoringChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")
    csl_fetch_time: str | None = None
    source_poll_minutes: StrictInt | None = Field(default=None, ge=1, le=1440)
    retention_weekday: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] | None = None
    retention_time: str | None = None

    @field_validator("csl_fetch_time", "retention_time")
    @classmethod
    def time(cls, value):
        return validate_time(value) if value is not None else value


def operation_health(db):
    # A newer success for the same operation/resource resolves its failure banner.
    health = []
    seen = set()
    for row in db.scalars(select(OperationAttempt).order_by(OperationAttempt.created_at.desc())):
        resource = (row.operation, row.source_id, row.party_id)
        if resource in seen or row.status in {"queued", "running", "skipped"}:
            continue
        seen.add(resource)
        if row.status in {"failed", "interrupted"}:
            health.append(
                {
                    "kind": "operation_failed",
                    "message": f"{row.operation.replace('_', ' ')} needs attention.",
                    "attempt_id": row.id,
                    "operation": row.operation,
                    "error_code": row.error_code,
                }
            )
    return health


@router.get("/settings/monitoring")
def get_monitoring_settings(request: Request, user: AdminUser, db: DB):
    return monitoring_settings(db, request.app.state.config)


@router.patch("/settings/monitoring")
def patch_monitoring_settings(body: MonitoringChanges, request: Request, user: AdminUser, db: DB):
    actor_id = user.id
    begin_write(db)
    validate_admin(db, actor_id)
    before = monitoring_settings(db, request.app.state.config)
    changes = body.model_dump(exclude_none=True)
    for key, value in changes.items():
        row = db.scalar(select(Setting).where(Setting.key == key))
        if row:
            row.value, row.encrypted = value, False
        else:
            db.add(Setting(key=key, value=value))
    audit(
        db,
        "monitoring.schedule_changed",
        "settings",
        after={**changes},
        before={key: before[key] for key in changes},
    )
    db.commit()
    runtime = current_runtime()
    if runtime:
        runtime.reconcile()
    return monitoring_settings(db, request.app.state.config)


@router.get("/monitoring/status")
def status(request: Request, user: CurrentUser, db: DB):
    runtime = current_runtime()
    return {
        "enabled": request.app.state.config.scheduler_enabled,
        "schedules": runtime.schedules() if runtime else [],
        "operations": [
            attempt_payload(row)
            for row in db.scalars(
                select(OperationAttempt).order_by(OperationAttempt.created_at.desc()).limit(50)
            )
        ],
        "health": operation_health(db),
    }


@router.post("/monitoring/cycle", status_code=202)
def cycle(request: Request, background: BackgroundTasks, user: AdminUser, db: DB):
    result = enqueue_operation(
        request.app.state.session_factory,
        request.app.state.config,
        "monitor_cycle",
        actor_user_id=user.id,
    )
    background.add_task(
        dispatch_attempt,
        request.app.state.session_factory,
        request.app.state.config,
        result["attempt_id"],
    )
    return result


@router.get("/monitoring/operations/{attempt_id}")
def operation(attempt_id: str, user: CurrentUser, db: DB):
    row = db.get(OperationAttempt, attempt_id)
    if row is None:
        raise HTTPException(404, "Operation not found")
    return attempt_payload(row)
