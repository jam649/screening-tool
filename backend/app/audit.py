from copy import deepcopy
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    Disposition,
    Hit,
    NotificationBatch,
    NotificationDelivery,
    OAuthState,
    OperationAttempt,
    Party,
    RetentionAttestation,
    RetentionHold,
    RetentionHoldRelease,
    RetentionPurgeCertificate,
    RunPartyEvidence,
    ScreeningRun,
    Setting,
    Source,
    Suppression,
    User,
)

# Values outside this list are private even if callers forget encrypted=True.
PUBLIC_SETTING_KEYS = frozenset(
    {
        "product_name",
        "display_name",
        "timezone",
        "retention_years",
        "candidate_min",
        "legal_suffixes",
        "score_weights",
        "candidate_stopwords",
        "enabled_source_lists",
        "w_det",
        "w_model",
        "review_threshold",
        "likely_threshold",
        "auto_clear_enabled",
        "auto_clear_threshold",
        "auto_clear_slider",
        "model_enabled",
        "model_provider",
        "model_name",
        "auto_clear_acknowledgment_audit_id",
        "public_base_url",
        "csl_fetch_time",
        "source_poll_minutes",
        "retention_weekday",
        "retention_time",
        "report_time",
        "report_timezone",
        "likely_alerts_enabled",
        "smtp_host",
        "smtp_port",
        "smtp_tls",
        "smtp_from",
        "report_recipients",
    }
)
SECRET_KEY_MARKERS = (
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "authorization",
    "apikey",
    "accesskey",
    "privatekey",
    "clientassertion",
    "clientstate",
    "connectionstring",
)


class ImmutableAuditError(ValueError):
    pass


def audit(
    db: Session,
    action: str,
    target_table: str,
    target_id: str | None = None,
    before: dict | None = None,
    after: dict | None = None,
    actor_user_id: str | None = None,
) -> AuditLog:
    row = AuditLog(
        action=action,
        target_table=target_table,
        target_id=target_id,
        before=before,
        after=after,
        actor_user_id=actor_user_id or db.info.get("actor_user_id"),
    )
    if row.actor_user_id is not None:
        actor = db.identity_map.get(db.identity_key(User, row.actor_user_id))
        if actor is not None:
            row.actor = actor
        else:
            for pending in db.new:
                if isinstance(pending, User) and pending.id == row.actor_user_id:
                    row.actor = pending
                    break
    db.add(row)
    return row


_PRIMITIVES = frozenset({str, int, float, bool, type(None)})


