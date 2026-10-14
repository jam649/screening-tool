"""Bounded deletion of old closed evidence, never permanent audit or live memory."""

from contextlib import contextmanager
from datetime import UTC, date, datetime, time
from typing import Any

from sqlalchemy import exists, func, or_, select, text
from sqlalchemy.orm import Session

from app.audit import audit
from app.canonical import canonical_hash
from app.models import AuditLog, Disposition, Hit, Party, ScreeningRun, Setting, Suppression, User
from app.models.base import utcnow
from app.models.retention import (
    RetentionAttestation,
    RetentionHold,
    RetentionHoldRelease,
    RetentionPurgeCertificate,
)


class RetentionValidationError(ValueError):
    pass


def actor_snapshot(actor: User) -> dict:
    return {
        "id": actor.id,
        "email": actor.email,
        "display_name": actor.display_name,
        "role": actor.role,
    }


def years_policy(db: Session) -> int | None:
    setting = db.scalar(select(Setting).where(Setting.key == "retention_years"))
    value = setting.value if setting and not setting.encrypted else None
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 5):
        raise RetentionValidationError("Retention is disabled or at least five years")
    return value


def anniversary(value: date, years: int) -> date | None:
    target_year = value.year + years
    if target_year > date.max.year:
        return None
    try:
        return value.replace(year=target_year)
    except ValueError:
        # Be conservative: a February 29 event has not reached five full calendar
        # years on February 28 of a non-leap year.
        return date(target_year, 3, 1)


def cutoff_at(now: datetime, years: int) -> datetime | None:
    target_year = now.year - years
    if target_year < datetime.min.year:
        return None
    try:
        return now.replace(year=target_year)
    except ValueError:
        return now.replace(year=target_year, day=28)


def latest_attestation(db: Session, party_id: str) -> RetentionAttestation | None:
    return db.scalar(
        select(RetentionAttestation)
        .where(RetentionAttestation.party_id == party_id)
        .order_by(RetentionAttestation.created_at.desc(), RetentionAttestation.id.desc())
        .limit(1)
    )


def active_holds(db: Session, party_id: str | None = None) -> list[RetentionHold]:
    query = select(RetentionHold).where(~RetentionHold.id.in_(select(RetentionHoldRelease.hold_id)))
    if party_id is not None:
        query = query.where(
            or_(RetentionHold.party_id.is_(None), RetentionHold.party_id == party_id)
        )
    return list(db.scalars(query))


def attest(
    db: Session,
    party: Party,
    actor: User,
    *,
    event_date: date,
    external_dates_considered: bool,
    government_holds_excluded: bool,
) -> RetentionAttestation:
    if event_date > utcnow().date():
        raise RetentionValidationError("The latest applicable event date cannot be in the future")
    # Record existing observed activity first so this proof can genuinely attest it.
    db.flush()
    row = RetentionAttestation(
        party_id=party.id,
        event_date=event_date,
        external_dates_considered=external_dates_considered,
        government_holds_excluded=government_holds_excluded,
        actor_user_id=actor.id,
        actor_snapshot=actor_snapshot(actor),
    )
    db.add(row)
    db.flush()
    audit(
        db,
        "retention.attested",
        row.__tablename__,
        row.id,
        after={
            "party_id": party.id,
            "event_date": event_date.isoformat(),
            "external_dates_considered": external_dates_considered,
            "government_holds_excluded": government_holds_excluded,
            "actor": row.actor_snapshot,
        },
    )
    return row


def place_hold(db: Session, actor: User, *, party_id: str | None, kind: str, reason: str):
    if kind not in {"government", "other"} or not reason.strip():
        raise RetentionValidationError("Choose a hold type and provide its reason")
    if party_id is not None and db.get(Party, party_id) is None:
        raise RetentionValidationError("Party not found")
    row = RetentionHold(
        party_id=party_id,
        kind=kind,
        reason=reason.strip(),
        actor_user_id=actor.id,
        actor_snapshot=actor_snapshot(actor),
    )
    db.add(row)
    db.flush()
    audit(
        db,
        "retention.hold_placed",
        row.__tablename__,
        row.id,
        after={
            "party_id": party_id,
            "kind": kind,
            "reason": row.reason,
            "actor": row.actor_snapshot,
        },
    )
    return row


