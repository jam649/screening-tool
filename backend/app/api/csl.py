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
from app.scheduler.leases import (
    OperationAuthorityError,
    OperationBusyError,
    operation_session,
    protected_operation,
)
from app.scheduler.runtime import current_runtime

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
    actor_id = user.id
    db.rollback()
    factory = request.app.state.session_factory
    try:
        with protected_operation(factory, "csl_refresh", actor_user_id=actor_id) as execution:
            with operation_session(factory, execution, actor_id) as operation_db:
                key = configured_api_key(operation_db, request.app.state.config)
                result = refresh_csl(operation_db, api_key=key)
            execution.complete(
                {
                    "fetch_id": result.fetch.id,
                    "version_id": result.version.id,
                    "changed": result.changed,
                    "pending_task_id": result.pending_task_id,
                    "closed_hits": result.closed_hits,
                }
            )
    except (CSLBusyError, OperationBusyError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except OperationAuthorityError:
        raise HTTPException(403, "Active Admin role required") from None
    except CSLValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    except CSLDownloadError as exc:
        raise HTTPException(502, str(exc)) from exc
    runtime = current_runtime()
    if runtime and result.pending_task_id:
        runtime.wake()
    return {
        **status_payload(db),
        "fetch_id": result.fetch.id,
        "changed": result.changed,
        "pending_task_id": result.pending_task_id,
        "closed_hits": result.closed_hits,
    }