def _safe(value: Any) -> Any:
    # Exact immutable primitives need no copy; containers and custom types still do.
    # Children are tested inline: every hit snapshot visits thousands of leaves.
    if type(value) in _PRIMITIVES:
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {
            key: item if type(item) in _PRIMITIVES else _safe(item) for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [item if type(item) in _PRIMITIVES else _safe(item) for item in value]
    return deepcopy(value)


def public_model_measurement(key: str, value: Any) -> bool:
    """Typed public counts/fingerprints are distinct from credential token values."""
    if value is None and key in {
        "input_tokens",
        "output_tokens",
        "sequence_tokens",
        "laya_max_tokens",
        "tokenizer_sha256",
        "laya_tokenizer_sha256",
    }:
        return True
    if key in {"input_tokens", "output_tokens", "sequence_tokens"}:
        return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10_000_000
    if key == "laya_max_tokens":
        return isinstance(value, int) and not isinstance(value, bool) and value == 1024
    if key in {"tokenizer_sha256", "laya_tokenizer_sha256"}:
        from app.adjudicators.contracts import LAYA_TOKENIZER_SHA256

        return value == LAYA_TOKENIZER_SHA256
    return False


def _credential_key(key: str) -> bool:
    normalized = "".join(character for character in key.casefold() if character.isalnum())
    return any(marker in normalized for marker in SECRET_KEY_MARKERS)


def redact_credentials(value: Any) -> Any:
    """Remove credential-bearing fields at every level of connector configuration."""
    if isinstance(value, dict):
        return {
            key: (
                "[redacted]"
                if _credential_key(key)
                and not public_model_measurement(key, item)
                and not (
                    key in {"token_set", "token_sort"}
                    and isinstance(item, (int, float))
                    and not isinstance(item, bool)
                    and 0 <= item <= 1
                )
                else redact_credentials(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_credentials(item) for item in value]
    return _safe(value)


def _apply_defaults(obj: Any) -> None:
    """Materialize insert defaults once so the audit matches the persisted row."""
    state = inspect(obj)
    for attr in state.mapper.column_attrs:
        column = attr.columns[0]
        default = column.default
        if getattr(obj, attr.key) is None and default is not None:
            if default.is_scalar:
                setattr(obj, attr.key, deepcopy(default.arg))
            elif default.is_callable:
                setattr(obj, attr.key, default.arg(None))


def _apply_update_defaults(obj: Any) -> None:
    state = inspect(obj)
    for attr in state.mapper.column_attrs:
        default = attr.columns[0].onupdate
        if default is not None and not state.attrs[attr.key].history.has_changes():
            if default.is_scalar:
                setattr(obj, attr.key, deepcopy(default.arg))
            elif default.is_callable:
                setattr(obj, attr.key, default.arg(None))


def _snapshot(obj: Any, before: bool = False) -> dict:
    result = {}
    state = inspect(obj)
    for column in state.mapper.column_attrs:
        key = column.key
        value = getattr(obj, key)
        if before and state.attrs[key].history.deleted:
            value = state.attrs[key].history.deleted[0]
        result[key] = _safe(value)
    for key in {"password_hash", "credentials_enc"} & result.keys():
        result[key] = "[redacted]" if result[key] is not None else None
    if isinstance(obj, Setting) and (
        result["encrypted"] or result["key"] not in PUBLIC_SETTING_KEYS
    ):
        result["value"] = "[redacted]"
    elif isinstance(obj, Setting):
        result["value"] = redact_credentials(result["value"])
    if isinstance(obj, Source):
        result["config"] = redact_credentials(result["config"])
    return result


@event.listens_for(Session, "before_flush")
def protect_and_audit(db: Session, flush_context, instances):
    permanent = (
        AuditLog,
        Party,
        ScreeningRun,
        RunPartyEvidence,
        NotificationBatch,
        NotificationDelivery,
        OperationAttempt,
        RetentionAttestation,
        RetentionHold,
        RetentionHoldRelease,
        RetentionPurgeCertificate,
        OAuthState,
    )
    for obj in db.deleted:
        if isinstance(obj, Source) and obj.kind == "bc" and obj.config.get("company_id"):
            raise ImmutableAuditError("Bound Business Central source identity is permanent")
        if isinstance(obj, permanent):
            raise ImmutableAuditError(f"{obj.__tablename__} records cannot be deleted")
        if isinstance(obj, (Disposition, Hit)) and (obj.__tablename__, obj.id) not in (
            db.connection().info.get("retention_delete_capability", set())
        ):
            raise ImmutableAuditError(
                f"{obj.__tablename__} deletion requires exact retention eligibility"
            )
    frozen_inputs = {
        OAuthState: {
            "id",
            "created_at",
            "state_hash",
            "source_id",
            "user_id",
            "session_id",
            "app_generation",
            "source_generation",
            "redirect_uri",
            "expires_at",
        },
        NotificationBatch: {
            "id",
            "created_at",
            "kind",
            "dedupe_key",
            "run_id",
            "invitation_id",
            "window_start",
            "window_end",
            "payload_enc",
        },
        NotificationDelivery: {"id", "created_at", "batch_id", "recipient", "message_id"},
        OperationAttempt: {
            "id",
            "created_at",
            "operation",
            "source_id",
            "party_id",
            "parameters",
            "actor_user_id",
        },
    }
    for obj in db.dirty:
        if isinstance(obj, Source):
            original_identity = inspect(obj).identity
            original_id = original_identity[0] if original_identity else obj.id
            previous_source = (
                db.connection()
                .execute(
                    select(Source.id, Source.kind, Source.config).where(Source.id == original_id)
                )
                .first()
            )
            if (
                previous_source is not None
                and previous_source.kind == "bc"
                and previous_source.config.get("company_id")
            ):
                if (
                    obj.id != previous_source.id
                    or obj.kind != "bc"
                    or any(
                        obj.config.get(key) != previous_source.config.get(key)
                        for key in ("tenant_id", "bc_environment", "company_id")
                    )
                ):
                    raise ImmutableAuditError("Bound Business Central source identity is permanent")
        if isinstance(obj, OAuthState):
            consumed = inspect(obj).attrs.consumed_at.history
            if consumed.has_changes():
                previous = (
                    consumed.deleted[0]
                    if consumed.deleted
                    else db.scalar(select(OAuthState.consumed_at).where(OAuthState.id == obj.id))
                )
                if previous is not None or obj.consumed_at is None:
                    raise ImmutableAuditError("OAuth state can only be consumed once")
        if isinstance(
            obj,
            (RetentionAttestation, RetentionHold, RetentionHoldRelease, RetentionPurgeCertificate),
        ) and db.is_modified(obj, include_collections=True):
            raise ImmutableAuditError("Retention evidence is insert-only")
        for model, fields in frozen_inputs.items():
            if isinstance(obj, model) and any(
                inspect(obj).attrs[field].history.has_changes() for field in fields
            ):
                raise ImmutableAuditError("Operation/notification input evidence is immutable")
        if isinstance(obj, RunPartyEvidence) and db.is_modified(obj, include_collections=True):
            raise ImmutableAuditError("Run-party evidence is insert-only")
        if isinstance(obj, AuditLog) and db.is_modified(obj, include_collections=True):
            raise ImmutableAuditError("Audit log records are insert-only")
        if isinstance(obj, Disposition) and db.is_modified(obj, include_collections=True):
            raise ImmutableAuditError("Disposition records are insert-only")
        if isinstance(obj, ScreeningRun):
            state = inspect(obj)
            history = state.attrs.finished_at.history
            previously_finished = (
                history.deleted[0]
                if history.deleted
                else db.scalar(select(ScreeningRun.finished_at).where(ScreeningRun.id == obj.id))
                if history.has_changes()
                else obj.finished_at
            )
            if previously_finished is not None and db.is_modified(obj, include_collections=True):
                raise ImmutableAuditError("Completed screening run provenance is immutable")
        if isinstance(obj, Hit):
            state = inspect(obj)
            for key in state.mapper.column_attrs.keys():
                if key == "status":
                    continue
                if state.attrs[key].history.has_changes():
                    raise ImmutableAuditError("Historical hit evidence is immutable")
            history = state.attrs.status.history
            if history.has_changes():
                previous = history.deleted[0] if history.deleted else None
                if previous != "open" or obj.status != "dispositioned":
                    raise ImmutableAuditError("Hit status only closes an open observation")
                if not db.scalar(
                    select(Disposition.id).where(Disposition.hit_id == obj.id).limit(1)
                ):
                    raise ImmutableAuditError("Persist a disposition before closing its hit")
    for obj in db.new:
        if isinstance(obj, Hit) and obj.status not in {None, "open", "suppressed"}:
            raise ImmutableAuditError("New hits must be open or have remembered suppression")
    tracked = (User, Source, Setting, Disposition, Party, Suppression, Hit)
    # Session.new constructs an IdentitySet on every access. Capture the same
    # initial membership once while auditing this batch; audit rows are untracked.
    new_rows, deleted_rows = list(db.new), list(db.deleted)
    new_objects, deleted_objects = set(new_rows), set(deleted_rows)
    for obj in new_rows + list(db.dirty) + deleted_rows:
        if not isinstance(obj, tracked):
            continue
        is_new = obj in new_objects
        if not is_new and obj not in deleted_objects and not db.is_modified(obj):
            continue
        if is_new and obj.id is None:
            obj.id = str(uuid4())
        if is_new:
            _apply_defaults(obj)
            if isinstance(obj, Disposition):
                if (obj.kind == "auto_cleared") != (obj.actor_user_id is None):
                    raise ValueError(
                        "Human dispositions require an actor; auto-clear is system-only"
                    )
                if obj.kind in {"confirmed_match", "escalated"} and not obj.note.strip():
                    raise ValueError("Confirmed matches and escalations require a note")
        elif obj not in deleted_objects:
            _apply_update_defaults(obj)
        action = "create" if is_new else "delete" if obj in deleted_objects else "update"
        audit(
            db,
            f"{obj.__tablename__}.{action}",
            obj.__tablename__,
            obj.id,
            before=None if is_new else _snapshot(obj, before=True),
            after=None if obj in deleted_objects else _snapshot(obj),
        )


@event.listens_for(Session, "do_orm_execute")
def reject_bulk_audit_changes(state):
    if state.is_update or state.is_delete:
        table = getattr(state.statement, "table", None)
        if table is not None and table.name in {"audit_log", "dispositions"}:
            raise ImmutableAuditError("Audit and disposition records are insert-only")
