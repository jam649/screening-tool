"""Atomic deterministic screening and restart-safe consumption of scoped tasks.

Every comparison is immutable evidence. Repeated unresolved comparisons reference
their initial queue anchor; they are never represented as false-positive decisions.
A changed scoring context creates a fresh anchor. queue_hits selects the latest
context for each current pairing while retaining superseded open evidence in history.
"""

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import inspect, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.audit import audit, redact_credentials
from app.csl.parser import canonical_hash
from app.csl.service import current_version
from app.models import (
    CSLEntry,
    CSLEntryRevision,
    Disposition,
    Hit,
    Party,
    ScreeningRun,
    ScreeningTask,
    Suppression,
    utcnow,
)
from app.screening.candidates import CandidateIndex
from app.screening.score import bucket, score_pair
from app.screening.settings import ScreeningSettings, ScreeningValidationError, load_settings


class ScreeningBusyError(RuntimeError):
    pass


@dataclass(frozen=True)
class Adjudication:
    """A future adapter returns values before insertion; it never rewrites a Hit."""

    model_prob: float | None = None
    final_confidence: float | None = None
    model_name: str | None = None
    fallback: dict[str, Any] | None = None


AdjudicationHook = Callable[[dict, dict, float, dict, ScreeningSettings], Adjudication]


def snapshot(record: Any) -> dict[str, Any]:
    result = {}
    for column in inspect(record).mapper.column_attrs:
        value = getattr(record, column.key)
        result[column.key] = value.isoformat() if isinstance(value, datetime) else deepcopy(value)
    return redact_credentials(result)


def run_payload(run: ScreeningRun) -> dict[str, Any]:
    return snapshot(run)


def _begin(db: Session) -> None:
    # Auth/query dependencies may already have opened a read transaction. Mutations
    # must be explicitly flushed by callers before entering this owned transaction.
    if db.new or db.dirty or db.deleted:
        raise ScreeningValidationError("Commit pending changes before starting screening")
    db.commit()
    try:
        db.execute(text("BEGIN IMMEDIATE"))
    except OperationalError as exc:
        db.rollback()
        raise ScreeningBusyError("The database is busy; retry screening") from exc


def queue_hits(db: Session) -> list[Hit]:
    """One current unresolved anchor per pair, using the newest scoring context.

    Historical open observations stay available for audit, including superseded
    contexts. A low result in a newer context hides an old queue item without
    inventing a human or system disposition. Removed/deleted/stale hashes never queue.
    """
    rows = db.scalars(
        select(Hit)
        .join(ScreeningRun, Hit.run_id == ScreeningRun.id)
        .join(Party, Hit.party_id == Party.id)
        .join(CSLEntry, Hit.csl_entry_id == CSLEntry.id)
        .where(
            Party.deleted_at.is_(None),
            CSLEntry.removed_version.is_(None),
            Hit.party_hash == Party.record_hash,
            Hit.entry_hash == CSLEntry.entry_hash,
        )
        .order_by(ScreeningRun.started_at.desc(), Hit.created_at.desc(), Hit.id.desc())
    )
    latest: dict[tuple[str, str], Hit] = {}
    for hit in rows:
        key = hit.party_id, hit.csl_entry_id
        latest.setdefault(key, hit)
    scopes = [
        (run.id, run.settings_snapshot.get("scope"))
        for run in db.scalars(
            select(ScreeningRun)
            .where(ScreeningRun.finished_at.is_not(None))
            .order_by(ScreeningRun.started_at.desc(), ScreeningRun.id.desc())
        )
    ]
    active = []
    for (party_id, entry_id), observation in latest.items():
        # A later completed scope with no stored hit proves the current pair fell
        # below candidate_min. It must not leave an older queue confidence active.
        covering_run = next(
            (
                run_id
                for run_id, scope in scopes
                if scope is not None
                and (scope["party_ids"] is None or party_id in scope["party_ids"])
                and (scope["entry_ids"] is None or entry_id in scope["entry_ids"])
            ),
            None,
        )
        if covering_run is not None and covering_run != observation.run_id:
            continue
        if observation.status != "open" or observation.bucket == "low":
            continue
        anchor_id = observation.explanation.get("queue_anchor_hit_id")
        anchor = db.get(Hit, anchor_id) if anchor_id else observation
        if anchor is not None and anchor.status == "open":
            active.append(anchor)
    return sorted(
        active,
        key=lambda hit: (hit.bucket != "likely", -hit.final_confidence, hit.created_at),
    )


