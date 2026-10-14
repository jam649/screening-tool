"""Append-only retention attestations, holds and exact purge certificates."""

from datetime import date
from typing import Any

from sqlalchemy import JSON, Boolean, CheckConstraint, Date, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Created, Identity


class RetentionAttestation(Identity, Created, Base):
    __tablename__ = "retention_attestations"
    party_id: Mapped[str] = mapped_column(ForeignKey("parties.id"), index=True)
    event_date: Mapped[date] = mapped_column(Date)
    external_dates_considered: Mapped[bool] = mapped_column(Boolean)
    government_holds_excluded: Mapped[bool] = mapped_column(Boolean)
    actor_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    actor_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)


class RetentionHold(Identity, Created, Base):
    __tablename__ = "retention_holds"
    __table_args__ = (
        CheckConstraint("kind IN ('government','other')", name="valid_retention_hold_kind"),
    )
    party_id: Mapped[str | None] = mapped_column(ForeignKey("parties.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(Text)
    actor_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    actor_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)


class RetentionHoldRelease(Identity, Created, Base):
    __tablename__ = "retention_hold_releases"
    hold_id: Mapped[str] = mapped_column(ForeignKey("retention_holds.id"), unique=True)
    authorization_reference: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    actor_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    actor_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)


class RetentionPurgeCertificate(Identity, Created, Base):
    __tablename__ = "retention_purge_certificates"
    __table_args__ = (CheckConstraint("years >= 5", name="valid_retention_certificate_years"),)
    party_id: Mapped[str] = mapped_column(ForeignKey("parties.id"), index=True)
    attestation_id: Mapped[str] = mapped_column(ForeignKey("retention_attestations.id"))
    years: Mapped[int] = mapped_column(Integer)
    cutoff_date: Mapped[date] = mapped_column(Date)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON)
    sha256: Mapped[str] = mapped_column(String(64))
