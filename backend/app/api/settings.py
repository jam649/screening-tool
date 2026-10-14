"""Public, validated administrator settings; secret writes require dedicated flows."""

from dataclasses import asdict
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Body, HTTPException, Request
from sqlalchemy import select

from app.api.deps import DB, AdminUser
from app.api.review import begin_write
from app.audit import audit
from app.config import DISCLAIMER, DISCLAIMER_URL, PRODUCT_NAME
from app.csl.service import current_version
from app.models import ScreeningTask, Setting, User
from app.screening.settings import (
    AUTO_CLEAR_ACK_KEY,
    AUTO_CLEAR_NOTICE,
    SCORING_SETTING_KEYS,
    ScreeningSettings,
    ScreeningValidationError,
    acknowledgment_valid,
    load_settings,
)

router = APIRouter()
GENERAL_DEFAULTS = {"display_name": PRODUCT_NAME, "timezone": "UTC", "retention_years": None}
ACK_REQUEST_KEY = "auto_clear_acknowledgment"
WRITABLE_KEYS = (set(GENERAL_DEFAULTS) | set(ScreeningSettings.__dataclass_fields__)) - {
    AUTO_CLEAR_ACK_KEY
}


def settings_payload(db):
    policy = load_settings(db, validate_acknowledgment=False)
    values = {**GENERAL_DEFAULTS, **asdict(policy)}
    for row in db.scalars(select(Setting).where(Setting.key.in_(GENERAL_DEFAULTS))):
        if not row.encrypted:
            values[row.key] = row.value
    return {
        **values,
        "disclaimer": DISCLAIMER,
        "disclaimer_url": DISCLAIMER_URL,
        "auto_clear_notice": AUTO_CLEAR_NOTICE,
        "auto_clear_acknowledgment_valid": acknowledgment_valid(db, policy),
    }


@router.get("/settings")
def get_settings(user: AdminUser, db: DB):
    return settings_payload(db)


@router.patch("/settings")
def update_settings(request: Request, user: AdminUser, db: DB, changes: Annotated[dict, Body()]):
    result = apply_settings(db, user, changes)
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        if "timezone" in changes:
            scheduler.reconcile()
        if result.get("pending_screening_task_id"):
            scheduler.wake()
    return result


