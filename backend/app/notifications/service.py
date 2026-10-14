"""Immutable encrypted mail batches and per-recipient retry without network locks."""

from copy import deepcopy
from datetime import timedelta
from html import escape
from uuid import uuid4

from sqlalchemy import select, text

from app.audit import audit
from app.config import DISCLAIMER, PRODUCT_NAME, Config
from app.models import Hit, Setting, User, UserInvitation, utcnow
from app.models.monitoring import NotificationBatch, NotificationDelivery
from app.notifications.settings import (
    NotificationConfigurationError,
    canonical_email,
    load_smtp_settings,
    public_settings,
)
from app.notifications.transport import DeliveryFailure, send_message
from app.scheduler.leases import OperationBusyError, protected_operation
from app.security import decrypt_json, digest, encrypt_json


def session_config(db):
    config = db.info.get("config")
    return config if config is not None else Config()


def create_batch(
    db,
    config,
    *,
    kind,
    dedupe_key,
    payload,
    recipients,
    run_id=None,
    invitation_id=None,
    window_start=None,
    window_end=None,
):
    existing = db.scalar(
        select(NotificationBatch).where(NotificationBatch.dedupe_key == dedupe_key)
    )
    if existing:
        return existing
    batch = NotificationBatch(
        id=str(uuid4()),
        kind=kind,
        dedupe_key=dedupe_key,
        payload_enc=encrypt_json(payload, config),
        run_id=run_id,
        invitation_id=invitation_id,
        window_start=window_start,
        window_end=window_end or utcnow(),
    )
    db.add(batch)
    db.flush()
    for recipient in dict.fromkeys(canonical_email(r) for r in recipients):
        db.add(
            NotificationDelivery(
                batch_id=batch.id,
                recipient=recipient,
                message_id=f"<{batch.id}.{digest(recipient)[:20]}@screening-tool.invalid>",
            )
        )
    audit(
        db,
        "notifications.queued",
        "notification_batches",
        batch.id,
        after={
            "kind": kind,
            "run_id": run_id,
            "invitation_id": invitation_id,
            "recipient_count": len(set(recipients)),
        },
    )
    db.flush()
    return batch


def product_name(db):
    row = db.scalar(select(Setting).where(Setting.key == "display_name"))
    return row.value if row and not row.encrypted and isinstance(row.value, str) else PRODUCT_NAME


def html_body(lines):
    return "<html><body>" + "".join(f"<p>{escape(line)}</p>" for line in lines) + "</body></html>"


def enqueue_likely_alert(db, run) -> None:
    values = public_settings(db)
    if not values["likely_alerts_enabled"]:
        return
    hits = list(
        db.scalars(
            select(Hit)
            .where(Hit.run_id == run.id, Hit.bucket == "likely")
            .order_by(Hit.final_confidence.desc(), Hit.id)
        )
    )
    if not hits:
        return
    if not values["report_recipients"]:
        audit(
            db,
            "notifications.configuration_blocked",
            "screening_runs",
            run.id,
            after={"kind": "likely", "error_code": "recipients_not_configured"},
        )
        return
    from app.api.review import disposition_payload
    from app.models import Disposition

    lines = [
        f"Likely observations from screening run {run.id}",
        f"CSL version: {run.csl_version_id}",
        "These are scoring observations; human decisions and prior history are labeled below.",
    ]
    for hit in hits:
        completed = hit.explanation.get("completed_disposition_id")
        suppressed = hit.explanation.get("suppression_disposition_id")
        prior = hit.explanation.get("prior_disposition_id")
        label = "current review item"
        if suppressed:
            label = "previously marked false positive; suppressed observation"
        elif completed:
            label = "previous human decision retained; repeated observation"
        elif hit.explanation.get("queue_anchor_hit_id"):
            label = "repeated unresolved observation"
        lines.append(
            f"{hit.party_snapshot.get('name', '')} | {hit.entry_snapshot.get('source_list', '')} | "
            f"confidence {hit.final_confidence:g} | {label}"
        )
        for decision_id in dict.fromkeys(d for d in (suppressed, completed, prior) if d):
            decision = db.get(Disposition, decision_id)
            if decision:
                record = disposition_payload(db, decision)
                actor = (record["actor"] or {}).get("display_name") or "system"
                lines.append(
                    f"Original decision: {decision.kind}; by {actor}; "
                    f"{decision.created_at.isoformat()}"
                )
                if decision.note:
                    lines.append(f"Original note: {decision.note}")
    lines.extend([f"Queue: {values['public_base_url']}/queue", DISCLAIMER])
    create_batch(
        db,
        session_config(db),
        kind="likely",
        dedupe_key=f"likely:{run.id}",
        run_id=run.id,
        payload={
            "subject": f"{product_name(db)}: {len(hits)} likely observations",
            "sender": values["smtp_from"],
            "text": "\n\n".join(lines),
            "html": html_body(lines),
            "run_id": run.id,
            "observation_ids": [h.id for h in hits],
        },
        recipients=values["report_recipients"],
    )


