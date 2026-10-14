from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import select

from app.api.audit import run_summary_payload
from app.api.deps import DB, AdminUser, CurrentUser
from app.models import ScreeningRun
from app.notifications.service import dispatch_outbox
from app.scheduler.leases import (
    OperationAuthorityError,
    OperationBusyError,
    operation_session,
    protected_operation,
)
from app.screening.engine import ScreeningBusyError, run_payload, run_screening
from app.screening.settings import ScreeningValidationError

router = APIRouter(prefix="/screen", tags=["screening"])


class ScreenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    all: bool = False
    party_id: UUID | None = None

    @model_validator(mode="after")
    def validate_scope(self):
        if self.all == (self.party_id is not None):
            raise ValueError("Choose exactly one scope: all=true or party_id")
        return self


@router.post("")
def screen(
    body: ScreenRequest, request: Request, background: BackgroundTasks, db: DB, user: AdminUser
):
    actor_id = user.id
    db.rollback()
    factory = request.app.state.session_factory
    operation = "screen_party" if body.party_id else "screen_all"
    try:
        with protected_operation(
            factory,
            operation,
            actor_user_id=actor_id,
            party_id=str(body.party_id) if body.party_id else None,
        ) as execution:
            with operation_session(factory, execution, actor_id) as operation_db:
                run = run_screening(
                    operation_db, party_ids=[str(body.party_id)] if body.party_id else None
                )
            execution.complete({"run_ids": [run.id]})
    except ScreeningValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (ScreeningBusyError, OperationBusyError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except OperationAuthorityError:
        raise HTTPException(403, "Active Admin role required") from None
    background.add_task(dispatch_outbox, factory, request.app.state.config)
    return run_payload(run)


@router.get("/runs")
def runs(db: DB, user: CurrentUser, limit: Annotated[int, Query(ge=1, le=100)] = 20):
    return [
        run_summary_payload(run)
        for run in db.scalars(
            select(ScreeningRun)
            .order_by(ScreeningRun.started_at.desc(), ScreeningRun.id.desc())
            .limit(limit)
        )
    ]


@router.get("/runs/{run_id}")
def run_status(run_id: UUID, db: DB, user: CurrentUser):
    run = db.get(ScreeningRun, str(run_id))
    if run is None:
        raise HTTPException(404, "Screening run not found")
    return run_payload(run)
