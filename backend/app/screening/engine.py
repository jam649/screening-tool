"""Atomic deterministic screening and restart-safe consumption of scoped tasks.

Every comparison is immutable evidence. Repeated unresolved comparisons reference
their initial queue anchor; they are never represented as false-positive decisions.
A changed scoring context creates a fresh anchor. queue_hits selects the latest
context for each current pairing while retaining superseded open evidence in history.
"""

from collections.abc import Callable, Iterator
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from types import SimpleNamespace
from typing import Any

from sqlalchemy import exists, inspect, or_, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.adjudicators.contracts import ModelConfig
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
    User,
    UserSession,
    utcnow,
)
from app.models.evidence import RunPartyEvidence
from app.screening.candidates import CandidateIndex
from app.screening.normalize import party_record_hash
from app.screening.score import bucket, score_pair
from app.screening.settings import ScreeningSettings, ScreeningValidationError, load_settings
from app.screening.writer import copy_tree, paused_gc


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
    metadata: dict[str, Any] | None = None


AdjudicationHook = Callable[[dict, dict, float, dict, ScreeningSettings], Adjudication]


@dataclass(frozen=True)
class PreparedPair:
    entry: dict[str, Any]
    score: float
    explanation: dict[str, Any]
    eligible: bool


@dataclass(frozen=True)
class PreparedScreening:
    settings: ScreeningSettings
    model: ModelConfig
    expected_model: ModelConfig
    version_id: str
    started_at: datetime
    parties: list[dict[str, Any]]
    entries: list[dict[str, Any]]
    sources: dict[str, dict[str, Any]]
    pairs: dict[str, list[PreparedPair]]
    candidate_counts: dict[str, int]
    run_snapshot: dict[str, Any]


def _model_policy(db: Session) -> ModelConfig:
    from app.adjudicators.settings import load_model_config

    config = db.info.get("config")
    return load_model_config(db, config) if config is not None else ModelConfig()


def _validate_prepared(db: Session, prepared: PreparedScreening) -> None:
    # Inference hooks can expose a previously loaded anchor; never let the
    # identity map retain its pre-inference status across the final writer.
    if db.new or db.dirty or db.deleted:
        raise ScreeningValidationError("Commit pending changes before starting screening")
    db.expire_all()
    execution = db.info.get("operation_execution")
    if execution is not None:
        execution.check_fence(db)
    actor_id = db.info.get("actor_user_id")
    actor = db.get(User, actor_id, populate_existing=True) if actor_id else None
    if actor_id and (actor is None or not actor.active or actor.role != "admin"):
        from app.scheduler.leases import OperationAuthorityError

        raise OperationAuthorityError("Active Admin role required")
    session_id = db.info.get("auth_session_id")
    if session_id:
        authenticated = db.get(UserSession, session_id, populate_existing=True)
        if (
            authenticated is None
            or authenticated.user_id != actor_id
            or authenticated.expires_at <= utcnow()
        ):
            from app.scheduler.leases import OperationAuthorityError

            raise OperationAuthorityError("Active Admin session required")
    if load_settings(db) != prepared.settings or _model_policy(db) != prepared.expected_model:
        raise ScreeningValidationError("Screening policy changed; retry screening")


def _validate_adjudication(result: Adjudication) -> None:
    for value, maximum in ((result.model_prob, 1), (result.final_confidence, 100)):
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
            or not 0 <= value <= maximum
        ):
            raise ScreeningValidationError("Adjudication returned invalid confidence")


def _infer(
    db: Session, prepared: PreparedScreening, adjudicate: AdjudicationHook | None
) -> dict[tuple[str, str], Adjudication]:
    # No ORM object or transaction is retained across an outbound model call.
    db.rollback()
    results = {}
    if adjudicate is not None:
        for party in prepared.parties:
            for pair in prepared.pairs[party["id"]]:
                if pair.eligible:
                    # A previous call may outlive a policy/authority revocation.
                    # Observe it before releasing any further identity evidence,
                    # while retaining the final atomic writer validation too.
                    try:
                        _validate_prepared(db, prepared)
                    finally:
                        db.rollback()
                    result = adjudicate(
                        deepcopy(party),
                        deepcopy(pair.entry),
                        pair.score,
                        deepcopy(pair.explanation),
                        prepared.settings,
                    )
                    _validate_adjudication(result)
                    results[party["id"], pair.entry["id"]] = deepcopy(result)
    return results


