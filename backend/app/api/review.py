"""Current review queue and atomic, attributed human decisions."""

from copy import deepcopy
from datetime import datetime
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.deps import DB, CurrentUser
from app.audit import SECRET_KEY_MARKERS, audit
from app.models import AuditLog, CSLEntry, Disposition, Hit, Party, Suppression, User
from app.screening.engine import queue_hit_is_current, queue_hits

router = APIRouter()


def begin_write(db: Session) -> None:
    """Release dependency read transactions before taking SQLite's writer lock."""
    db.commit()
    try:
        db.execute(text("BEGIN IMMEDIATE"))
        db.expire_all()
    except OperationalError as exc:
        db.rollback()
        raise HTTPException(409, "Another operation is in progress; refresh and retry") from exc


def actor_payload(actor: User | None):
    return (
        {"id": actor.id, "email": actor.email, "display_name": actor.display_name}
        if actor
        else None
    )


def historical_actor(db: Session, actor_id: str | None, at: datetime):
    """Use actual recorded identity snapshots; never substitute today's user record."""
    if actor_id is None:
        return None
    histories = db.info.setdefault("actor_identity_history", {})
    if actor_id not in histories:
        histories[actor_id] = list(
            db.scalars(
                select(AuditLog)
                .where(
                    AuditLog.target_table == "users",
                    AuditLog.target_id == actor_id,
                    AuditLog.action.in_(["users.create", "users.update"]),
                )
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            )
        )
    for event in histories[actor_id]:
        state = event.after or {}
        if event.created_at <= at and "email" in state and "display_name" in state:
            return {"id": actor_id, "email": state["email"], "display_name": state["display_name"]}
    return {"id": actor_id, "email": None, "display_name": f"Historical user {actor_id}"}


