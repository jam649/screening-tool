"""Current party views and immutable per-party screening history."""

from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from app.api.audit import audit_payload, run_summary_payload
from app.api.deps import DB, CurrentUser
from app.api.review import hit_payload
from app.models import (
    AuditLog,
    CSLEntry,
    CSLFetch,
    Disposition,
    Hit,
    Party,
    RunPartyEvidence,
    Source,
    Suppression,
    utcnow,
)
from app.reports.metrics import (
    auto_clear_observations,
    current_screening_coverage,
    party_screenings,
)
from app.retention.service import purged_run_evidence
from app.screening.engine import queue_hits, snapshot
from app.screening.normalize import normalize_name

router = APIRouter()


def source_payload(source: Source | None):
    return (
        {
            "id": source.id,
            "kind": source.kind,
            "label": source.label,
            "last_synced_at": source.last_synced_at,
            "record_count": source.record_count,
            "last_error": source.last_error,
        }
        if source
        else None
    )


def party_rows(db: Session):
    queue_parties = {hit.party_id for hit in queue_hits(db)}
    histories = party_screenings(db)
    coverage = current_screening_coverage(db, histories)
    entries = {entry.id: entry for entry in db.scalars(select(CSLEntry))}
    current = {p.id: p for p in db.scalars(select(Party))}
    confirmed = set()
    escalated = set()
    auto_cleared = {hit.party_id for _, hit in auto_clear_observations(db)}
    latest_humans: dict[tuple[str, str], tuple[Disposition, Hit]] = {}
    latest_positives: dict[tuple[str, str], Disposition] = {}
    latest_false_positives: dict[tuple[str, str, str, str], Disposition] = {}
    for disposition, hit in db.execute(
        select(Disposition, Hit)
        .join(Hit)
        .where(Disposition.actor_user_id.is_not(None))
        .order_by(Disposition.created_at.desc(), Disposition.id.desc())
    ):
        pair = hit.party_id, hit.csl_entry_id
        latest_humans.setdefault(pair, (disposition, hit))
        if disposition.kind in {"confirmed_match", "escalated"}:
            latest_positives.setdefault(pair, disposition)
        elif disposition.kind == "false_positive":
            latest_false_positives.setdefault((*pair, hit.party_hash, hit.entry_hash), disposition)
    for disposition, hit in latest_humans.values():
        party, entry = current.get(hit.party_id), entries.get(hit.csl_entry_id)
        if (
            party
            and entry
            and party.record_hash == hit.party_hash
            and entry.entry_hash == hit.entry_hash
            and entry.removed_version is None
        ):
            if disposition.kind == "confirmed_match":
                confirmed.add(party.id)
            elif disposition.kind == "escalated":
                escalated.add(party.id)
    suppressed: dict[str, int] = {}
    for row in db.scalars(select(Suppression)):
        party, entry = current.get(row.party_id), entries.get(row.csl_entry_id)
        remembered = latest_false_positives.get(
            (row.party_id, row.csl_entry_id, row.party_hash, row.entry_hash)
        )
        remembered = remembered or db.get(Disposition, row.disposition_id)
        positive = latest_positives.get((row.party_id, row.csl_entry_id))
        if (
            positive
            and remembered
            and (positive.created_at, positive.id) > (remembered.created_at, remembered.id)
        ):
            continue
        if (
            party
            and entry
            and party.record_hash == row.party_hash
            and entry.entry_hash == row.entry_hash
            and entry.removed_version is None
        ):
            suppressed[party.id] = suppressed.get(party.id, 0) + 1
    sources = {s.id: s for s in db.scalars(select(Source))}
    result = []
    for party in current.values():
        runs = histories.get(party.id, [])
        last = next(
            (
                r
                for r in runs
                if (r.settings_snapshot.get("scope") or {}).get("party_hashes", {}).get(party.id)
                == party.record_hash
            ),
            None,
        )
        status = (
            "deleted"
            if party.deleted_at
            else "open_hit"
            if party.id in queue_parties
            else "confirmed_match"
            if party.id in confirmed
            else "escalated"
            if party.id in escalated
            else "suppressed"
            if suppressed.get(party.id)
            else "clear"
            if coverage.get(party.id, False)
            else "unscreened"
        )
        result.append(
            {
                **snapshot(party),
                "source": source_payload(sources.get(party.source_id)),
                "last_screened_at": last.finished_at if last else None,
                "status": status,
                "screened_current": coverage.get(party.id, False),
                "suppressed_pairings": suppressed.get(party.id, 0),
                "stale": last is None or last.finished_at < utcnow() - timedelta(hours=24),
                "auto_cleared": party.id in auto_cleared,
            }
        )
    return result


def filter_party_rows(
    rows,
    *,
    q=None,
    role=None,
    source_id=None,
    country=None,
    status=None,
    stale=None,
    auto_cleared=None,
    include_deleted=False,
):
    terms = normalize_name(q, legal_suffixes=()).ascii.split() if q else []
    result = []
    for row in rows:
        if row["deleted_at"] and not include_deleted:
            continue
        if any(
            value is not None and row[key] != value
            for key, value in (
                ("role", role),
                ("source_id", source_id),
                ("status", status),
                ("stale", stale),
                ("auto_cleared", auto_cleared),
            )
        ):
            continue
        if country and row["country_iso2"] != country.upper():
            continue
        text = " ".join(
            str(row.get(key) or "")
            for key in (
                "name",
                "alt_names",
                "address_lines",
                "city",
                "region",
                "postal_code",
                "external_id",
            )
        )
        normalized = normalize_name(text, legal_suffixes=()).ascii
        if not all(term in normalized for term in terms):
            continue
        result.append(row)
    return sorted(result, key=lambda row: (row["name"].casefold(), row["id"]))


