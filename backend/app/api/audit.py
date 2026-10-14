"""Authenticated immutable provenance views."""

from copy import deepcopy
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import DB, CurrentUser
from app.api.review import historical_actor
from app.models import AuditLog
from app.screening.engine import run_payload

router = APIRouter()


def project_scope(scope: dict, *, party_id: str | None = None):
    result: dict[str, Any] = {
        "party_count": len(scope["party_ids"])
        if isinstance(scope.get("party_ids"), list)
        else None,
        "entry_count": len(scope["entry_ids"])
        if isinstance(scope.get("entry_ids"), list)
        else None,
    }
    if party_id is not None:
        result.update(
            {"party_id": party_id, "party_hash": (scope.get("party_hashes") or {}).get(party_id)}
        )
    return result


def project_run_payload(payload: dict, *, party_id: str | None = None):
    result = {key: value for key, value in payload.items() if key != "settings_snapshot"}
    settings = payload.get("settings_snapshot")
    if isinstance(settings, dict):
        result["settings_snapshot"] = {
            key: value for key, value in settings.items() if key != "scope"
        }
        scope = settings.get("scope")
        if isinstance(scope, dict):
            result["settings_snapshot"]["scope"] = project_scope(scope, party_id=party_id)
    return result


def run_summary_payload(run, *, party_id: str | None = None):
    return project_run_payload(run_payload(run), party_id=party_id)


def audit_snapshot_payload(snapshot: dict | None, target_table: str):
    if snapshot is None:
        return None
    if target_table == "screening_runs":
        return project_run_payload(snapshot)
    if target_table == "screening_tasks":
        result = {
            key: value for key, value in snapshot.items() if key not in {"party_ids", "entry_ids"}
        }
        for key in ("party", "entry"):
            ids = snapshot.get(f"{key}_ids")
            if isinstance(ids, list):
                result[f"{key}_count"] = len(ids)
        return result
    return deepcopy(snapshot)


def audit_payload(db: Session, row: AuditLog):
    return {
        "id": row.id,
        "created_at": row.created_at,
        "action": row.action,
        "actor_user_id": row.actor_user_id,
        "actor": (row.after or {}).get("actor")
        or historical_actor(db, row.actor_user_id, row.created_at),
        "target_table": row.target_table,
        "target_id": row.target_id,
        "before": audit_snapshot_payload(row.before, row.target_table),
        "after": audit_snapshot_payload(row.after, row.target_table),
    }


@router.get("/audit-log")
def audit_log(
    user: CurrentUser,
    db: DB,
    action: str | None = None,
    target_table: str | None = None,
    target_id: str | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
):
    query = select(AuditLog)
    for key, value in (
        ("action", action),
        ("target_table", target_table),
        ("target_id", target_id),
    ):
        if value is not None:
            query = query.where(getattr(AuditLog, key) == value)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.scalars(
        query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).offset(offset).limit(limit)
    )
    return {"items": [audit_payload(db, row) for row in rows], "total": total}


@router.get("/runs")
def runs(
    user: CurrentUser,
    db: DB,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
):
    from app.models import ScreeningRun

    total = db.scalar(select(func.count()).select_from(ScreeningRun)) or 0
    rows = db.scalars(
        select(ScreeningRun).order_by(ScreeningRun.started_at.desc()).offset(offset).limit(limit)
    )
    return {"items": [run_summary_payload(row) for row in rows], "total": total}
