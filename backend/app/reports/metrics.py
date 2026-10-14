"""Shared home/report facts derived from completed, explicit run membership."""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    CSLEntry,
    CSLEntryRevision,
    CSLFetch,
    CSLVersion,
    Disposition,
    Hit,
    Party,
    ScreeningRun,
    Setting,
    Source,
    utcnow,
)
from app.screening.engine import queue_hits
from app.screening.settings import load_settings


def party_screenings(db: Session):
    parties = {p.id: p for p in db.scalars(select(Party))}
    runs = list(
        db.scalars(
            select(ScreeningRun)
            .where(ScreeningRun.finished_at.is_not(None))
            .order_by(ScreeningRun.finished_at.desc())
        )
    )
    observations: dict[str, list[ScreeningRun]] = {id: [] for id in parties}
    legacy_membership: dict[str, set[str]] = {}
    for run_id, party_id in db.execute(select(Hit.run_id, Hit.party_id).distinct()):
        legacy_membership.setdefault(run_id, set()).add(party_id)
    for run in runs:
        scope = run.settings_snapshot.get("scope") or {}
        ids = scope.get("party_ids")
        members = ids if isinstance(ids, list) else legacy_membership.get(run.id, set())
        for party_id in members:
            if party_id in observations:
                observations[party_id].append(run)
    return observations


def current_screening_coverage(db: Session, histories=None):
    """Prove complete current-list coverage by unioning actual immutable inputs.

    A one-entry delta cannot establish whole-list coverage for a newly added party.
    Older full comparisons plus current deltas can establish coverage, including
    no-hit comparisons. Input manifests also survive metadata-only list versions.
    """
    histories = party_screenings(db) if histories is None else histories
    settings = load_settings(db)
    required_query = select(CSLEntry).where(CSLEntry.removed_version.is_(None))
    if settings.enabled_source_lists is not None:
        required_query = required_query.where(
            CSLEntry.source_list.in_(settings.enabled_source_lists)
        )
    required = {entry.id: entry.entry_hash for entry in db.scalars(required_query)}
    current_settings = settings.snapshot()
    unique_runs = {run.id: run for history in histories.values() for run in history}
    legacy_versions = {
        run.csl_version_id
        for run in unique_runs.values()
        if not isinstance((run.settings_snapshot.get("scope") or {}).get("entry_hashes"), dict)
    }
    revision_hashes = {
        (version_id, entry_id): entry_hash
        for version_id, entry_id, entry_hash in db.execute(
            select(
                CSLEntryRevision.version_id,
                CSLEntryRevision.entry_id,
                CSLEntryRevision.snapshot["entry_hash"].as_string(),
            ).where(CSLEntryRevision.version_id.in_(legacy_versions))
        )
    }
    covered_by_run: dict[str, frozenset[str]] = {}
    for run in unique_runs.values():
        scope = run.settings_snapshot.get("scope") or {}
        entry_ids = scope.get("entry_ids")
        if not isinstance(entry_ids, list) or any(
            run.settings_snapshot.get(key) != value for key, value in current_settings.items()
        ):
            covered_by_run[run.id] = frozenset()
            continue
        manifest = scope.get("entry_hashes")
        hashes = (
            manifest
            if isinstance(manifest, dict)
            else {
                entry_id: revision_hashes.get((run.csl_version_id, entry_id))
                for entry_id in entry_ids
            }
        )
        covered_by_run[run.id] = frozenset(
            entry_id
            for entry_id in entry_ids
            if entry_id in required and hashes.get(entry_id) == required[entry_id]
        )
    # Full-book runs give many parties identical coverage sets. Compute each shared
    # signature once instead of scanning every listing once for every party.
    signatures = {}
    coverage = {}
    for party in db.scalars(select(Party)):
        signature = tuple(
            run.id
            for run in histories.get(party.id, [])
            if (run.settings_snapshot.get("scope") or {}).get("party_hashes", {}).get(party.id)
            == party.record_hash
        )
        if signature not in signatures:
            remaining = set(required)
            for run_id in signature:
                remaining.difference_update(covered_by_run[run_id])
                if not remaining:
                    break
            signatures[signature] = bool(required) and bool(signature) and not remaining
        coverage[party.id] = signatures[signature]
    return coverage


