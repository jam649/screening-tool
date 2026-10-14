"""Public, validated administrator settings; secret writes require dedicated flows."""

from dataclasses import asdict
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Body, HTTPException
from sqlalchemy import select

from app.api.deps import DB, AdminUser
from app.api.review import begin_write
from app.config import DISCLAIMER, DISCLAIMER_URL, PRODUCT_NAME
from app.models import Setting, User
from app.screening.settings import ScreeningSettings, ScreeningValidationError, load_settings

router = APIRouter()
GENERAL_DEFAULTS = {"display_name": PRODUCT_NAME, "timezone": "UTC", "retention_years": None}
WRITABLE_KEYS = set(GENERAL_DEFAULTS) | set(ScreeningSettings.__dataclass_fields__)


def settings_payload(db):
    values = {**GENERAL_DEFAULTS, **asdict(load_settings(db))}
    for row in db.scalars(select(Setting).where(Setting.key.in_(GENERAL_DEFAULTS))):
        if not row.encrypted:
            values[row.key] = row.value
    return {**values, "disclaimer": DISCLAIMER, "disclaimer_url": DISCLAIMER_URL}


@router.get("/settings")
def get_settings(user: AdminUser, db: DB):
    return settings_payload(db)


@router.patch("/settings")
def update_settings(user: AdminUser, db: DB, changes: Annotated[dict, Body()]):
    if not changes or set(changes) - WRITABLE_KEYS:
        raise HTTPException(422, "Choose supported general or scoring settings")
    actor_id = user.id
    begin_write(db)
    actor = db.get(User, actor_id)
    if actor is None or not actor.active or actor.role != "admin":
        raise HTTPException(403, "Active Admin role required")
    values = settings_payload(db)
    values.update(changes)
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
        ScreeningSettings(**{key: values[key] for key in ScreeningSettings.__dataclass_fields__})
    except (ScreeningValidationError, TypeError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from None
    for key in changes:
        row = db.scalar(select(Setting).where(Setting.key == key))
        if row:
            row.value, row.encrypted = values[key], False
        else:
            db.add(Setting(key=key, value=values[key], encrypted=False))
    db.commit()
    return settings_payload(db)
