from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Created, Identity, UTCDateTime, utcnow


class User(Identity, Created, Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('admin', 'reviewer')", name="valid_user_role"),)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="reviewer")
    password_hash: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class UserSession(Identity, Created, Base):
    __tablename__ = "sessions"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)


class LoginAttempt(Identity, Created, Base):
    __tablename__ = "login_attempts"
    address_hash: Mapped[str] = mapped_column(String(64), index=True)
    identity_hash: Mapped[str] = mapped_column(String(64), index=True)


class Source(Identity, Created, Base):
    __tablename__ = "sources"
    __table_args__ = (CheckConstraint("kind IN ('csv','qbo','bc')", name="valid_source_kind"),)
    kind: Mapped[str] = mapped_column(String(20))
    label: Mapped[str] = mapped_column(String(200))
    credentials_enc: Mapped[str | None] = mapped_column(Text)
    cursor: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_synced_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)


class Party(Identity, Base):
    __tablename__ = "parties"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_source_external"),
        CheckConstraint("party_type IN ('company','individual')", name="valid_party_type"),
        CheckConstraint("role IN ('customer','vendor','both')", name="valid_party_role"),
    )
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(500))
    party_type: Mapped[str] = mapped_column(String(20), default="company")
    role: Mapped[str] = mapped_column(String(20), default="customer")
    name: Mapped[str] = mapped_column(Text)
    alt_names: Mapped[list[str]] = mapped_column(JSON, default=list)
    address_lines: Mapped[list[str]] = mapped_column(JSON, default=list)
    city: Mapped[str | None] = mapped_column(Text)
    region: Mapped[str | None] = mapped_column(Text)
    postal_code: Mapped[str | None] = mapped_column(Text)
    country_iso2: Mapped[str | None] = mapped_column(String(2))
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    record_hash: Mapped[str] = mapped_column(String(64), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class CSLVersion(Identity, Base):
    __tablename__ = "csl_versions"
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    file_sha256: Mapped[str] = mapped_column(String(64), index=True)
    semantic_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    entry_count: Mapped[int] = mapped_column(Integer)
    added: Mapped[int] = mapped_column(Integer, default=0)
    changed: Mapped[int] = mapped_column(Integer, default=0)
    removed: Mapped[int] = mapped_column(Integer, default=0)
    source_list_stamps: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class CSLEntry(Identity, Base):
    __tablename__ = "csl_entries"
    __table_args__ = (UniqueConstraint("source_list", "csl_id", name="uq_csl_source_id"),)
    csl_id: Mapped[str] = mapped_column(String(300))
    source_list: Mapped[str] = mapped_column(String(100), index=True)
    name: Mapped[str] = mapped_column(Text)
    alt_names: Mapped[list[str]] = mapped_column(JSON, default=list)
    addresses: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    countries: Mapped[list[str]] = mapped_column(JSON, default=list)
    programs: Mapped[list[str]] = mapped_column(JSON, default=list)
    remarks: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    entity_type: Mapped[str | None] = mapped_column(String(30))
    fr_notice_url: Mapped[str | None] = mapped_column(Text)
    source_list_url: Mapped[str | None] = mapped_column(Text)
    entry_hash: Mapped[str] = mapped_column(String(64), index=True)
    first_seen_version: Mapped[str] = mapped_column(ForeignKey("csl_versions.id"))
    last_seen_version: Mapped[str] = mapped_column(ForeignKey("csl_versions.id"))
    removed_version: Mapped[str | None] = mapped_column(ForeignKey("csl_versions.id"))


class ScreeningRun(Identity, Base):
    __tablename__ = "screening_runs"
    __table_args__ = (
        CheckConstraint(
            "trigger IN ('nightly','list_change','party_change','manual')", name="valid_run_trigger"
        ),
    )
    trigger: Mapped[str] = mapped_column(String(30))
    csl_version_id: Mapped[str] = mapped_column(ForeignKey("csl_versions.id"))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    parties_screened: Mapped[int] = mapped_column(Integer, default=0)
    hits_new: Mapped[int] = mapped_column(Integer, default=0)
    hits_suppressed: Mapped[int] = mapped_column(Integer, default=0)
    auto_cleared: Mapped[int] = mapped_column(Integer, default=0)
    model_name: Mapped[str | None] = mapped_column(String(100))
    settings_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    model_fallbacks: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)