def _execute(
    db: Session,
    *,
    trigger: str,
    party_ids: list[str] | None,
    entry_ids: list[str] | None,
    csl_version_id: str | None,
    adjudicate: AdjudicationHook | None = None,
) -> ScreeningRun:
    version = current_version(db)
    if version is None:
        raise ScreeningValidationError("Fetch the CSL before screening parties")
    settings = load_settings(db)
    context_hash = canonical_hash(settings.snapshot())
    parties_query = select(Party).where(Party.deleted_at.is_(None))
    if party_ids is not None:
        parties_query = parties_query.where(Party.id.in_(party_ids))
    parties = list(db.scalars(parties_query))
    if party_ids is not None and any(id not in {p.id for p in parties} for id in party_ids):
        raise ScreeningValidationError("Party does not exist or is inactive")
    entries_query = select(CSLEntry).where(CSLEntry.removed_version.is_(None))
    if entry_ids is not None:
        entries_query = entries_query.where(CSLEntry.id.in_(entry_ids))
    if settings.enabled_source_lists is not None:
        entries_query = entries_query.where(CSLEntry.source_list.in_(settings.enabled_source_lists))
    entries = list(db.scalars(entries_query))
    if csl_version_id is not None and entry_ids is not None:
        revisions = {
            revision.entry_id: revision.snapshot.get("entry_hash")
            for revision in db.scalars(
                select(CSLEntryRevision).where(
                    CSLEntryRevision.version_id == csl_version_id,
                    CSLEntryRevision.entry_id.in_(entry_ids),
                )
            )
        }
        # Later no-op semantic versions must not discard still-relevant deltas;
        # only a changed/removed entry hash makes an older task stale.
        entries = [entry for entry in entries if revisions.get(entry.id) == entry.entry_hash]
    index = CandidateIndex(entries, settings)
    run_snapshot = {
        **settings.snapshot(),
        "scope": {"party_ids": party_ids, "entry_ids": entry_ids},
    }
    run = ScreeningRun(
        trigger=trigger,
        csl_version_id=csl_version_id or version.id,
        settings_snapshot=run_snapshot,
    )
    db.add(run)
    db.flush()
    party_set = {party.id for party in parties}
    existing_hits = list(
        db.scalars(
            select(Hit)
            .join(ScreeningRun)
            .where(Hit.party_id.in_(party_set))
            .order_by(ScreeningRun.started_at.desc(), Hit.created_at.desc(), Hit.id.desc())
        )
    )
    anchors: dict[tuple, Hit] = {}
    historical: dict[tuple[str, str], Hit] = {}
    latest_anchors: set[tuple[str, str]] = set()
    for hit in existing_hits:
        pair = hit.party_id, hit.csl_entry_id
        historical.setdefault(pair, hit)
        key = (*pair, hit.party_hash, hit.entry_hash, hit.explanation.get("queue_context"))
        if not hit.explanation.get("queue_anchor_hit_id") and pair not in latest_anchors:
            latest_anchors.add(pair)
            if hit.status == "open" and hit.bucket != "low":
                anchors[key] = hit
    suppressions = list(db.scalars(select(Suppression).where(Suppression.party_id.in_(party_set))))
    exact_suppressions = {
        (s.party_id, s.csl_entry_id, s.party_hash, s.entry_hash): s for s in suppressions
    }
    prior_suppressions: dict[tuple[str, str], Suppression] = {}
    for remembered in sorted(suppressions, key=lambda s: s.created_at, reverse=True):
        prior_suppressions.setdefault((remembered.party_id, remembered.csl_entry_id), remembered)
    dispositions: dict[tuple[str, str], Disposition] = {}
    for disposition in db.scalars(
        select(Disposition)
        .join(Hit)
        .where(Hit.party_id.in_(party_set))
        .order_by(Disposition.created_at.desc())
    ):
        disposition_hit = db.get(Hit, disposition.hit_id)
        assert disposition_hit is not None
        dispositions.setdefault(
            (disposition_hit.party_id, disposition_hit.csl_entry_id), disposition
        )
    for party in parties:
        candidates, fallback = index.candidates(party)
        run.parties_screened += 1
        for entry in candidates:
            scored = score_pair(party, entry, settings)
            if scored.score < settings.candidate_min:
                continue
            party_snapshot, entry_snapshot = snapshot(party), snapshot(entry)
            explanation = scored.explanation
            explanation.update({"blocking_fallback": fallback, "queue_context": context_hash})
            exact_key = party.id, entry.id, party.record_hash, entry.entry_hash
            suppression = exact_suppressions.get(exact_key)
            prior = prior_suppressions.get((party.id, entry.id))
            previous_disposition = dispositions.get((party.id, entry.id))
            if suppression:
                explanation["suppression_disposition_id"] = suppression.disposition_id
            elif prior or previous_disposition:
                if prior is not None:
                    disposition_id = prior.disposition_id
                else:
                    assert previous_disposition is not None
                    disposition_id = previous_disposition.id
                explanation["prior_disposition_id"] = disposition_id
                prior_decision = db.get(Disposition, disposition_id)
                assert prior_decision is not None
                prior_hit = db.get(Hit, prior_decision.hit_id)
                assert prior_hit is not None
                changes = {}
                for side, before, after in (
                    ("party", prior_hit.party_snapshot, party_snapshot),
                    ("entry", prior_hit.entry_snapshot, entry_snapshot),
                ):
                    changes[side] = {
                        key: {"before": before.get(key), "after": after.get(key)}
                        for key in after
                        if before.get(key) != after.get(key)
                    }
                explanation["what_changed"] = changes
            result = Adjudication()
            if adjudicate and not suppression and scored.score >= settings.review_threshold - 10:
                result = adjudicate(
                    party_snapshot, entry_snapshot, scored.score, deepcopy(explanation), settings
                )
            confidence = (
                scored.score if result.final_confidence is None else result.final_confidence
            )
            if not 0 <= confidence <= 100 or (
                result.model_prob is not None and not 0 <= result.model_prob <= 1
            ):
                raise ScreeningValidationError("Adjudication returned invalid confidence")
            hit_bucket = bucket(confidence, settings, scored.score)
            anchor = anchors.get((*exact_key, context_hash)) if hit_bucket != "low" else None
            if anchor and not suppression:
                explanation["queue_anchor_hit_id"] = anchor.id
            hit = Hit(
                run_id=run.id,
                party_id=party.id,
                party_hash=party.record_hash,
                csl_entry_id=entry.id,
                entry_hash=entry.entry_hash,
                det_score=scored.score,
                model_prob=result.model_prob,
                final_confidence=confidence,
                bucket=hit_bucket,
                explanation=explanation,
                status="suppressed" if suppression else "open",
                party_snapshot=party_snapshot,
                entry_snapshot=entry_snapshot,
            )
            db.add(hit)
            if suppression:
                run.hits_suppressed += 1
            elif hit_bucket != "low" and not anchor:
                run.hits_new += 1
            if result.model_name:
                run.model_name = result.model_name
            if result.fallback:
                run.model_fallbacks = [*run.model_fallbacks, redact_credentials(result.fallback)]
        # Audit listeners can otherwise accumulate an entire book in one flush.
        if run.parties_screened % 100 == 0:
            db.flush()
    run.finished_at = utcnow()
    db.flush()
    audit(db, "screening.completed", "screening_runs", run.id, after=run_payload(run))
    return run