def report_boundary(db: Session) -> datetime | None:
    row = db.scalar(select(Setting).where(Setting.key == "last_report_at"))
    if row and isinstance(row.value, str):
        try:
            result = datetime.fromisoformat(row.value.replace("Z", "+00:00"))
            return result if result.tzinfo else None
        except ValueError:
            pass
    return None


def auto_clear_observations(db: Session):
    """Decisions since the last successful report, linked to immutable comparisons."""
    since = report_boundary(db)
    query = (
        select(Disposition, Hit)
        .join(Hit)
        .where(Disposition.kind == "auto_cleared", Disposition.slider_value_at_time.is_not(None))
    )
    if since:
        query = query.where(Disposition.created_at > since)
    return list(db.execute(query))


def removed_entry_closure_observations(db: Session):
    since = report_boundary(db)
    query = (
        select(Disposition, Hit)
        .join(Hit)
        .where(
            Disposition.kind == "auto_cleared",
            Disposition.slider_value_at_time.is_(None),
            Disposition.note.startswith("entry removed from list in version "),
        )
    )
    if since:
        query = query.where(Disposition.created_at > since)
    return list(db.execute(query))


def dashboard_metrics(db: Session, *, now: datetime | None = None):
    now = now or utcnow()
    queue = queue_hits(db)
    latest = db.scalar(select(CSLVersion).order_by(CSLVersion.fetched_at.desc()).limit(1))
    run = db.scalar(
        select(ScreeningRun)
        .where(ScreeningRun.finished_at.is_not(None))
        .order_by(ScreeningRun.finished_at.desc())
        .limit(1)
    )
    parties = list(db.scalars(select(Party).where(Party.deleted_at.is_(None))))
    screenings = party_screenings(db)
    stale = 0
    coverage = current_screening_coverage(db, screenings)
    covered = sum(coverage.get(party.id, False) for party in parties)
    for party in parties:
        history = screenings.get(party.id, [])
        latest_current = next(
            (
                r
                for r in history
                if (r.settings_snapshot.get("scope") or {}).get("party_hashes", {}).get(party.id)
                == party.record_hash
            ),
            None,
        )
        if latest_current is None or latest_current.finished_at < now - timedelta(hours=24):
            stale += 1
    latest_fetch = (
        db.scalar(
            select(CSLFetch)
            .where(CSLFetch.status == "success", CSLFetch.version_id == latest.id)
            .order_by(CSLFetch.fetched_at.desc())
            .limit(1)
        )
        if latest
        else None
    )
    since = report_boundary(db)
    # Report item count, never repeated observations of the same four-way pair.
    cleared_pairs = {
        (hit.party_id, hit.csl_entry_id, hit.party_hash, hit.entry_hash)
        for _, hit in auto_clear_observations(db)
    }
    return {
        "open_items": len(queue),
        "likely_matches": sum(h.bucket == "likely" for h in queue),
        "auto_cleared_since_report": len(cleared_pairs),
        "removed_entry_closures_since_report": len(
            {
                (hit.party_id, hit.csl_entry_id, hit.party_hash, hit.entry_hash)
                for _, hit in removed_entry_closure_observations(db)
            }
        ),
        "last_report_at": since,
        "stale_parties": stale,
        "total_parties": len(parties),
        "parties_screened": covered,
        "coverage_latest_version": covered,
        "csl_version": {
            "id": latest.id,
            "fetched_at": latest.fetched_at,
            "entry_count": latest.entry_count,
            "added": latest.added,
            "changed": latest.changed,
            "removed": latest.removed,
            "last_fetched_at": latest_fetch.fetched_at if latest_fetch else latest.fetched_at,
            "source_list_stamps": latest_fetch.source_list_stamps
            if latest_fetch
            else latest.source_list_stamps,
        }
        if latest
        else None,
        "last_run": {"id": run.id, "finished_at": run.finished_at} if run else None,
        "sync_health": [
            {
                "id": s.id,
                "label": s.label,
                "last_synced_at": s.last_synced_at,
                "stale": s.last_synced_at is None or s.last_synced_at < now - timedelta(hours=24),
                "last_error": s.last_error,
            }
            for s in db.scalars(select(Source))
        ],
    }
