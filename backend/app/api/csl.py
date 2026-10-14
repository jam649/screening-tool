from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select

from app.api.deps import DB, AdminUser
from app.csl.fetch import CSLDownloadError
from app.csl.parser import CSLValidationError
from app.csl.service import (
    CSLBusyError,
    configured_api_key,
    refresh_csl,
    status_payload,
    version_payload,
)
from app.models import CSLVersion

router = APIRouter(prefix="/csl", tags=["csl"])


@router.get("/status")
def status(db: DB, user: AdminUser):
    return status_payload(db)


@router.get("/versions")
def versions(db: DB, user: AdminUser, limit: int = Query(20, ge=1, le=100)):
    return [
        version_payload(version)
        for version in db.scalars(
            select(CSLVersion)
            .order_by(CSLVersion.fetched_at.desc(), CSLVersion.id.desc())
            .limit(limit)
        )
    ]


@router.post("/refresh")
def refresh(request: Request, db: DB, user: AdminUser):
    try:
        key = configured_api_key(db, request.app.state.config)
        result = refresh_csl(db, api_key=key)
    except CSLBusyError as exc:
        raise HTTPException(409, str(exc)) from exc
    except CSLValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    except CSLDownloadError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {
        **status_payload(db),
        "fetch_id": result.fetch.id,
        "changed": result.changed,
        "pending_task_id": result.pending_task_id,
        "closed_hits": result.closed_hits,
    }