def safe_source_url(value: str | None) -> str | None:
    if not value or any(c.isspace() or ord(c) < 32 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        # Force validation of malformed or out-of-range explicit ports.
        _ = parsed.port
        for key, _ in parse_qsl(parsed.query):
            clean = "".join(c for c in key.casefold() if c.isalnum())
            if any(marker in clean for marker in SECRET_KEY_MARKERS):
                return None
        return value
    except ValueError:
        return None


def disposition_payload(db: Session, row: Disposition):
    event = db.scalar(
        select(AuditLog)
        .where(AuditLog.action == "review.disposition", AuditLog.target_id == row.id)
        .order_by(AuditLog.created_at)
        .limit(1)
    )
    actor = (event.after or {}).get("actor") if event else None
    return {
        "id": row.id,
        "hit_id": row.hit_id,
        "kind": row.kind,
        "actor_user_id": row.actor_user_id,
        "actor": actor or historical_actor(db, row.actor_user_id, row.created_at),
        "note": row.note,
        "created_at": row.created_at,
        "slider_value_at_time": row.slider_value_at_time,
    }


def hit_payload(db: Session, hit: Hit, *, detailed: bool = True):
    result = {
        "id": hit.id,
        "run_id": hit.run_id,
        "party_id": hit.party_id,
        "csl_entry_id": hit.csl_entry_id,
        "party_hash": hit.party_hash,
        "entry_hash": hit.entry_hash,
        "party_name": hit.party_snapshot.get("name"),
        "entry_name": hit.entry_snapshot.get("name"),
        "source_list": hit.entry_snapshot.get("source_list"),
        "det_score": hit.det_score,
        "model_prob": hit.model_prob,
        "final_confidence": hit.final_confidence,
        "bucket": hit.bucket,
        "status": hit.status,
        "created_at": hit.created_at,
    }
    if detailed:
        # Explanation is trusted engine output. matched_tokens is public comparison
        # evidence, not an authentication token, and must survive serialization.
        result.update(
            {
                "party_snapshot": deepcopy(hit.party_snapshot),
                "entry_snapshot": deepcopy(hit.entry_snapshot),
                "explanation": deepcopy(hit.explanation),
                "source_url": safe_source_url(hit.entry_snapshot.get("fr_notice_url"))
                or safe_source_url(hit.entry_snapshot.get("source_list_url")),
                "dispositions": [
                    disposition_payload(db, d)
                    for d in db.scalars(
                        select(Disposition)
                        .where(Disposition.hit_id == hit.id)
                        .order_by(Disposition.created_at.desc())
                    )
                ],
                "prior_disposition": None,
                "suppression_disposition": None,
                "completed_disposition": None,
            }
        )
        completed_id = hit.explanation.get("completed_disposition_id")
        completed = db.get(Disposition, completed_id) if completed_id else None
        if completed:
            result["completed_disposition"] = disposition_payload(db, completed)
        suppression_id = hit.explanation.get("suppression_disposition_id")
        suppression_decision = db.get(Disposition, suppression_id) if suppression_id else None
        if suppression_decision:
            result["suppression_disposition"] = disposition_payload(db, suppression_decision)
        prior_id = hit.explanation.get("prior_disposition_id")
        prior = db.get(Disposition, prior_id) if prior_id else None
        if prior:
            result["prior_disposition"] = {
                **disposition_payload(db, prior),
                "what_changed": deepcopy(hit.explanation.get("what_changed", {})),
            }
    return result


class DispositionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["false_positive", "confirmed_match", "escalated"]
    note: str = Field(default="", max_length=10000)

    @model_validator(mode="after")
    def validate_note(self):
        self.note = self.note.strip()
        if self.kind != "false_positive" and not self.note:
            raise ValueError("Confirmed matches and escalations require a note")
        return self


@router.get("/queue")
def review_queue(user: CurrentUser, db: DB):
    rows = queue_hits(db)
    return {"items": [hit_payload(db, hit, detailed=False) for hit in rows], "total": len(rows)}


@router.get("/hits/{hit_id}")
def get_hit(hit_id: str, user: CurrentUser, db: DB):
    hit = db.get(Hit, hit_id)
    if hit is None:
        raise HTTPException(404, "Comparison not found")
    return hit_payload(db, hit)


@router.post("/hits/{hit_id}/dispositions", status_code=201)
def decide(hit_id: str, body: DispositionBody, user: CurrentUser, db: DB):
    actor_id = user.id
    begin_write(db)
    actor = db.get(User, actor_id)
    if actor is None or not actor.active:
        raise HTTPException(401, "Sign in required")
    hit = db.get(Hit, hit_id)
    if hit is None:
        raise HTTPException(404, "Comparison not found")
    if not queue_hit_is_current(db, hit):
        raise HTTPException(409, "This item changed or was already reviewed; refresh the queue")
    party, entry = db.get(Party, hit.party_id), db.get(CSLEntry, hit.csl_entry_id)
    if (
        party is None
        or entry is None
        or party.record_hash != hit.party_hash
        or entry.entry_hash != hit.entry_hash
    ):
        raise HTTPException(409, "The source record changed; refresh the queue")
    observations = list(
        db.scalars(
            select(Hit).where(
                Hit.party_id == hit.party_id,
                Hit.csl_entry_id == hit.csl_entry_id,
                Hit.party_hash == hit.party_hash,
                Hit.entry_hash == hit.entry_hash,
                Hit.status == "open",
            )
        )
    )
    decisions = {
        observation.id: Disposition(
            hit_id=observation.id, kind=body.kind, actor_user_id=actor_id, note=body.note
        )
        for observation in observations
    }
    db.add_all(decisions.values())
    # Status transitions require already-persisted immutable decision evidence.
    db.flush()
    selected = decisions.get(hit.id)
    for observation in observations:
        decision = decisions[observation.id]
        observation.status = "dispositioned"
        audit(
            db,
            "review.disposition",
            "dispositions",
            decision.id,
            after={
                "hit_id": observation.id,
                "party_id": hit.party_id,
                "csl_entry_id": hit.csl_entry_id,
                "party_hash": hit.party_hash,
                "entry_hash": hit.entry_hash,
                "kind": body.kind,
                "note": body.note,
                "actor": actor_payload(actor),
                "queue_hit_id": hit.id,
            },
        )
    assert selected is not None
    if body.kind == "false_positive":
        remembered = db.scalar(
            select(Suppression).where(
                Suppression.party_id == hit.party_id,
                Suppression.csl_entry_id == hit.csl_entry_id,
                Suppression.party_hash == hit.party_hash,
                Suppression.entry_hash == hit.entry_hash,
            )
        )
        if remembered is None:
            db.add(
                Suppression(
                    party_id=hit.party_id,
                    csl_entry_id=hit.csl_entry_id,
                    party_hash=hit.party_hash,
                    entry_hash=hit.entry_hash,
                    disposition_id=selected.id,
                )
            )
    db.commit()
    return {
        "disposition": disposition_payload(db, selected),
        "observations_dispositioned": len(observations),
        "hit_id": hit.id,
    }