def snapshot(record: Any) -> dict[str, Any]:
    result = {}
    for column in inspect(record).mapper.column_attrs:
        value = getattr(record, column.key)
        result[column.key] = value.isoformat() if isinstance(value, datetime) else deepcopy(value)
    return redact_credentials(result)


def run_payload(run: ScreeningRun) -> dict[str, Any]:
    return snapshot(run)


@dataclass(frozen=True, slots=True)
class HitReference:
    """Historical authority/context without materializing private snapshots."""

    id: str
    run_id: str
    party_id: str
    csl_entry_id: str
    party_hash: str
    entry_hash: str
    status: str
    bucket: str
    queue_context: str | None
    queue_anchor_hit_id: str | None


def _hit_reference_query():
    return select(
        Hit.id,
        Hit.run_id,
        Hit.party_id,
        Hit.csl_entry_id,
        Hit.party_hash,
        Hit.entry_hash,
        Hit.status,
        Hit.bucket,
        Hit.explanation["queue_context"].as_string(),
        Hit.explanation["queue_anchor_hit_id"].as_string(),
    )


def _hit_references(db: Session, query) -> Iterator[HitReference]:
    for row in db.execute(query).yield_per(500):
        yield HitReference(*row)


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
        _hit_reference_query()
        .join(ScreeningRun, Hit.run_id == ScreeningRun.id)
        .join(Party, Hit.party_id == Party.id)
        .join(CSLEntry, Hit.csl_entry_id == CSLEntry.id)
        .where(
            Party.deleted_at.is_(None),
            CSLEntry.removed_version.is_(None),
            Hit.party_hash == Party.record_hash,
            Hit.entry_hash == CSLEntry.entry_hash,
        )
        # Run ties use the same order as covering scopes and writer anchors.
        # Hit insertion times cannot make an older tied run appear current.
        .order_by(
            ScreeningRun.started_at.desc(),
            ScreeningRun.id.desc(),
            Hit.created_at.desc(),
            Hit.id.desc(),
        )
    )
    if enabled_lists is not None:
        query = query.where(CSLEntry.source_list.in_(enabled_lists))
    if party_id is not None:
        query = query.where(Hit.party_id == party_id)
    if entry_id is not None:
        query = query.where(Hit.csl_entry_id == entry_id)
    rows = _hit_references(db, query)
    latest: dict[tuple[str, str], HitReference] = {}
    for hit in rows:
        key = hit.party_id, hit.csl_entry_id
        latest.setdefault(key, hit)
    if not latest:
        return []
    pending = {
        pair: observation
        for pair, observation in latest.items()
        if observation.status == "open" and observation.bucket != "low"
    }
    del latest
    if not pending:
        return []
    pending_by_party: dict[str, set[str]] = {}
    for party, entry in pending:
        pending_by_party.setdefault(party, set()).add(entry)
    relevant = (
        select(RunPartyEvidence.run_id)
        .join(Party)
        .where(
            RunPartyEvidence.party_id.in_(pending_by_party),
            RunPartyEvidence.party_hash == Party.record_hash,
        )
    )
    legacy = ~exists(select(RunPartyEvidence.id).where(RunPartyEvidence.run_id == ScreeningRun.id))
    # Sort identifiers, never whole manifests: an SQLite temporary ORDER BY
    # must not retain every historical JSON scope before the first result.
    runs = db.execute(
        select(ScreeningRun.id)
        .where(
            ScreeningRun.finished_at.is_not(None),
            or_(ScreeningRun.id.in_(relevant), legacy),
        )
        .order_by(ScreeningRun.started_at.desc(), ScreeningRun.id.desc())
    ).yield_per(500)
    active_ids = set()
    try:
        for (run_id,) in runs:
            scope = db.scalar(
                select(ScreeningRun.settings_snapshot["scope"]).where(ScreeningRun.id == run_id)
            )
            if scope is None:
                continue
            party_ids = None if scope["party_ids"] is None else set(scope["party_ids"])
            entry_ids = None if scope["entry_ids"] is None else set(scope["entry_ids"])
            party_hashes, entry_hashes = scope.get("party_hashes"), scope.get("entry_hashes")
            for party_id in list(
                pending_by_party.keys() & party_ids if party_ids is not None else pending_by_party
            ):
                entries = pending_by_party[party_id]
                for entry_id in list(entries & entry_ids if entry_ids is not None else entries):
                    observation = pending[party_id, entry_id]
                    if (
                        party_hashes is not None
                        and party_hashes.get(party_id) != observation.party_hash
                        or entry_hashes is not None
                        and entry_hashes.get(entry_id) != observation.entry_hash
                    ):
                        continue
                    # The newest covering scope resolves this pair even with
                    # zero hits. Older scopes cannot resurrect an old anchor.
                    if run_id == observation.run_id:
                        active_ids.add(observation.queue_anchor_hit_id or observation.id)
                    del pending[party_id, entry_id]
                    entries.remove(entry_id)
                if not entries:
                    del pending_by_party[party_id]
            if not pending:
                break
    finally:
        runs.close()
    # Preserve legacy observations with no explicit covering manifest at all.
    active_ids.update(row.queue_anchor_hit_id or row.id for row in pending.values())
    # Preserve complete original objects at the public boundary, loading only
    # returned anchors rather than every prior low/suppressed/repeat snapshot.
    active: list[Hit] = []
    ids = sorted(active_ids)
    for start in range(0, len(ids), 900):
        active.extend(
            db.scalars(
                select(Hit)
                .where(
                    Hit.id.in_(ids[start : start + 900]),
                    Hit.status == "open",
                )
                .execution_options(populate_existing=True)
            )
        )
    return sorted(
        active,
        key=lambda hit: (hit.bucket != "likely", -hit.final_confidence, hit.created_at),
    )