def release_hold(
    db: Session,
    hold: RetentionHold,
    actor: User,
    *,
    reason: str,
    authorization_reference: str | None = None,
):
    if not reason.strip():
        raise RetentionValidationError("Provide the reason for releasing the hold")
    reference = authorization_reference.strip() if authorization_reference else None
    if hold.kind == "government" and not reference:
        raise RetentionValidationError(
            "Government holds require a written agency authorization reference"
        )
    if db.scalar(select(RetentionHoldRelease.id).where(RetentionHoldRelease.hold_id == hold.id)):
        raise RetentionValidationError("This hold is already released")
    row = RetentionHoldRelease(
        hold_id=hold.id,
        reason=reason.strip(),
        authorization_reference=reference,
        actor_user_id=actor.id,
        actor_snapshot=actor_snapshot(actor),
    )
    db.add(row)
    db.flush()
    audit(
        db,
        "retention.hold_released",
        row.__tablename__,
        row.id,
        after={
            "hold_id": hold.id,
            "party_id": hold.party_id,
            "reason": row.reason,
            "authorization_reference": reference,
            "actor": row.actor_snapshot,
        },
    )
    return row


def eligibility(db: Session, party: Party, *, now: datetime | None = None) -> dict:
    now = now or utcnow()
    years = years_policy(db)
    proof = latest_attestation(db, party.id)
    holds = active_holds(db, party.id)
    reasons = []
    if years is None:
        reasons.append("retention_disabled")
    if party.deleted_at is None:
        reasons.append("active_party")
    if proof is None:
        reasons.append("external_event_date_unproven")
    elif not proof.external_dates_considered or not proof.government_holds_excluded:
        reasons.append("eligibility_not_attested")
    if holds:
        reasons.append("active_hold")
    dates = [party.first_seen_at, party.last_seen_at, party.deleted_at]
    dates.extend(db.scalars(select(Hit.created_at).where(Hit.party_id == party.id)))
    dates.extend(
        db.scalars(select(Disposition.created_at).join(Hit).where(Hit.party_id == party.id))
    )
    # Include zero-hit participation and later public record edits, not only comparison rows.
    members = func.json_each(ScreeningRun.settings_snapshot["scope"]["party_ids"]).table_valued(
        "value"
    )
    dates.extend(
        db.scalars(
            select(ScreeningRun.finished_at).where(
                ScreeningRun.finished_at.is_not(None),
                exists(
                    select(1)
                    .select_from(members)
                    .where(members.c.value == party.id)
                    .correlate(ScreeningRun)
                ),
            )
        )
    )
    dates.extend(
        db.scalars(
            select(AuditLog.created_at).where(
                AuditLog.target_table == "parties", AuditLog.target_id == party.id
            )
        )
    )
    observed_activity = max(value for value in dates if value is not None)
    if proof is not None and proof.created_at < observed_activity:
        reasons.append("attestation_predates_activity")
    # A later mistaken attestation cannot silently erase a previously declared event.
    latest_event = db.scalar(
        select(func.max(RetentionAttestation.event_date)).where(
            RetentionAttestation.party_id == party.id
        )
    )
    if latest_event is not None:
        dates.append(datetime.combine(latest_event, time.max, tzinfo=UTC))
    basis = max(value for value in dates if value is not None)
    expires = anniversary(basis.date(), years) if years else None
    eligible_at = datetime.combine(expires, basis.timetz()) if expires else None
    if years is not None and (eligible_at is None or now < eligible_at):
        reasons.append("within_retention_period")
    return {
        "party_id": party.id,
        "eligible": not reasons,
        "reasons": reasons,
        "years": years,
        "basis_at": basis.isoformat(),
        "observed_activity_at": observed_activity.isoformat(),
        "latest_attested_event_date": latest_event.isoformat() if latest_event else None,
        "eligible_on": expires.isoformat() if expires else None,
        "eligible_at": eligible_at.isoformat() if eligible_at else None,
        "attestation": {
            "id": proof.id,
            "event_date": proof.event_date.isoformat(),
            "external_dates_considered": proof.external_dates_considered,
            "government_holds_excluded": proof.government_holds_excluded,
            "actor": proof.actor_snapshot,
            "created_at": proof.created_at.isoformat(),
        }
        if proof
        else None,
        "holds": [
            {
                "id": hold.id,
                "kind": hold.kind,
                "reason": hold.reason,
                "party_id": hold.party_id,
                "actor": hold.actor_snapshot,
            }
            for hold in holds
        ],
    }


def _row_snapshot(row: Any) -> dict:
    return {
        column.key: (value.isoformat() if isinstance(value, (datetime, date)) else value)
        for column in row.__mapper__.column_attrs
        for value in [getattr(row, column.key)]
    }


