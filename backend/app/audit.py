from copy import deepcopy
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from app.models import AuditLog, Disposition, Hit, Party, Setting, Source, Suppression, User

# Values outside this list are private even if callers forget encrypted=True.
PUBLIC_SETTING_KEYS = frozenset(
    {
        "product_name",
        "display_name",
        "timezone",
        "retention_years",
        "retention_enabled",
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


def _safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    return deepcopy(value)


def redact_credentials(value: Any) -> Any:
    """Remove credential-bearing fields at every level of connector configuration."""
    if isinstance(value, dict):
        return {
            key: (
                "[redacted]"
                if any(
                    marker in "".join(c for c in key.casefold() if c.isalnum())
                    for marker in SECRET_KEY_MARKERS
                )
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
    for obj in db.deleted:
        if isinstance(obj, (AuditLog, Party, Disposition, Hit)):
            raise ImmutableAuditError(f"{obj.__tablename__} records cannot be deleted")
    for obj in db.dirty:
        if isinstance(obj, AuditLog) and db.is_modified(obj, include_collections=True):
            raise ImmutableAuditError("Audit log records are insert-only")
        if isinstance(obj, Disposition) and db.is_modified(obj, include_collections=True):
            raise ImmutableAuditError("Disposition records are insert-only")
        if isinstance(obj, Hit):
            for key in inspect(obj).mapper.column_attrs.keys():
                if key == "status":
                    continue
                if inspect(obj).attrs[key].history.has_changes():
                    raise ImmutableAuditError("Historical hit evidence is immutable")
    tracked = (User, Source, Setting, Disposition, Party, Suppression, Hit)
    for obj in list(db.new) + list(db.dirty) + list(db.deleted):
        if not isinstance(obj, tracked):
            continue
        is_new = obj in db.new
        if not is_new and obj not in db.deleted and not db.is_modified(obj):
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
        elif obj not in db.deleted:
            _apply_update_defaults(obj)
        action = "create" if is_new else "delete" if obj in db.deleted else "update"
        audit(
            db,
            f"{obj.__tablename__}.{action}",
            obj.__tablename__,
            obj.id,
            before=None if is_new else _snapshot(obj, before=True),
            after=None if obj in db.deleted else _snapshot(obj),
        )


@event.listens_for(Session, "do_orm_execute")
def reject_bulk_audit_changes(state):
    if state.is_update or state.is_delete:
        table = getattr(state.statement, "table", None)
        if table is not None and table.name in {"audit_log", "dispositions"}:
            raise ImmutableAuditError("Audit and disposition records are insert-only")
