"""Administrative attestations, holds and bounded evidence retention."""

from datetime import date

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import func, select

from app.api.deps import DB, AdminUser
from app.api.review import begin_write
from app.models import Party, RetentionHold, RetentionPurgeCertificate, User
from app.retention.service import (
    RetentionValidationError,
    active_holds,
    apply_retention,
    attest,
    eligibility,
    place_hold,
    purge_plan,
    release_hold,
    years_policy,
)
from app.scheduler.leases import (
    OperationAuthorityError,
    OperationBusyError,
    operation_session,
    protected_operation,
)

router = APIRouter()


class RetentionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EligibilityInput(RetentionInput):
    event_date: date
    external_dates_considered: StrictBool
    government_holds_excluded: StrictBool


class HoldInput(RetentionInput):
    party_id: str | None = None
    kind: str = Field(pattern="^(government|other)$")
    reason: str = Field(min_length=1, max_length=4000)


class ReleaseInput(RetentionInput):
    reason: str = Field(min_length=1, max_length=4000)
    authorization_reference: str | None = Field(default=None, max_length=4000)


def active_admin_write(db, actor_id):
    begin_write(db)
    actor = db.get(User, actor_id)
    if actor is None or not actor.active or actor.role != "admin":
        raise HTTPException(403, "Active Admin role required")
    return actor


def require_party(db, party_id):
    party = db.get(Party, party_id)
    if party is None:
        raise HTTPException(404, "Party not found")
    return party


@router.get("/retention/parties/{party_id}/eligibility")
def get_eligibility(party_id: str, user: AdminUser, db: DB):
    return eligibility(db, require_party(db, party_id))


@router.patch("/retention/parties/{party_id}/eligibility")
def update_eligibility(party_id: str, values: EligibilityInput, user: AdminUser, db: DB):
    actor = active_admin_write(db, user.id)
    party = require_party(db, party_id)
    try:
        attest(db, party, actor, **values.model_dump())
        db.commit()
    except RetentionValidationError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from None
    return eligibility(db, party)


@router.post("/retention/holds", status_code=201)
def add_hold(values: HoldInput, user: AdminUser, db: DB):
    actor = active_admin_write(db, user.id)
    try:
        row = place_hold(db, actor, **values.model_dump())
        db.commit()
    except RetentionValidationError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from None
    return {
        "id": row.id,
        "party_id": row.party_id,
        "kind": row.kind,
        "reason": row.reason,
        "actor": row.actor_snapshot,
        "created_at": row.created_at,
    }


@router.post("/retention/holds/{hold_id}/release", status_code=201)
def end_hold(hold_id: str, values: ReleaseInput, user: AdminUser, db: DB):
    actor = active_admin_write(db, user.id)
    hold = db.get(RetentionHold, hold_id)
    if hold is None:
        raise HTTPException(404, "Hold not found")
    try:
        row = release_hold(db, hold, actor, **values.model_dump())
        db.commit()
    except RetentionValidationError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from None
    return {
        "id": row.id,
        "hold_id": row.hold_id,
        "reason": row.reason,
        "authorization_reference": row.authorization_reference,
        "actor": row.actor_snapshot,
        "created_at": row.created_at,
    }


@router.get("/retention/status")
def retention_status(user: AdminUser, db: DB):
    years = years_policy(db)
    certificates = list(
        db.scalars(
            select(RetentionPurgeCertificate)
            .order_by(RetentionPurgeCertificate.created_at.desc())
            .limit(100)
        )
    )
    return {
        "enabled": years is not None,
        "years": years,
        "minimum_years": 5,
        "inactive_parties": db.scalar(
            select(func.count()).select_from(Party).where(Party.deleted_at.is_not(None))
        ),
        "active_holds": [
            {
                "id": row.id,
                "party_id": row.party_id,
                "kind": row.kind,
                "reason": row.reason,
                "actor": row.actor_snapshot,
                "created_at": row.created_at,
            }
            for row in active_holds(db)
        ],
        "certificates": [
            {
                "id": row.id,
                "party_id": row.party_id,
                "sha256": row.sha256,
                "created_at": row.created_at,
            }
            for row in certificates
        ],
        "permanently_retained": [
            "audit_log",
            "parties",
            "screening_runs",
            "run_party_evidence",
            "csl_entry_revisions",
            "retention_attestations",
            "retention_holds",
            "retention_hold_releases",
            "retention_purge_certificates",
        ],
    }


@router.post("/retention/preview")
def preview_retention(user: AdminUser, db: DB):
    return purge_plan(db)


@router.post("/retention/apply")
def apply_retention_now(request: Request, user: AdminUser, db: DB):
    actor_id = user.id
    db.rollback()
    factory = request.app.state.session_factory
    try:
        with protected_operation(factory, "retention_apply", actor_user_id=actor_id) as execution:
            with operation_session(factory, execution, actor_id) as operation_db:
                result = apply_retention(operation_db)
            execution.complete(
                {
                    "hits": result["hits"],
                    "dispositions": result["dispositions"],
                    "certificate_ids": [row["id"] for row in result["certificates"]],
                    "enabled": result["enabled"],
                }
            )
        return result
    except OperationBusyError:
        raise HTTPException(409, "Retention is already running or the database is busy") from None
    except OperationAuthorityError:
        raise HTTPException(403, "Active Admin role required") from None
    except RetentionValidationError as exc:
        raise HTTPException(422, str(exc)) from None