def purge_plan(db: Session, *, now: datetime | None = None) -> dict:
    now = now or utcnow()
    years = years_policy(db)
    if years is None:
        return {
            "enabled": False,
            "years": None,
            "as_of": now.isoformat(),
            "bundles": [],
            "blocked": [],
            "hits": 0,
            "dispositions": 0,
        }
    candidate_hits: dict[str, Hit] = {}
    eligibilities = {}
    blocked = []
    for party in db.scalars(select(Party).where(Party.deleted_at.is_not(None))):
        proof = eligibility(db, party, now=now)
        eligibilities[party.id] = proof
        if not proof["eligible"]:
            blocked.append({"party_id": party.id, "reasons": proof["reasons"]})
            continue
        for hit in db.scalars(
            select(Hit).where(Hit.party_id == party.id, Hit.status == "dispositioned")
        ):
            candidate_hits[hit.id] = hit
    candidate_dispositions = {
        row.id: row
        for row in db.scalars(select(Disposition).where(Disposition.hit_id.in_(candidate_hits)))
    }
    protected_dispositions = set(db.scalars(select(Suppression.disposition_id)))
    protected_hits = set(
        db.scalars(select(Disposition.hit_id).where(Disposition.id.in_(protected_dispositions)))
    )
    # Resolve references to a fixed point. Removing a protected row from the proposed bundle
    # makes its own referenced anchor/decision protected too.
    all_hits = list(db.scalars(select(Hit)))
    changed = True
    while changed:
        changed = False
        retained_hits = [
            hit for hit in all_hits if hit.id not in candidate_hits or hit.id in protected_hits
        ]
        for hit in retained_hits:
            references = hit.explanation or {}
            if not isinstance(references, dict):
                raise RetentionValidationError("Comparison reference provenance is invalid")
            anchor = references.get("queue_anchor_hit_id")
            if anchor in candidate_hits:
                protected_hits.add(anchor)
            for key in (
                "prior_disposition_id",
                "suppression_disposition_id",
                "completed_disposition_id",
            ):
                reference = references.get(key)
                if reference in candidate_dispositions:
                    protected_dispositions.add(reference)
        protected_hits.update(
            row.hit_id
            for row in candidate_dispositions.values()
            if row.id in protected_dispositions
        )
        for hit_id in list(candidate_hits):
            if hit_id in protected_hits:
                hit = candidate_hits.pop(hit_id)
                blocked.append(
                    {
                        "party_id": hit.party_id,
                        "hit_id": hit_id,
                        "reasons": ["retained_anchor_or_decision_reference"],
                    }
                )
                changed = True
    candidate_dispositions = {
        key: row for key, row in candidate_dispositions.items() if row.hit_id in candidate_hits
    }
    bundles = []
    for party_id, proof in eligibilities.items():
        hits = [hit for hit in candidate_hits.values() if hit.party_id == party_id]
        if not hits:
            continue
        hit_ids = {hit.id for hit in hits}
        decisions = [row for row in candidate_dispositions.values() if row.hit_id in hit_ids]
        bundles.append(
            {
                "party_id": party_id,
                "attestation_id": proof["attestation"]["id"],
                "basis_at": proof["basis_at"],
                "eligible_on": proof["eligible_on"],
                "eligible_at": proof["eligible_at"],
                "hit_ids": sorted(hit_ids),
                "disposition_ids": sorted(row.id for row in decisions),
            }
        )
    return {
        "enabled": True,
        "years": years,
        "as_of": now.isoformat(),
        "bundles": bundles,
        "blocked": blocked,
        "hits": len(candidate_hits),
        "dispositions": len(candidate_dispositions),
    }


@contextmanager
def _delete_capability(db: Session, grants: set[tuple[str, str]]):
    """Connection-local exact-ID capability; never configurable or serializable through SQL."""
    connection = db.connection()
    capability = connection.info["retention_delete_capability"]
    if capability:
        raise RetentionValidationError("A retention capability is already active")
    capability.update(grants)
    try:
        yield
    finally:
        capability.clear()