class Hit(Identity, Created, Base):
    __tablename__ = "hits"
    __table_args__ = (
        UniqueConstraint("run_id", "party_id", "csl_entry_id", name="uq_run_pair"),
        CheckConstraint("det_score >= 0 AND det_score <= 100", name="valid_det_score"),
        CheckConstraint(
            "model_prob IS NULL OR (model_prob >= 0 AND model_prob <= 1)", name="valid_model_prob"
        ),
        CheckConstraint(
            "final_confidence >= 0 AND final_confidence <= 100", name="valid_confidence"
        ),
        CheckConstraint("bucket IN ('low','review','likely')", name="valid_bucket"),
        CheckConstraint("status IN ('open','dispositioned','suppressed')", name="valid_hit_status"),
        Index("ix_hit_queue", "status", "bucket", "final_confidence"),
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("screening_runs.id"), index=True)
    party_id: Mapped[str] = mapped_column(ForeignKey("parties.id"), index=True)
    party_hash: Mapped[str] = mapped_column(String(64))
    csl_entry_id: Mapped[str] = mapped_column(ForeignKey("csl_entries.id"), index=True)
    entry_hash: Mapped[str] = mapped_column(String(64))
    det_score: Mapped[float] = mapped_column(Float)
    model_prob: Mapped[float | None] = mapped_column(Float)
    final_confidence: Mapped[float] = mapped_column(Float)
    bucket: Mapped[str] = mapped_column(String(20))
    explanation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="open")
    # Immutable inputs are captured per hit so later source edits cannot rewrite its evidence.
    party_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    entry_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)


class Disposition(Identity, Created, Base):
    __tablename__ = "dispositions"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('false_positive','confirmed_match','escalated','auto_cleared')",
            name="valid_disposition_kind",
        ),
    )
    hit_id: Mapped[str] = mapped_column(ForeignKey("hits.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    note: Mapped[str] = mapped_column(Text, default="")
    slider_value_at_time: Mapped[int | None] = mapped_column(Integer)


class Suppression(Identity, Created, Base):
    __tablename__ = "suppressions"
    __table_args__ = (
        UniqueConstraint(
            "party_id", "csl_entry_id", "party_hash", "entry_hash", name="uq_suppression_pair"
        ),
    )
    party_id: Mapped[str] = mapped_column(ForeignKey("parties.id"), index=True)
    csl_entry_id: Mapped[str] = mapped_column(ForeignKey("csl_entries.id"))
    party_hash: Mapped[str] = mapped_column(String(64))
    entry_hash: Mapped[str] = mapped_column(String(64))
    disposition_id: Mapped[str] = mapped_column(ForeignKey("dispositions.id"))


class Setting(Identity, Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), unique=True)
    value: Mapped[Any] = mapped_column(JSON)
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class AuditLog(Identity, Created, Base):
    __tablename__ = "audit_log"
    actor: Mapped[User | None] = relationship()
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    target_table: Mapped[str] = mapped_column(String(100))
    target_id: Mapped[str | None] = mapped_column(String(36), index=True)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class CSLFetch(Identity, Base):
    __tablename__ = "csl_fetches"
    __table_args__ = (
        CheckConstraint("status IN ('fetching','success','failed')", name="valid_csl_fetch_status"),
    )
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(20), default="fetching", index=True)
    source: Mapped[str] = mapped_column(Text)
    file_sha256: Mapped[str | None] = mapped_column(String(64))
    semantic_sha256: Mapped[str | None] = mapped_column(String(64))
    version_id: Mapped[str | None] = mapped_column(ForeignKey("csl_versions.id"))
    source_list_stamps: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text)


class CSLEntryRevision(Identity, Base):
    __tablename__ = "csl_entry_revisions"
    __table_args__ = (UniqueConstraint("version_id", "entry_id", name="uq_csl_version_entry"),)
    version_id: Mapped[str] = mapped_column(ForeignKey("csl_versions.id"), index=True)
    entry_id: Mapped[str] = mapped_column(ForeignKey("csl_entries.id"), index=True)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)


class ScreeningTask(Identity, Created, Base):
    __tablename__ = "screening_tasks"
    __table_args__ = (
        CheckConstraint(
            "trigger IN ('nightly','list_change','party_change','manual')",
            name="valid_task_trigger",
        ),
        CheckConstraint(
            "status IN ('pending','running','complete','failed')", name="valid_task_status"
        ),
    )
    trigger: Mapped[str] = mapped_column(String(30))
    csl_version_id: Mapped[str] = mapped_column(ForeignKey("csl_versions.id"))
    entry_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    party_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)