def enqueue_invitation(db, config, invitation, token):
    values = public_settings(db)
    if not values["smtp_host"] or not values["smtp_from"] or not values["public_base_url"]:
        return None
    url = f"{values['public_base_url']}/accept-invite#token={token}"
    lines = [
        f"You have been invited to {product_name(db)} as {invitation.role}.",
        f"Accept invitation: {url}",
        f"Expires: {invitation.expires_at.isoformat()}",
        "This link can be used once. If you did not expect this invitation, disregard it.",
    ]
    return create_batch(
        db,
        config,
        kind="invitation",
        dedupe_key=f"invitation:{invitation.id}",
        invitation_id=invitation.id,
        payload={
            "subject": f"{product_name(db)} invitation",
            "sender": values["smtp_from"],
            "text": "\n\n".join(lines),
            "html": html_body(lines),
        },
        recipients=[invitation.email],
    )


def enqueue_test(db, config, recipient):
    values = public_settings(db)
    if not load_smtp_settings(db, config).configured:
        raise NotificationConfigurationError("Configure SMTP host and from address first")
    lines = [
        f"{product_name(db)} SMTP test message.",
        "This message contains no party records.",
        DISCLAIMER,
    ]
    return create_batch(
        db,
        config,
        kind="test",
        dedupe_key=f"test:{uuid4()}",
        payload={
            "subject": f"{product_name(db)}: SMTP test",
            "sender": values["smtp_from"],
            "text": "\n\n".join(lines),
            "html": html_body(lines),
        },
        recipients=[canonical_email(recipient)],
    )


def batch_payload(db, batch):
    deliveries = list(
        db.scalars(
            select(NotificationDelivery)
            .where(NotificationDelivery.batch_id == batch.id)
            .order_by(NotificationDelivery.recipient)
        )
    )
    return {
        "id": batch.id,
        "batch_id": batch.id,
        "kind": batch.kind,
        "status": batch.status,
        "created_at": batch.created_at,
        "completed_at": batch.completed_at,
        "window_start": batch.window_start,
        "window_end": batch.window_end,
        "deliveries": [
            {
                "id": row.id,
                "recipient": row.recipient,
                "status": row.status,
                "attempts": row.attempts,
                "error_code": row.last_error_code,
                "retry_at": row.retry_at,
            }
            for row in deliveries
        ],
    }


def _invitation_valid(db, batch):
    invitation = db.get(UserInvitation, batch.invitation_id)
    if (
        invitation is None
        or invitation.used_at
        or invitation.revoked_at
        or invitation.expires_at <= utcnow()
    ):
        return False
    issuer = db.get(User, invitation.invited_by_user_id)
    return issuer is not None and issuer.active and issuer.role == "admin"


def _finish_batch(db, batch):
    statuses = list(
        db.scalars(
            select(NotificationDelivery.status).where(NotificationDelivery.batch_id == batch.id)
        )
    )
    if statuses and all(status == "sent" for status in statuses):
        batch.status, batch.completed_at = "sent", utcnow()
        if batch.kind == "morning":
            boundary = db.scalar(select(Setting).where(Setting.key == "last_report_at"))
            value = batch.window_end.isoformat()
            if boundary:
                # Never move the boundary backwards when an older retry finishes.
                from datetime import datetime

                old = datetime.fromisoformat(boundary.value.replace("Z", "+00:00"))
                if batch.window_end > old:
                    boundary.value = value
            else:
                db.add(Setting(key="last_report_at", value=value))
        audit(
            db,
            "notifications.batch_sent",
            "notification_batches",
            batch.id,
            after={
                "kind": batch.kind,
                "window_end": batch.window_end.isoformat(),
                "recipient_count": len(statuses),
            },
        )
    elif any(status == "uncertain" for status in statuses):
        batch.status = "uncertain"
    elif any(status == "failed" for status in statuses):
        batch.status = "failed"
    elif statuses and all(status == "cancelled" for status in statuses):
        batch.status = "cancelled"
    else:
        batch.status = "pending"