def apply_retention(db: Session, *, now: datetime | None = None) -> dict:
    now = now or utcnow()
    if db.new or db.dirty or db.deleted:
        raise RetentionValidationError("Apply retention in an independent operation")
    if db.in_transaction():
        db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        actor_id = db.info.get("actor_user_id")
        if actor_id:
            actor = db.get(User, actor_id)
            if actor is None or not actor.active or actor.role != "admin":
                raise RetentionValidationError("An active Admin is required")
        plan = purge_plan(db, now=now)
        certificates = []
        for bundle in plan["bundles"]:
            hits = list(db.scalars(select(Hit).where(Hit.id.in_(bundle["hit_ids"]))))
            decisions = list(
                db.scalars(select(Disposition).where(Disposition.id.in_(bundle["disposition_ids"])))
            )
            snapshots = []
            for row in decisions:
                evidence = _row_snapshot(row)
                if row.actor_user_id:
                    actor = db.get(User, row.actor_user_id)
                    # Actual original review audit identity is the authority when present.
                    recorded = db.scalar(
                        select(AuditLog)
                        .where(
                            AuditLog.action == "review.disposition", AuditLog.target_id == row.id
                        )
                        .order_by(AuditLog.created_at)
                        .limit(1)
                    )
                    evidence["actor"] = (recorded.after or {}).get("actor") if recorded else None
                    if evidence["actor"] is None:
                        user_history = db.scalar(
                            select(AuditLog)
                            .where(
                                AuditLog.target_table == "users",
                                AuditLog.target_id == row.actor_user_id,
                                AuditLog.created_at <= row.created_at,
                            )
                            .order_by(AuditLog.created_at.desc())
                            .limit(1)
                        )
                        evidence["actor"] = (
                            user_history.after
                            if user_history
                            else {
                                "id": actor.id if actor else row.actor_user_id,
                                "historical_identity_unavailable": True,
                            }
                        )
                snapshots.append(evidence)
            proof = db.get(RetentionAttestation, bundle["attestation_id"])
            cutoff = cutoff_at(now, plan["years"])
            hold_history = list(
                db.scalars(
                    select(RetentionHold).where(
                        or_(
                            RetentionHold.party_id.is_(None),
                            RetentionHold.party_id == bundle["party_id"],
                        )
                    )
                )
            )
            release_history = list(
                db.scalars(
                    select(RetentionHoldRelease).where(
                        RetentionHoldRelease.hold_id.in_([hold.id for hold in hold_history])
                    )
                )
            )
            evidence = {
                "schema": 1,
                "minimum_years": 5,
                "cutoff_at": cutoff.isoformat() if cutoff else None,
                "attestation": _row_snapshot(proof),
                "holds_checked": [_row_snapshot(hold) for hold in hold_history],
                "hold_releases": [_row_snapshot(release) for release in release_history],
                **bundle,
                "as_of": now.isoformat(),
                "years": plan["years"],
                "hits": [_row_snapshot(hit) for hit in hits],
                "dispositions": snapshots,
                "run_ids": sorted({hit.run_id for hit in hits}),
                "audit_retained": True,
                "runs_retained": True,
                "party_retained": True,
            }
            certificate = RetentionPurgeCertificate(
                party_id=bundle["party_id"],
                attestation_id=bundle["attestation_id"],
                years=plan["years"],
                cutoff_date=cutoff.date() if cutoff else date.min,
                actor_user_id=db.info.get("actor_user_id"),
                evidence=evidence,
                sha256=canonical_hash(evidence),
            )
            db.add(certificate)
            db.flush()
            audit(
                db,
                "retention.purged",
                certificate.__tablename__,
                certificate.id,
                after={
                    "party_id": bundle["party_id"],
                    "certificate_id": certificate.id,
                    "sha256": certificate.sha256,
                    "years": plan["years"],
                    "as_of": now.isoformat(),
                    "basis_at": bundle["basis_at"],
                    "hit_ids": bundle["hit_ids"],
                    "disposition_ids": bundle["disposition_ids"],
                    "attestation_id": bundle["attestation_id"],
                },
            )
            grants = {("hits", row.id) for row in hits} | {
                ("dispositions", row.id) for row in decisions
            }
            with _delete_capability(db, grants):
                for disposition in decisions:
                    db.delete(disposition)
                db.flush()
                for hit in hits:
                    db.delete(hit)
                db.flush()
            certificates.append(
                {
                    "id": certificate.id,
                    "party_id": certificate.party_id,
                    "sha256": certificate.sha256,
                }
            )
        db.commit()
        return {**plan, "certificates": certificates, "applied": True}
    except Exception:
        db.rollback()
        raise


def purged_run_evidence(db: Session, party_id: str) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for certificate in db.scalars(
        select(RetentionPurgeCertificate).where(RetentionPurgeCertificate.party_id == party_id)
    ):
        for hit in certificate.evidence.get("hits", []):
            result.setdefault(hit["run_id"], []).append(
                {
                    "certificate_id": certificate.id,
                    "certificate_sha256": certificate.sha256,
                    "hit": hit,
                    "dispositions": [
                        row
                        for row in certificate.evidence.get("dispositions", [])
                        if row["hit_id"] == hit["id"]
                    ],
                }
            )
    return result
