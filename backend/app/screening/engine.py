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
from app.csl.parser import HASH_RULE_VERSION, canonical_hash, csl_entry_hash
from app.csl.service import current_version
from app.models import (
    CSLEntry,
    CSLEntryRevision,
    Disposition,
    Hit,
    Party,
    ScreeningRun,
    ScreeningTask,
    Source,
    Suppression,
    utcnow,
)
from app.models.evidence import RunPartyEvidence
from app.screening.candidates import CandidateIndex
from app.screening.normalize import party_record_hash
from app.screening.score import bucket, score_pair
from app.screening.settings import ScreeningSettings, ScreeningValidationError, load_settings


class ScreeningBusyError(RuntimeError):
    pass


class ScreeningTasksFailedError(ScreeningValidationError):
    """Partial progress is committed; diagnostics never contain exception text."""

    def __init__(self, completed_runs: list[ScreeningRun], failures: list[dict[str, Any]]):
        self.completed_runs = completed_runs
        self.failures = failures
        super().__init__(
            f"{len(failures)} screening task(s) require retry; "
            f"{len(completed_runs)} independent task(s) completed. "
            "Unfinished tasks remain available for retry."
        )


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


def queue_hits(
    db: Session, *, party_id: str | None = None, entry_id: str | None = None
) -> list[Hit]:
    """One current unresolved anchor per pair, using the newest scoring context.

    Historical open observations stay available for audit, including superseded
    contexts. A low result in a newer context hides an old queue item without
    inventing a human or system disposition. Removed/deleted/stale hashes never queue.
    """
    enabled_lists = load_settings(db).enabled_source_lists
    query = (
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
    if enabled_lists is not None:
        query = query.where(CSLEntry.source_list.in_(enabled_lists))
    if party_id is not None:
        query = query.where(Hit.party_id == party_id)
    if entry_id is not None:
        query = query.where(Hit.csl_entry_id == entry_id)
    rows = db.scalars(query)
    latest: dict[tuple[str, str], Hit] = {}
    for hit in rows:
        key = hit.party_id, hit.csl_entry_id
        latest.setdefault(key, hit)
    if not latest:
        return []
    scopes = []
    for run in db.scalars(
        select(ScreeningRun)
        .where(ScreeningRun.finished_at.is_not(None))
        .order_by(ScreeningRun.started_at.desc(), ScreeningRun.id.desc())
    ):
        scope = run.settings_snapshot.get("scope")
        if scope is not None:
            party_ids = None if scope["party_ids"] is None else set(scope["party_ids"])
            entry_ids = None if scope["entry_ids"] is None else set(scope["entry_ids"])
            scopes.append((run.id, party_ids, entry_ids))
    active = []
    for (party_id, entry_id), observation in latest.items():
        # A later completed scope with no stored hit proves the current pair fell
        # below candidate_min. It must not leave an older queue confidence active.
        covering_run = next(
            (
                run_id
                for run_id, party_ids, entry_ids in scopes
                if (party_ids is None or party_id in party_ids)
                and (entry_ids is None or entry_id in entry_ids)
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


def queue_hit_is_current(db: Session, hit: Hit) -> bool:
    """Validate one selected pair without materializing the entire book's queue."""
    return any(
        row.id == hit.id for row in queue_hits(db, party_id=hit.party_id, entry_id=hit.csl_entry_id)
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
    context_hash = canonical_hash(settings.scoring_snapshot())
    decision_context = canonical_hash(
        {"scoring_context": context_hash, "auto_clear_policy": settings.auto_clear_snapshot()}
    )
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
    if any(party.record_hash != party_record_hash(snapshot(party)) for party in parties) or any(
        entry.entry_hash != csl_entry_hash(snapshot(entry)) for entry in entries
    ):
        raise ScreeningValidationError(
            "Cached matching hashes require an explicit upgrade; "
            "run screening-tool hashes upgrade before screening"
        )
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
        "scoring_context": context_hash,
        "decision_context": decision_context,
        "hash_rule_version": HASH_RULE_VERSION,
        "scope": {
            "party_ids": [party.id for party in parties],
            "party_hashes": {party.id: party.record_hash for party in parties},
            "entry_ids": [entry.id for entry in entries],
            "entry_hashes": {entry.id: entry.entry_hash for entry in entries},
        },
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
    latest_anchors: set[tuple[str, str]] = set()
    for hit in existing_hits:
        pair = hit.party_id, hit.csl_entry_id
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
    human_decisions: dict[tuple[str, str], Disposition] = {}
    positive_decisions: dict[tuple[str, str], Disposition] = {}
    false_positive_decisions: dict[tuple[str, str, str, str], Disposition] = {}
    decision_hits = {hit.id: hit for hit in existing_hits}
    for disposition in db.scalars(
        select(Disposition)
        .join(Hit)
        .where(Hit.party_id.in_(party_set))
        .order_by(Disposition.created_at.desc(), Disposition.id.desc())
    ):
        disposition_hit = decision_hits[disposition.hit_id]
        pair = disposition_hit.party_id, disposition_hit.csl_entry_id
        dispositions.setdefault(pair, disposition)
        if disposition.actor_user_id is not None:
            human_decisions.setdefault(pair, disposition)
            if disposition.kind in {"confirmed_match", "escalated"}:
                positive_decisions.setdefault(pair, disposition)
            elif disposition.kind == "false_positive":
                exact_pair = (*pair, disposition_hit.party_hash, disposition_hit.entry_hash)
                false_positive_decisions.setdefault(exact_pair, disposition)
    scoped_entries = {entry.id: entry for entry in entries}
    party_hashes = {party.id: party.record_hash for party in parties}
    sources = {
        source.id: source
        for source in db.scalars(
            select(Source).where(Source.id.in_({party.source_id for party in parties}))
        )
    }
    forced: dict[str, set[str]] = {}
    for hit in existing_hits:
        current_entry = scoped_entries.get(hit.csl_entry_id)
        if (
            hit.status == "open"
            and hit.bucket != "low"
            and current_entry is not None
            and hit.party_hash == party_hashes[hit.party_id]
            and hit.entry_hash == current_entry.entry_hash
        ):
            forced.setdefault(hit.party_id, set()).add(hit.csl_entry_id)
    for party in parties:
        original_party = snapshot(party)
        counts = {
            key: 0
            for key in (
                "low",
                "review",
                "likely",
                "suppressed",
                "auto_cleared",
                "completed_human",
                "new_queue",
            )
        }
        candidates, fallback = index.candidates(party)
        candidate_ids = {entry.id for entry in candidates}
        candidates.extend(
            scoped_entries[id] for id in sorted(forced.get(party.id, set()) - candidate_ids)
        )
        run.parties_screened += 1
        for entry in candidates:
            scored = score_pair(party, entry, settings)
            if scored.score < settings.candidate_min:
                continue
            party_snapshot, entry_snapshot = deepcopy(original_party), snapshot(entry)
            explanation = scored.explanation
            explanation.update(
                {
                    "blocking_fallback": fallback,
                    "queue_context": context_hash,
                    "decision_context": decision_context,
                    "auto_clear_policy": settings.auto_clear_snapshot(),
                }
            )
            exact_key = party.id, entry.id, party.record_hash, entry.entry_hash
            suppression = exact_suppressions.get(exact_key)
            prior = prior_suppressions.get((party.id, entry.id))
            pair = party.id, entry.id
            previous_human = human_decisions.get(pair)
            previous_disposition = previous_human or dispositions.get(pair)
            effective_false_positive = false_positive_decisions.get(exact_key)
            if suppression and effective_false_positive is None:
                effective_false_positive = db.get(Disposition, suppression.disposition_id)
            positive = positive_decisions.get(pair)
            if (
                suppression
                and positive is not None
                and effective_false_positive is not None
                and (positive.created_at, positive.id)
                > (effective_false_positive.created_at, effective_false_positive.id)
            ):
                suppression = None
            completed_decision = None
            completed_anchor = None
            if previous_human is not None and previous_human.kind in {
                "confirmed_match",
                "escalated",
            }:
                decision_hit = decision_hits[previous_human.hit_id]
                if (decision_hit.party_hash, decision_hit.entry_hash) == (
                    party.record_hash,
                    entry.entry_hash,
                ):
                    completed_decision, completed_anchor = previous_human, decision_hit
            if suppression:
                explanation["suppression_disposition_id"] = (
                    effective_false_positive.id
                    if effective_false_positive
                    else suppression.disposition_id
                )
                explanation["suppression_record_id"] = suppression.id
            elif prior or previous_disposition:
                if previous_disposition is not None:
                    disposition_id = previous_disposition.id
                else:
                    assert prior is not None
                    disposition_id = prior.disposition_id
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
                prior_run = db.get(ScreeningRun, prior_hit.run_id)
                assert prior_run is not None
                prior_rule = prior_run.settings_snapshot.get("hash_rule_version", 1)
                if prior_rule != HASH_RULE_VERSION:
                    explanation["hash_rule_change"] = {
                        "previous": prior_rule,
                        "current": HASH_RULE_VERSION,
                        "reason": "Type-aware hash policy invalidated the prior remembered pairing",
                    }
            result = Adjudication()
            if (
                adjudicate
                and not suppression
                and not completed_decision
                and scored.score >= settings.review_threshold - 10
            ):
                result = adjudicate(
                    deepcopy(party_snapshot),
                    deepcopy(entry_snapshot),
                    scored.score,
                    deepcopy(explanation),
                    settings,
                )
            confidence = (
                scored.score if result.final_confidence is None else result.final_confidence
            )
            if not 0 <= confidence <= 100 or (
                result.model_prob is not None and not 0 <= result.model_prob <= 1
            ):
                raise ScreeningValidationError("Adjudication returned invalid confidence")
            hit_bucket = bucket(confidence, settings, scored.score)
            auto_clear = (
                not suppression
                and not completed_decision
                and settings.auto_clear_enabled
                and hit_bucket == "review"
                and confidence < settings.auto_clear_threshold
            )
            anchor = (
                None
                if auto_clear
                else completed_anchor
                or (anchors.get((*exact_key, context_hash)) if hit_bucket != "low" else None)
            )
            if anchor and not suppression:
                explanation["queue_anchor_hit_id"] = anchor.id
            if completed_decision is not None:
                explanation["completed_disposition_id"] = completed_decision.id
            if entry.id in forced.get(party.id, set()):
                explanation["prior_pair_forced_candidate"] = True
            if auto_clear:
                explanation["auto_clear"] = {
                    "threshold": settings.auto_clear_threshold,
                    "acknowledgment_audit_id": settings.auto_clear_acknowledgment_audit_id,
                }
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
            counts[hit_bucket] += 1
            if suppression:
                run.hits_suppressed += 1
                counts["suppressed"] += 1
            elif auto_clear:
                db.flush()
                requested_actor = db.info.pop("actor_user_id", None)
                try:
                    model = result.model_name or "none (deterministic-only)"
                    decision = Disposition(
                        hit_id=hit.id,
                        kind="auto_cleared",
                        actor_user_id=None,
                        slider_value_at_time=settings.auto_clear_threshold,
                        note=f"Auto-clear in run {run.id}; "
                        f"threshold={settings.auto_clear_threshold}; "
                        f"model={model}; det_score={scored.score}; "
                        f"model_prob={result.model_prob}; final_confidence={confidence}.",
                    )
                    db.add(decision)
                    db.flush()
                    hit.status = "dispositioned"
                    audit(
                        db,
                        "screening.auto_cleared",
                        "dispositions",
                        decision.id,
                        after={
                            "run_id": run.id,
                            "hit_id": hit.id,
                            "party_id": party.id,
                            "threshold": settings.auto_clear_threshold,
                            "det_score": scored.score,
                            "model_prob": result.model_prob,
                            "final_confidence": confidence,
                            "model_name": result.model_name,
                            "decision_context": decision_context,
                            "acknowledgment_audit_id": settings.auto_clear_acknowledgment_audit_id,
                        },
                    )
                    db.flush()
                finally:
                    if requested_actor is not None:
                        db.info["actor_user_id"] = requested_actor
                run.auto_cleared += 1
                counts["auto_cleared"] += 1
            elif hit_bucket != "low" and not anchor:
                run.hits_new += 1
                counts["new_queue"] += 1
            if completed_decision is not None:
                counts["completed_human"] += 1
            if result.model_name:
                run.model_name = result.model_name
            if result.fallback:
                run.model_fallbacks = [*run.model_fallbacks, redact_credentials(result.fallback)]
        source = sources[party.source_id]
        db.add(
            RunPartyEvidence(
                run_id=run.id,
                party_id=party.id,
                party_hash=party.record_hash,
                party_snapshot=original_party,
                source_snapshot={"id": source.id, "kind": source.kind, "label": source.label},
                scoring_context=context_hash,
                decision_context=decision_context,
                candidates_considered=len(candidates),
                hits_recorded=sum(counts[key] for key in ("low", "review", "likely")),
                result_counts=counts,
            )
        )
        # Audit listeners can otherwise accumulate an entire book in one flush.
        if run.parties_screened % 100 == 0:
            db.flush()
    db.flush()
    from app.notifications.service import enqueue_likely_alert

    enqueue_likely_alert(db, run)
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
    tasks = list(
        db.execute(
            select(ScreeningTask.id, ScreeningTask.attempts)
            .where(ScreeningTask.status.in_(["pending", "failed", "running"]))
            .order_by(ScreeningTask.created_at, ScreeningTask.id)
        )
    )
    runs = []
    failures: list[dict[str, Any]] = []
    for task_id, previous_attempts in tasks:
        try:
            _begin(db)
        except ScreeningBusyError:
            # Keep pending/running state intact. Earlier independent commits must
            # remain visible in the aggregate even when another writer takes over.
            failures.append(
                {
                    "task_id": task_id,
                    "attempts": previous_attempts,
                    "error": "Screening remains pending while the database is busy",
                }
            )
            break
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
            failures.append(
                {"task_id": task.id, "attempts": task.attempts, "error": task.last_error}
            )
    if failures:
        raise ScreeningTasksFailedError(runs, failures)
    return runs