def queue_hit_is_current(db: Session, hit: Hit) -> bool:
    """Validate one selected pair without materializing the entire book's queue."""
    return any(
        row.id == hit.id for row in queue_hits(db, party_id=hit.party_id, entry_id=hit.csl_entry_id)
    )


def current_scoring_context(db: Session) -> str:
    return canonical_hash(
        {**load_settings(db).scoring_snapshot(), "model": _model_policy(db).scoring_snapshot()}
    )


def _bypassed_pairs(db: Session, parties: set[str]) -> set[tuple[str, str, str, str]]:
    hits = {
        hit.id: hit
        for hit in _hit_references(db, _hit_reference_query().where(Hit.party_id.in_(parties)))
    }
    humans: dict[tuple[str, str], Disposition] = {}
    positives: dict[tuple[str, str], Disposition] = {}
    false_positives: dict[tuple[str, str, str, str], Disposition] = {}
    for decision in db.scalars(
        select(Disposition)
        .join(Hit)
        .where(Hit.party_id.in_(parties), Disposition.actor_user_id.is_not(None))
        .order_by(Disposition.created_at.desc(), Disposition.id.desc())
    ):
        hit = hits[decision.hit_id]
        pair = hit.party_id, hit.csl_entry_id
        humans.setdefault(pair, decision)
        if decision.kind in {"confirmed_match", "escalated"}:
            positives.setdefault(pair, decision)
        elif decision.kind == "false_positive":
            false_positives.setdefault((*pair, hit.party_hash, hit.entry_hash), decision)
    bypassed = set()
    for pair, decision in humans.items():
        if decision.kind in {"confirmed_match", "escalated"}:
            hit = hits[decision.hit_id]
            bypassed.add((*pair, hit.party_hash, hit.entry_hash))
    for suppression in db.scalars(select(Suppression).where(Suppression.party_id.in_(parties))):
        pair = suppression.party_id, suppression.csl_entry_id
        key = *pair, suppression.party_hash, suppression.entry_hash
        fp = false_positives.get(key) or db.get(Disposition, suppression.disposition_id)
        positive = positives.get(pair)
        if (
            positive is None
            or fp is None
            or (positive.created_at, positive.id) <= (fp.created_at, fp.id)
        ):
            bypassed.add(key)
    return bypassed


