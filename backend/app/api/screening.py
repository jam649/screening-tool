from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import select

from app.api.deps import DB, AdminUser, CurrentUser
from app.models import ScreeningRun
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
def screen(body: ScreenRequest, db: DB, user: AdminUser):
    try:
        run = run_screening(db, party_ids=[str(body.party_id)] if body.party_id else None)
    except ScreeningValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ScreeningBusyError as exc:
        raise HTTPException(409, str(exc)) from exc
    return run_payload(run)


@router.get("/runs")
def runs(db: DB, user: CurrentUser, limit: Annotated[int, Query(ge=1, le=100)] = 20):
    return [
        run_payload(run)
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
