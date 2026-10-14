"""Durable operations and encrypted notification evidence in the single store."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Created, Identity, UTCDateTime


class OperationLease(Identity, Base):
    __tablename__ = "operation_leases"
    key: Mapped[str] = mapped_column(String(200), unique=True)
    owner_id: Mapped[str | None] = mapped_column(String(100))
    owner_pid: Mapped[int | None] = mapped_column(Integer)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    fence: Mapped[int] = mapped_column(Integer, default=0)


class OperationAttempt(Identity, Created, Base):
    __tablename__ = "operation_attempts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','success','failed','skipped','interrupted')",
            name="valid_operation_status",
        ),
    )
    operation: Mapped[str] = mapped_column(String(50), index=True)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id"))
    party_id: Mapped[str | None] = mapped_column(ForeignKey("parties.id"))
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    error_code: Mapped[str | None] = mapped_column(String(100))
    outcome: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class NotificationBatch(Identity, Created, Base):
    __tablename__ = "notification_batches"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('morning','likely','invitation','test')", name="valid_notification_kind"
        ),
        CheckConstraint(
            "status IN ('pending','sending','sent','failed','uncertain','cancelled')",
            name="valid_notification_batch_status",
        ),
    )
    kind: Mapped[str] = mapped_column(String(20), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("screening_runs.id"))
    invitation_id: Mapped[str | None] = mapped_column(ForeignKey("user_invitations.id"))
    window_start: Mapped[datetime | None] = mapped_column(UTCDateTime())
    window_end: Mapped[datetime] = mapped_column(UTCDateTime())
    payload_enc: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class NotificationDelivery(Identity, Created, Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (
        UniqueConstraint("batch_id", "recipient", name="uq_notification_recipient"),
        CheckConstraint(
            "status IN ('pending','sending','sent','failed','uncertain','cancelled')",
            name="valid_notification_delivery_status",
        ),
    )
    batch_id: Mapped[str] = mapped_column(ForeignKey("notification_batches.id"), index=True)
    recipient: Mapped[str] = mapped_column(String(320))
    message_id: Mapped[str] = mapped_column(String(200), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    retry_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error_code: Mapped[str | None] = mapped_column(String(100))
