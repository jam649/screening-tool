"""Validated SMTP configuration and frozen report/delivery status."""

from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from app.api.deps import DB, AdminUser, CurrentUser
from app.api.review import begin_write
from app.api.users import validate_admin
from app.audit import audit
from app.models import NotificationBatch, NotificationDelivery, Source
from app.notifications.service import (
    batch_payload,
    dispatch_outbox,
    enqueue_test,
    notification_health,
)
from app.notifications.settings import (
    NotificationChanges,
    NotificationConfigurationError,
    canonical_email,
    notification_settings_payload,
    save_notification_settings,
)
from app.reports.metrics import report_boundary
from app.reports.morning import enqueue_morning_report
from app.scheduler.runtime import current_runtime
from app.scheduler.service import enqueue_bc_subscriptions

router = APIRouter()


class TestMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipient: str = Field(max_length=320)

    @field_validator("recipient")
    @classmethod
    def email(cls, value):
        return canonical_email(value)


class RetryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retry_uncertain: bool = False


@router.get("/settings/notifications")
def get_settings(request: Request, user: AdminUser, db: DB):
    return notification_settings_payload(db, request.app.state.config)


@router.patch("/settings/notifications")
def patch_settings(body: NotificationChanges, request: Request, user: AdminUser, db: DB):
    actor_id = user.id
    begin_write(db)
    validate_admin(db, actor_id)
    before = notification_settings_payload(db, request.app.state.config)
    try:
        save_notification_settings(db, request.app.state.config, body)
    except NotificationConfigurationError as exc:
        raise HTTPException(422, str(exc)) from None
    after = notification_settings_payload(db, request.app.state.config)
    if before["public_base_url"] != after["public_base_url"]:
        for source in db.scalars(select(Source).where(Source.kind == "bc")):
            if not source.credentials_enc or not (
                source.config.get("webhooks_enabled")
                or source.config.get("cleanup_status") in {"pending", "failed"}
            ):
                continue
            if urlsplit(after["public_base_url"]).scheme != "https":
                source.config = {
                    **source.config,
                    "webhooks_enabled": False,
                    "cleanup_status": "pending",
                }
            attempt = enqueue_bc_subscriptions(db, source.id)
            audit(
                db,
                "bc.webhook_address_changed",
                "sources",
                source.id,
                after={
                    "attempt_id": attempt.id,
                    "webhooks_enabled": source.config.get("webhooks_enabled", False),
                },
            )
    # Payload contains only public settings and configured flags, never secret input.
    audit(db, "notifications.settings_changed", "settings", before=before, after=after)
    db.commit()
    runtime = current_runtime()
    if runtime:
        runtime.reconcile()
        if before["public_base_url"] != after["public_base_url"]:
            runtime.wake()
    return after


def _schedule_delivery(request, background, batch_id, *, retry_uncertain=False):
    background.add_task(
        dispatch_outbox,
        request.app.state.session_factory,
        request.app.state.config,
        batch_id=batch_id,
        force=True,
        retry_uncertain=retry_uncertain,
    )


@router.post("/notifications/test", status_code=202)
def test(body: TestMessage, request: Request, background: BackgroundTasks, user: AdminUser, db: DB):
    actor_id = user.id
    begin_write(db)
    validate_admin(db, actor_id)
    try:
        batch = enqueue_test(db, request.app.state.config, body.recipient)
    except NotificationConfigurationError as exc:
        raise HTTPException(422, str(exc)) from None
    result = batch_payload(db, batch)
    db.commit()
    _schedule_delivery(request, background, batch.id)
    return result


@router.post("/reports/morning/send", status_code=202)
def send_report(request: Request, background: BackgroundTasks, user: AdminUser, db: DB):
    actor_id = user.id
    begin_write(db)
    validate_admin(db, actor_id)
    try:
        batch = enqueue_morning_report(db, request.app.state.config)
    except NotificationConfigurationError as exc:
        raise HTTPException(422, str(exc)) from None
    result = batch_payload(db, batch)
    db.commit()
    _schedule_delivery(request, background, batch.id)
    return result


def status_payload(db, config, *, morning=False):
    query = select(NotificationBatch).order_by(NotificationBatch.created_at.desc()).limit(50)
    if morning:
        query = query.where(NotificationBatch.kind == "morning")
    return {
        "batches": [batch_payload(db, row) for row in db.scalars(query)],
        "health": notification_health(db, config),
    }


@router.get("/reports/morning/status")
def report_status(request: Request, user: CurrentUser, db: DB):
    return {
        "last_report_at": report_boundary(db),
        **status_payload(db, request.app.state.config, morning=True),
    }


@router.get("/notifications/status")
def notification_status(request: Request, user: CurrentUser, db: DB):
    return status_payload(db, request.app.state.config)


@router.post("/notifications/batches/{batch_id}/retry", status_code=202)
def retry(
    batch_id: str,
    body: RetryMessage,
    request: Request,
    background: BackgroundTasks,
    user: AdminUser,
    db: DB,
):
    actor_id = user.id
    begin_write(db)
    validate_admin(db, actor_id)
    from app.scheduler.leases import operation_is_busy

    if operation_is_busy(db, "alert_dispatch"):
        raise HTTPException(409, "Email delivery is running; wait for its recorded outcome")
    batch = db.get(NotificationBatch, batch_id)
    if batch is None:
        raise HTTPException(404, "Notification batch not found")
    deliveries = list(
        db.scalars(select(NotificationDelivery).where(NotificationDelivery.batch_id == batch.id))
    )
    uncertain = any(row.status in {"sending", "uncertain"} for row in deliveries)
    if uncertain and not body.retry_uncertain:
        raise HTTPException(
            409, "Delivery may already have succeeded; explicitly authorize uncertain retry"
        )
    if body.retry_uncertain:
        for row in deliveries:
            if row.status in {"sending", "uncertain"}:
                row.status = "failed"
                row.last_error_code = "uncertain_retry_authorized"
    audit(
        db,
        "notifications.retry_requested",
        "notification_batches",
        batch.id,
        after={"retry_uncertain": body.retry_uncertain},
    )
    db.flush()
    result = batch_payload(db, batch)
    db.commit()
    _schedule_delivery(request, background, batch.id, retry_uncertain=body.retry_uncertain)
    return result