def apply_settings(db, user, changes: dict):
    if not changes or set(changes) - (WRITABLE_KEYS | {ACK_REQUEST_KEY}):
        raise HTTPException(422, "Choose supported general or scoring settings")
    actor_id = user.id
    begin_write(db)
    actor = db.get(User, actor_id)
    if actor is None or not actor.active or actor.role != "admin":
        raise HTTPException(403, "Active Admin role required")
    current = load_settings(db, validate_acknowledgment=False)
    values = settings_payload(db)
    values.update(changes)
    if "score_weights" in changes:
        if not isinstance(changes["score_weights"], dict):
            raise HTTPException(422, "Score weights must be an object")
        values["score_weights"] = {**current.score_weights, **changes["score_weights"]}
    if (
        not isinstance(values["display_name"], str)
        or not 1 <= len(values["display_name"].strip()) <= 200
    ):
        raise HTTPException(422, "Display name must contain 1 to 200 characters")
    values["display_name"] = values["display_name"].strip()
    try:
        if not isinstance(values["timezone"], str):
            raise ValueError
        ZoneInfo(values["timezone"])
    except (ValueError, ZoneInfoNotFoundError):
        raise HTTPException(422, "Choose a valid IANA timezone") from None
    retention = values["retention_years"]
    if retention is not None and (
        isinstance(retention, bool) or not isinstance(retention, int) or retention < 5
    ):
        raise HTTPException(422, "Retention is disabled or at least five years")
    try:
        policy = ScreeningSettings(
            **{key: values[key] for key in ScreeningSettings.__dataclass_fields__}
        )
    except (ScreeningValidationError, TypeError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from None
    enabling = policy.auto_clear_enabled and (
        not current.auto_clear_enabled or not acknowledgment_valid(db, current)
    )
    if enabling and changes.get(ACK_REQUEST_KEY) != "I understand":
        raise HTTPException(422, "Type I understand to enable auto-clear")
    if ACK_REQUEST_KEY in changes and not enabling:
        raise HTTPException(422, "Acknowledgment is only accepted when enabling auto-clear")
    if enabling:
        proof = audit(
            db,
            "settings.auto_clear_acknowledged",
            "settings",
            "auto_clear_enabled",
            before={"enabled": current.auto_clear_enabled},
            after={
                "enabled": True,
                "threshold": policy.auto_clear_threshold,
                "acknowledgment": "I understand",
                "notice": AUTO_CLEAR_NOTICE,
                "actor": {
                    "id": actor.id,
                    "email": actor.email,
                    "display_name": actor.display_name,
                    "role": actor.role,
                    "active": actor.active,
                },
            },
            actor_user_id=actor.id,
        )
        db.flush()
        values[AUTO_CLEAR_ACK_KEY] = proof.id
    stored_keys = set(changes) - {ACK_REQUEST_KEY}
    if enabling:
        stored_keys.add(AUTO_CLEAR_ACK_KEY)
    for key in stored_keys:
        row = db.scalar(select(Setting).where(Setting.key == key))
        if row:
            row.value, row.encrypted = values[key], False
        else:
            db.add(Setting(key=key, value=values[key], encrypted=False))
    if set(changes) & {"auto_clear_enabled", "auto_clear_threshold"}:
        audit(
            db,
            "settings.auto_clear_policy_changed",
            "settings",
            "auto_clear_enabled",
            before=current.auto_clear_snapshot(),
            after={key: values[key] for key in current.auto_clear_snapshot()},
        )
    task = None
    changed_scoring = [
        key
        for key in SCORING_SETTING_KEYS
        if current.scoring_snapshot()[key] != policy.scoring_snapshot()[key]
    ]
    if changed_scoring:
        version = current_version(db)
        if version is not None:
            task = ScreeningTask(
                trigger="manual", csl_version_id=version.id, party_ids=[], entry_ids=[]
            )
            db.add(task)
            db.flush()
        audit(
            db,
            "settings.scoring_changed",
            "settings",
            after={
                "changed_keys": sorted(changed_scoring),
                "before": current.scoring_snapshot(),
                "after": policy.scoring_snapshot(),
                "full_screening_task_id": task.id if task else None,
            },
        )
    db.commit()
    return {**settings_payload(db), "pending_screening_task_id": task.id if task else None}


@router.get("/settings/auto-clear")
def get_auto_clear(user: AdminUser, db: DB):
    values = settings_payload(db)
    return {
        "enabled": values["auto_clear_enabled"],
        "threshold": values["auto_clear_threshold"],
        "review_threshold": values["review_threshold"],
        "likely_threshold": values["likely_threshold"],
        "acknowledgment_audit_id": values[AUTO_CLEAR_ACK_KEY],
        "acknowledgment_valid": values["auto_clear_acknowledgment_valid"],
        "notice": AUTO_CLEAR_NOTICE,
    }


@router.patch("/settings/auto-clear")
def update_auto_clear(user: AdminUser, db: DB, changes: Annotated[dict, Body()]):
    aliases = {
        "enabled": "auto_clear_enabled",
        "threshold": "auto_clear_threshold",
        "acknowledgment": ACK_REQUEST_KEY,
    }
    if set(changes) - set(aliases):
        raise HTTPException(422, "Choose enabled, threshold, or acknowledgment")
    result = apply_settings(db, user, {aliases[key]: value for key, value in changes.items()})
    return {
        **get_auto_clear(user, db),
        "pending_screening_task_id": result["pending_screening_task_id"],
    }
