from fastapi import APIRouter
from sqlalchemy import func, select

from app.api.deps import DB, CurrentUser
from app.models import CSLVersion, Hit, Party, ScreeningRun

router = APIRouter()


@router.get("/dashboard")
def dashboard(user: CurrentUser, db: DB):
    latest = db.scalar(select(CSLVersion).order_by(CSLVersion.fetched_at.desc()).limit(1))
    run = db.scalar(select(ScreeningRun).order_by(ScreeningRun.started_at.desc()).limit(1))
    return {
        "open_items": db.scalar(
            select(func.count())
            .select_from(Hit)
            .where(Hit.status == "open", Hit.bucket.in_(["review", "likely"]))
        )
        or 0,
        "likely_matches": db.scalar(
            select(func.count())
            .select_from(Hit)
            .where(Hit.status == "open", Hit.bucket == "likely")
        )
        or 0,
        "auto_cleared_since_report": 0,
        "stale_parties": db.scalar(
            select(func.count()).select_from(Party).where(Party.deleted_at.is_(None))
        )
        or 0,
        "total_parties": db.scalar(
            select(func.count()).select_from(Party).where(Party.deleted_at.is_(None))
        )
        or 0,
        "parties_screened": run.parties_screened if run else 0,
        "csl_version": {
            "id": latest.id,
            "fetched_at": latest.fetched_at,
            "entry_count": latest.entry_count,
        }
        if latest
        else None,
        "last_run": {"id": run.id, "finished_at": run.finished_at} if run else None,
    }