@router.get("/parties")
def parties(
    user: CurrentUser,
    db: DB,
    q: str | None = Query(default=None, max_length=500),
    role: Literal["customer", "vendor", "both"] | None = None,
    source_id: str | None = None,
    country: str | None = None,
    status: Literal[
        "clear", "open_hit", "confirmed_match", "escalated", "suppressed", "unscreened", "deleted"
    ]
    | None = None,
    stale: bool | None = None,
    auto_cleared: bool | None = None,
    include_deleted: bool = False,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=20000),
):
    rows = filter_party_rows(
        party_rows(db),
        q=q,
        role=role,
        source_id=source_id,
        country=country,
        status=status,
        stale=stale,
        auto_cleared=auto_cleared,
        include_deleted=include_deleted,
    )
    return {"items": rows[offset : offset + limit], "total": len(rows)}


@router.get("/parties/{party_id}")
def party_file(party_id: str, user: CurrentUser, db: DB):
    party = db.get(Party, party_id)
    if party is None:
        raise HTTPException(404, "Party not found")
    hits = list(db.scalars(select(Hit).where(Hit.party_id == party.id)))
    by_run: dict[str, list[Hit]] = {}
    for hit in hits:
        by_run.setdefault(hit.run_id, []).append(hit)
    runs = party_screenings(db).get(party.id, [])
    purged = purged_run_evidence(db, party.id)
    participation = {
        row.run_id: row
        for row in db.scalars(select(RunPartyEvidence).where(RunPartyEvidence.party_id == party.id))
    }
    events = []
    for run in runs:
        observations = by_run.get(run.id, [])
        suppressed = [h for h in observations if h.status == "suppressed"]
        evidence = participation.get(run.id)
        pruned = purged.get(run.id, [])
        events.append(
            {
                "run": run_summary_payload(run, party_id=party.id),
                "hits": [
                    hit_payload(db, hit) for hit in observations if hit.status != "suppressed"
                ],
                "previously_cleared": {
                    "count": len(suppressed),
                    "hits": [hit_payload(db, h) for h in suppressed],
                },
                "zero_hits": evidence.hits_recorded == 0
                if evidence
                else not observations and not pruned,
                "purged_comparisons": pruned,
                "party_evidence": {
                    "id": evidence.id,
                    "party_hash": evidence.party_hash,
                    "party_snapshot": evidence.party_snapshot,
                    "source_snapshot": evidence.source_snapshot,
                    "candidates_considered": evidence.candidates_considered,
                    "hits_recorded": evidence.hits_recorded,
                    "result_counts": evidence.result_counts,
                }
                if evidence
                else None,
                "party_hash": (run.settings_snapshot.get("scope") or {})
                .get("party_hashes", {})
                .get(party.id),
            }
        )
    ids = {
        party.id,
        party.source_id,
        *(h.id for h in hits),
        *(h.csl_entry_id for h in hits),
        *(run.id for run in runs),
        *(run.csl_version_id for run in runs),
    }
    ids.update(
        db.scalars(select(Disposition.id).where(Disposition.hit_id.in_([h.id for h in hits])))
    )
    ids.update(db.scalars(select(Suppression.id).where(Suppression.party_id == party.id)))
    for rows in purged.values():
        for row in rows:
            ids.add(row["certificate_id"])
            ids.add(row["hit"]["id"])
            ids.add(row["hit"]["csl_entry_id"])
            ids.update(decision["id"] for decision in row["dispositions"])
    ids.update(row.id for row in participation.values())
    ids.update(
        db.scalars(
            select(CSLFetch.id).where(CSLFetch.version_id.in_([run.csl_version_id for run in runs]))
        )
    )
    # Task completion is the recorded link from requested scope to actual run.
    ids.update(
        db.scalars(
            select(AuditLog.target_id).where(
                AuditLog.target_table == "screening_tasks",
                AuditLog.after["run_id"].as_string().in_([run.id for run in runs]),
            )
        )
    )
    requested_members = func.json_each(AuditLog.after["party_ids"]).table_valued("value")
    ids.update(
        db.scalars(
            select(AuditLog.target_id).where(
                AuditLog.target_table == "screening_tasks",
                exists(
                    select(1)
                    .select_from(requested_members)
                    .where(requested_members.c.value == party.id)
                    .correlate(AuditLog)
                ),
            )
        )
    )
    audit_rows = db.scalars(
        select(AuditLog)
        .where(or_(AuditLog.target_id.in_(ids), AuditLog.target_table == "settings"))
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    )
    current = next(row for row in party_rows(db) if row["id"] == party.id)
    return {
        "party": current,
        "source": source_payload(db.get(Source, party.source_id)),
        "events": events,
        "audit_log": [audit_payload(db, row) for row in audit_rows],
    }