def _prepare(
    db: Session,
    *,
    party_ids: list[str] | None,
    entry_ids: list[str] | None,
    csl_version_id: str | None,
    adjudicate: AdjudicationHook | None,
    model_config: ModelConfig | None,
) -> tuple[PreparedScreening, AdjudicationHook | None]:
    if db.new or db.dirty or db.deleted:
        raise ScreeningValidationError("Commit pending changes before starting screening")
    db.rollback()
    # sqlite3's legacy mode does not start a transaction for SELECT. An explicit
    # read transaction pins party, list-version and policy snapshots together;
    # WAL still permits connector writes, and rollback releases it before HTTP.
    db.execute(text("BEGIN"))
    started_at = utcnow()
    if adjudicate is None and model_config is None:
        from app.adjudicators.runner import build_adjudication_hook

        model, adjudicate = build_adjudication_hook(db)
        expected_model = model
    else:
        expected_model = _model_policy(db)
        # Explicit hook/policy injection is internal and used only by tests.
        model = model_config or expected_model
    version = current_version(db)
    if version is None:
        raise ScreeningValidationError("Fetch the CSL before screening parties")
    settings = load_settings(db)
    context_hash = canonical_hash(
        {**settings.scoring_snapshot(), "model": model.scoring_snapshot()}
    )
    auto_clear_policy = settings.auto_clear_snapshot()
    decision_context = canonical_hash(
        {"scoring_context": context_hash, "auto_clear_policy": auto_clear_policy}
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
    if any(
        party.record_hash
        != party_record_hash(
            {
                key: getattr(party, key)
                for key in (
                    "party_type",
                    "name",
                    "alt_names",
                    "address_lines",
                    "city",
                    "region",
                    "postal_code",
                    "country_iso2",
                )
            }
        )
        for party in parties
    ) or any(
        entry.entry_hash
        != csl_entry_hash(
            {
                key: getattr(entry, key)
                for key in (
                    "name",
                    "alt_names",
                    "addresses",
                    "countries",
                    "programs",
                    "entity_type",
                )
            }
        )
        for entry in entries
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
    run_snapshot = {
        **settings.snapshot(),
        "model": model.scoring_snapshot(),
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
    party_set = {party.id for party in parties}
    bypassed = _bypassed_pairs(db, party_set)
    entries_by_id = {entry.id: entry for entry in entries}
    hashes = {party.id: party.record_hash for party in parties}
    forced: dict[str, set[str]] = {}
    for hit in _hit_references(
        db,
        _hit_reference_query().where(
            Hit.party_id.in_(party_set), Hit.status == "open", Hit.bucket != "low"
        ),
    ):
        entry = entries_by_id.get(hit.csl_entry_id)
        if (
            entry is not None
            and hit.party_hash == hashes[hit.party_id]
            and hit.entry_hash == entry.entry_hash
        ):
            forced.setdefault(hit.party_id, set()).add(entry.id)
    source_snapshots = {
        source.id: {"id": source.id, "kind": source.kind, "label": source.label}
        for source in db.scalars(
            select(Source).where(Source.id.in_({p.source_id for p in parties}))
        )
    }
    index = CandidateIndex(entries, settings)
    # One immutable prepared snapshot per entry; actual persisted Hit snapshots
    # and inference inputs are still copied independently before use.
    entry_snapshots = {entry.id: snapshot(entry) for entry in entries}
    pairs: dict[str, list[PreparedPair]] = {}
    candidate_counts = {}
    for party in parties:
        candidates, fallback = index.candidates(party)
        candidate_ids = {entry.id for entry in candidates}
        candidates.extend(
            entries_by_id[id] for id in sorted(forced.get(party.id, set()) - candidate_ids)
        )
        candidate_counts[party.id] = len(candidates)
        pairs[party.id] = []
        for entry in candidates:
            scored = score_pair(party, entry, settings, explanation_minimum=settings.candidate_min)
            if scored.score < settings.candidate_min:
                continue
            explanation = deepcopy(scored.explanation)
            explanation.update(
                {
                    "blocking_fallback": fallback,
                    "queue_context": context_hash,
                    "decision_context": decision_context,
                    "auto_clear_policy": auto_clear_policy,
                }
            )
            if entry.id in forced.get(party.id, set()):
                explanation["prior_pair_forced_candidate"] = True
            eligible = (
                party.id,
                entry.id,
                party.record_hash,
                entry.entry_hash,
            ) not in bypassed and scored.score >= settings.review_threshold - 10
            pairs[party.id].append(
                PreparedPair(entry_snapshots[entry.id], scored.score, explanation, eligible)
            )
    prepared = PreparedScreening(
        settings,
        model,
        expected_model,
        csl_version_id or version.id,
        started_at,
        [snapshot(p) for p in parties],
        list(entry_snapshots.values()),
        source_snapshots,
        pairs,
        candidate_counts,
        run_snapshot,
    )
    db.rollback()
    return prepared, adjudicate


@paused_gc
def _execute(
    db: Session,
    *,
    trigger: str,
    party_ids: list[str] | None,
    entry_ids: list[str] | None,
    csl_version_id: str | None,
    adjudicate: AdjudicationHook | None = None,
    prepared: PreparedScreening | None = None,
    results: dict[tuple[str, str], Adjudication] | None = None,
) -> ScreeningRun:
    assert prepared is not None
    settings = prepared.settings
    context_hash = prepared.run_snapshot["scoring_context"]
    decision_context = prepared.run_snapshot["decision_context"]
    parties = [SimpleNamespace(**values) for values in prepared.parties]
    entries = [SimpleNamespace(**values) for values in prepared.entries]
    run_snapshot = deepcopy(prepared.run_snapshot)
    call_summary = {"calls": len(results or {}), "superseded": 0, "fallbacks": 0}
    run = ScreeningRun(
        trigger=trigger,
        csl_version_id=prepared.version_id,
        started_at=prepared.started_at,
        settings_snapshot=run_snapshot,
    )
    db.add(run)
    db.flush()
    party_set = {party.id for party in parties}
    existing_hits = list(
        _hit_references(
            db,
            _hit_reference_query()
            .join(ScreeningRun)
            .where(Hit.party_id.in_(party_set))
            .order_by(
                ScreeningRun.started_at.desc(),
                ScreeningRun.id.desc(),
                Hit.created_at.desc(),
                Hit.id.desc(),
            ),
        )
    )
    anchors: dict[tuple, HitReference] = {}
    latest_anchors: set[tuple[str, str]] = set()
    for reference in existing_hits:
        pair = reference.party_id, reference.csl_entry_id
        key = (*pair, reference.party_hash, reference.entry_hash, reference.queue_context)
        if not reference.queue_anchor_hit_id and pair not in latest_anchors:
            latest_anchors.add(pair)
            if reference.status == "open" and reference.bucket != "low":
                anchors[key] = reference
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
    forced: dict[str, set[str]] = {}
    for reference in existing_hits:
        current_entry = scoped_entries.get(reference.csl_entry_id)
        if (
            reference.status == "open"
            and reference.bucket != "low"
            and current_entry is not None
            and reference.party_hash == party_hashes[reference.party_id]
            and reference.entry_hash == current_entry.entry_hash
        ):
            forced.setdefault(reference.party_id, set()).add(reference.csl_entry_id)
    # Run counters are assigned once before completion. Every intermediate UPDATE
    # rewrote the whole run row, including its scope manifest, inside this same
    # transaction, so no reader could ever observe those values.
    totals = dict.fromkeys(("parties_screened", "hits_new", "hits_suppressed", "auto_cleared"), 0)
    for party in parties:
        original_party = copy_tree(vars(party))
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
        totals["parties_screened"] += 1
        for comparison in prepared.pairs[party.id]:
            entry = SimpleNamespace(**comparison.entry)
            scored = SimpleNamespace(score=comparison.score)
            party_snapshot, entry_snapshot = copy_tree(original_party), copy_tree(comparison.entry)
            explanation = copy_tree(comparison.explanation)
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
            observed = (results or {}).get((party.id, entry.id), Adjudication())
            result = Adjudication() if suppression or completed_decision else observed
            _validate_adjudication(result)
            confidence = (
                scored.score
                if result.model_prob is None
                else round(
                    prepared.model.w_det * scored.score
                    + prepared.model.w_model * 100 * result.model_prob
                )
            )
            if observed.metadata is not None:
                explanation["adjudication"] = deepcopy(observed.metadata)
            if (party.id, entry.id) in (results or {}) and (suppression or completed_decision):
                explanation["adjudication_superseded_by_human"] = True
                call_summary["superseded"] += 1
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
                totals["hits_suppressed"] += 1
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
                totals["auto_cleared"] += 1
                counts["auto_cleared"] += 1
            elif hit_bucket != "low" and not anchor:
                totals["hits_new"] += 1
                counts["new_queue"] += 1
            if completed_decision is not None:
                counts["completed_human"] += 1
            if observed.model_name:
                run.model_name = observed.model_name
            if observed.fallback:
                fallback_proof = {
                    **redact_credentials(observed.fallback),
                    "party_id": party.id,
                    "csl_entry_id": entry.id,
                }
                run.model_fallbacks = [*run.model_fallbacks, fallback_proof]
                call_summary["fallbacks"] += 1
                audit(
                    db, "screening.model_fallback", "screening_runs", run.id, after=fallback_proof
                )
        source_snapshot = prepared.sources[party.source_id]
        db.add(
            RunPartyEvidence(
                run_id=run.id,
                party_id=party.id,
                party_hash=party.record_hash,
                party_snapshot=original_party,
                source_snapshot=copy_tree(source_snapshot),
                scoring_context=context_hash,
                decision_context=decision_context,
                candidates_considered=prepared.candidate_counts[party.id],
                hits_recorded=sum(counts[key] for key in ("low", "review", "likely")),
                result_counts=counts,
            )
        )
        # Audit listeners can otherwise accumulate an entire book in one flush.
        if totals["parties_screened"] % 100 == 0:
            db.flush()
    db.flush()
    from app.notifications.service import enqueue_likely_alert

    for counter, total in totals.items():
        setattr(run, counter, total)
    run.settings_snapshot = {**run_snapshot, "adjudication_summary": call_summary}
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
    model_config: ModelConfig | None = None,
) -> ScreeningRun:
    if db.new or db.dirty or db.deleted:
        raise ScreeningValidationError("Commit pending changes before starting screening")
    try:
        prepared, hook = _prepare(
            db,
            party_ids=party_ids,
            entry_ids=entry_ids,
            csl_version_id=csl_version_id,
            adjudicate=adjudicate,
            model_config=model_config,
        )
        results = _infer(db, prepared, hook)
        _begin(db)
        _validate_prepared(db, prepared)
        run = _execute(
            db,
            trigger=trigger,
            party_ids=party_ids,
            entry_ids=entry_ids,
            csl_version_id=csl_version_id,
            prepared=prepared,
            results=results,
        )
        db.commit()
        return run
    except Exception:
        db.rollback()
        raise


def process_pending_tasks(
    db: Session,
    *,
    adjudicate: AdjudicationHook | None = None,
    model_config: ModelConfig | None = None,
) -> list[ScreeningRun]:
    """Infer without a writer; commit each complete task and its evidence atomically.

    A crash before application leaves the durable task untouched. Each independent
    task can complete even when another fails. No partially committed comparison
    is exposed, and sanitized diagnostics never include provider exception text.
    """
    if db.new or db.dirty or db.deleted:
        raise ScreeningValidationError("Commit pending changes before starting screening")
    tasks = list(
        db.execute(
            select(ScreeningTask.id, ScreeningTask.attempts)
            .where(ScreeningTask.status.in_(["pending", "failed", "running"]))
            .order_by(ScreeningTask.created_at, ScreeningTask.id)
        )
    )
    db.rollback()
    runs = []
    failures: list[dict[str, Any]] = []
    for task_id, previous_attempts in tasks:
        try:
            task = db.get(ScreeningTask, task_id, populate_existing=True)
            if task is None or task.status == "complete":
                db.rollback()
                continue
            trigger, task_version = task.trigger, task.csl_version_id
            entry_ids = deepcopy(task.entry_ids) if trigger == "list_change" else None
            active_party_ids = None
            if task.party_ids:
                active_party_ids = list(
                    db.scalars(
                        select(Party.id).where(
                            Party.id.in_(task.party_ids), Party.deleted_at.is_(None)
                        )
                    )
                )
            prepared, hook = _prepare(
                db,
                party_ids=active_party_ids,
                entry_ids=entry_ids,
                csl_version_id=task_version if trigger == "list_change" else None,
                adjudicate=adjudicate,
                model_config=model_config,
            )
            results = _infer(db, prepared, hook)
            _begin(db)
            _validate_prepared(db, prepared)
            task = db.get(ScreeningTask, task_id, populate_existing=True)
            if task is None or task.status == "complete":
                db.rollback()
                continue
            task.status = "running"
            task.attempts += 1
            task.started_at = prepared.started_at
            task.last_error = None
            run = _execute(
                db,
                trigger=trigger,
                party_ids=active_party_ids,
                entry_ids=entry_ids,
                csl_version_id=task_version if trigger == "list_change" else None,
                prepared=prepared,
                results=results,
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
        except ScreeningBusyError:
            db.rollback()
            failures.append(
                {
                    "task_id": task_id,
                    "attempts": previous_attempts,
                    "error": "Screening remains pending while the database is busy",
                }
            )
            break
        except Exception as exc:
            db.rollback()
            # Fence/authority failures must not write task state under revoked ownership.
            from app.scheduler.leases import OperationAuthorityError, OperationLeaseLostError

            if isinstance(exc, (OperationAuthorityError, OperationLeaseLostError)):
                raise
            _begin(db)
            task = db.get(ScreeningTask, task_id, populate_existing=True)
            if task is None or task.status == "complete":
                db.rollback()
                continue
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
    # A later task's preparation rollback expires earlier ORM rows. Return
    # complete immutable records that remain readable after the operation's
    # Session closes, including partial-success diagnostics.
    for completed_run in runs:
        db.refresh(completed_run)
        db.expunge(completed_run)
    if failures:
        raise ScreeningTasksFailedError(runs, failures)
    return runs