def dispatch_outbox(
    factory, config, *, batch_id=None, retry_uncertain=False, force=False, execution=None
):
    try:
        from contextlib import nullcontext

        context = (
            nullcontext(execution)
            if execution is not None
            else protected_operation(factory, "alert_dispatch")
        )
        with context as execution:
            with factory() as db:
                query = select(NotificationDelivery.id).where(
                    NotificationDelivery.status.in_(
                        ["pending", "failed", "sending", "uncertain"]
                        if retry_uncertain
                        else ["pending", "failed", "sending"]
                    )
                )
                if batch_id:
                    query = query.where(NotificationDelivery.batch_id == batch_id)
                if not force:
                    query = query.where(
                        (NotificationDelivery.retry_at.is_(None))
                        | (NotificationDelivery.retry_at <= utcnow())
                    )
                ids = list(db.scalars(query.order_by(NotificationDelivery.created_at)))
            counts = {"sent": 0, "failed": 0, "uncertain": 0, "cancelled": 0}
            for delivery_id in ids:
                with factory() as db:
                    db.execute(text("BEGIN IMMEDIATE"))
                    execution.check_fence(db)
                    delivery = db.get(NotificationDelivery, delivery_id)
                    batch = db.get(NotificationBatch, delivery.batch_id)
                    if delivery.status == "sent" or delivery.status == "cancelled":
                        continue
                    if delivery.status == "sending":
                        # Crash after SMTP acceptance has an unknowable outcome.
                        delivery.status, delivery.last_error_code = (
                            "uncertain",
                            "delivery_uncertain",
                        )
                        delivery.finished_at = utcnow()
                        _finish_batch(db, batch)
                        db.commit()
                        counts["uncertain"] += 1
                        continue
                    if batch.kind == "invitation" and not _invitation_valid(db, batch):
                        delivery.status, delivery.last_error_code = (
                            "cancelled",
                            "invitation_no_longer_valid",
                        )
                        _finish_batch(db, batch)
                        db.commit()
                        counts["cancelled"] += 1
                        continue
                    delivery.status, delivery.started_at = "sending", utcnow()
                    delivery.attempts += 1
                    batch.status = "sending"
                    preparation_error = None
                    try:
                        payload = decrypt_json(batch.payload_enc, config)
                        smtp = load_smtp_settings(db, config)
                    except Exception:
                        payload, smtp = {}, None
                        preparation_error = DeliveryFailure("smtp_configuration_invalid")
                    recipient, message_id = delivery.recipient, delivery.message_id
                    effective_payload = deepcopy(payload)
                    effective_payload["sender"] = payload.get("sender") or (
                        smtp.sender if smtp else ""
                    )
                    audit(
                        db,
                        "notifications.delivery_started",
                        "notification_deliveries",
                        delivery.id,
                        after={"batch_id": batch.id, "attempt": delivery.attempts},
                    )
                    db.commit()
                # No session/transaction is alive around SMTP network I/O.
                error = None
                try:
                    if preparation_error:
                        raise preparation_error
                    send_message(
                        smtp, recipient=recipient, payload=effective_payload, message_id=message_id
                    )
                except DeliveryFailure as failure:
                    error = failure
                except Exception:
                    error = DeliveryFailure("delivery_internal_error")
                with factory() as db:
                    db.execute(text("BEGIN IMMEDIATE"))
                    execution.check_fence(db)
                    delivery = db.get(NotificationDelivery, delivery_id)
                    batch = db.get(NotificationBatch, delivery.batch_id)
                    delivery.finished_at = utcnow()
                    if error:
                        delivery.status = "uncertain" if error.uncertain else "failed"
                        delivery.last_error_code = error.code
                        delivery.retry_at = (
                            None
                            if error.uncertain
                            else utcnow()
                            + timedelta(seconds=min(3600, 60 * 2 ** min(delivery.attempts - 1, 6)))
                        )
                    else:
                        delivery.status, delivery.last_error_code, delivery.retry_at = (
                            "sent",
                            None,
                            None,
                        )
                    counts[delivery.status] += 1
                    audit(
                        db,
                        f"notifications.delivery_{delivery.status}",
                        "notification_deliveries",
                        delivery.id,
                        after={
                            "batch_id": batch.id,
                            "error_code": delivery.last_error_code,
                            "attempts": delivery.attempts,
                        },
                    )
                    db.flush()
                    _finish_batch(db, batch)
                    db.commit()
            execution.complete({"delivery": counts})
            return counts
    except OperationBusyError:
        return {"sent": 0, "failed": 0, "uncertain": 0, "cancelled": 0, "busy": True}
    except Exception:
        # The protected operation records a generic failure when the store is
        # available. Notification problems must not reverse a committed screening.
        return {
            "sent": 0,
            "failed": 0,
            "uncertain": 0,
            "cancelled": 0,
            "error_code": "dispatcher_failed",
        }


def notification_health(db, config):
    from sqlalchemy import func

    values = public_settings(db)
    failed = (
        db.scalar(
            select(func.count())
            .select_from(NotificationDelivery)
            .where(NotificationDelivery.status == "failed")
        )
        or 0
    )
    uncertain = (
        db.scalar(
            select(func.count())
            .select_from(NotificationDelivery)
            .where(NotificationDelivery.status == "uncertain")
        )
        or 0
    )
    health: list[dict] = []
    try:
        configured = load_smtp_settings(db, config).configured
    except NotificationConfigurationError:
        configured = False
    if (values["likely_alerts_enabled"] or values["report_recipients"]) and not configured:
        health.append(
            {"kind": "smtp_not_configured", "message": "Configure SMTP to deliver notifications."}
        )
    if failed:
        health.append(
            {
                "kind": "delivery_failed",
                "message": f"{failed} email delivery attempt(s) need retry.",
                "count": failed,
            }
        )
    if uncertain:
        health.append(
            {
                "kind": "delivery_uncertain",
                "message": f"{uncertain} email delivery outcome(s) are uncertain; "
                "retry may duplicate a message.",
                "count": uncertain,
            }
        )
    return health