def run_screening(
    db: Session,
    *,
    trigger: str = "manual",
    party_ids: list[str] | None = None,
    entry_ids: list[str] | None = None,
    csl_version_id: str | None = None,
    adjudicate: AdjudicationHook | None = None,
) -> ScreeningRun:
    _begin(db)
    try:
        run = _execute(
            db,
            trigger=trigger,
            party_ids=party_ids,
            entry_ids=entry_ids,
            csl_version_id=csl_version_id,
            adjudicate=adjudicate,
        )
        db.commit()
        return run
    except Exception:
        db.rollback()
        raise


def process_pending_tasks(db: Session) -> list[ScreeningRun]:
    """Consume tasks one at a time under SQLite's write lock.

    Task state, immutable comparisons, and completion commit together: a crashed
    transaction leaves the task pending and no partially committed comparisons.
    Failed tasks retain a sanitized error and are retried on the next invocation.
    """
    task_ids = list(
        db.scalars(
            select(ScreeningTask.id)
            .where(ScreeningTask.status.in_(["pending", "failed", "running"]))
            .order_by(ScreeningTask.created_at, ScreeningTask.id)
        )
    )
    runs = []
    for task_id in task_ids:
        _begin(db)
        task = db.get(ScreeningTask, task_id, populate_existing=True)
        if task is None or task.status == "complete":
            db.rollback()
            continue
        try:
            task.status = "running"
            task.attempts += 1
            task.started_at = utcnow()
            task.last_error = None
            active_party_ids = None
            if task.party_ids:
                active_party_ids = list(
                    db.scalars(
                        select(Party.id).where(
                            Party.id.in_(task.party_ids), Party.deleted_at.is_(None)
                        )
                    )
                )
            run = _execute(
                db,
                trigger=task.trigger,
                party_ids=active_party_ids,
                entry_ids=task.entry_ids if task.trigger == "list_change" else None,
                csl_version_id=task.csl_version_id if task.trigger == "list_change" else None,
            )
            task.status = "complete"
            task.finished_at = utcnow()
            audit(
                db,
                "screening.task_completed",
                "screening_tasks",
                task.id,
                after={"run_id": run.id, "attempts": task.attempts},
            )
            db.commit()
            runs.append(run)
        except Exception:
            db.rollback()
            task = db.get(ScreeningTask, task_id)
            assert task is not None
            task.status = "failed"
            task.attempts += 1
            task.last_error = "Screening failed; no comparison from this task was committed"
            audit(
                db,
                "screening.task_failed",
                "screening_tasks",
                task.id,
                after={"error": task.last_error},
            )
            db.commit()
            raise
    return runs
