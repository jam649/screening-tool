"""Shared home/report facts derived from completed, explicit run membership."""

from datetime import datetime, timedelta

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    CSLEntry,
    CSLEntryRevision,
    CSLFetch,
    CSLVersion,
    Disposition,
    Hit,
    Party,
    RunPartyEvidence,
    ScreeningRun,
    Setting,
    Source,
    utcnow,
)
from app.screening.engine import queue_hits
from app.screening.settings import load_settings


def party_screenings(db: Session, *, party_id: str | None = None):
    party_query = select(Party.id)
    if party_id is not None:
        party_query = party_query.where(Party.id == party_id)
    parties = list(db.scalars(party_query))
    runs_query = select(ScreeningRun).where(ScreeningRun.finished_at.is_not(None))
    if party_id is not None:
        scope_members = func.json_each(
            ScreeningRun.settings_snapshot, "$.scope.party_ids"
        ).table_valued("value")
        explicit = exists(select(scope_members.c.value).where(scope_members.c.value == party_id))
        ids_type = func.json_type(ScreeningRun.settings_snapshot, "$.scope.party_ids")
        hit_membership = exists(
            select(Hit.id).where(Hit.run_id == ScreeningRun.id, Hit.party_id == party_id)
        )
        legacy = ~exists(
            select(RunPartyEvidence.id).where(RunPartyEvidence.run_id == ScreeningRun.id)
        )
        runs_query = runs_query.where(
            or_(
                ScreeningRun.id.in_(
                    select(RunPartyEvidence.run_id).where(RunPartyEvidence.party_id == party_id)
                ),
                and_(
                    legacy,
                    or_(
                        explicit, and_(or_(ids_type.is_(None), ids_type != "array"), hit_membership)
                    ),
                ),
            )
        )

        def requested_runs():
            # Complete selected-party history, without retaining all original
            # large scope manifests or sorting those payloads inside SQLite.
            identifiers = db.execute(
                runs_query.with_only_columns(ScreeningRun.id).order_by(
                    ScreeningRun.finished_at.desc()
                )
            ).yield_per(500)
            try:
                for (run_id,) in identifiers:
                    run = db.get(ScreeningRun, run_id, populate_existing=True)
                    if run is None:
                        continue
                    scope = run.settings_snapshot.get("scope") or {}
                    members = scope.get("party_ids")
                    if isinstance(members, list):
                        belongs = party_id in members
                    else:
                        belongs = bool(
                            db.scalar(
                                select(
                                    exists(
                                        select(Hit.id).where(
                                            Hit.run_id == run_id,
                                            Hit.party_id == party_id,
                                        )
                                    )
                                )
                            )
                        )
                    if belongs:
                        yield run
            finally:
                identifiers.close()

        return {party_id: requested_runs()} if parties else {}
    runs = list(db.scalars(runs_query.order_by(ScreeningRun.finished_at.desc())))
    observations: dict[str, list[ScreeningRun]] = {id: [] for id in parties}
    legacy_membership: dict[str, set[str]] = {}
    membership = select(Hit.run_id, Hit.party_id).distinct()
    if party_id is not None:
        membership = membership.where(Hit.party_id == party_id)
    for run_id, member_id in db.execute(membership):
        legacy_membership.setdefault(run_id, set()).add(member_id)
    for run in runs:
        scope = run.settings_snapshot.get("scope") or {}
        ids = scope.get("party_ids")
        members = ids if isinstance(ids, list) else legacy_membership.get(run.id, set())
        for party_id in members:
            if party_id in observations:
                observations[party_id].append(run)
    return observations


def latest_current_screenings(db: Session) -> dict[str, datetime]:
    """Latest completed participation timestamp for each actual current party hash."""
    latest: dict[str, datetime] = {
        party_id: finished_at
        for party_id, finished_at in db.execute(
            select(
                RunPartyEvidence.party_id,
                func.max(ScreeningRun.finished_at),
            )
            .select_from(RunPartyEvidence)
            .join(ScreeningRun)
            .join(Party)
            .where(
                ScreeningRun.finished_at.is_not(None),
                RunPartyEvidence.party_hash == Party.record_hash,
            )
            .group_by(RunPartyEvidence.party_id)
        ).all()
        if finished_at is not None
    }
    hashes = dict(db.execute(select(Party.id, Party.record_hash)).all())
    legacy = ~exists(select(RunPartyEvidence.id).where(RunPartyEvidence.run_id == ScreeningRun.id))
    ids = db.execute(
        select(ScreeningRun.id, ScreeningRun.finished_at)
        .where(
            ScreeningRun.finished_at.is_not(None),
            legacy,
        )
        .order_by(ScreeningRun.finished_at.desc())
    ).yield_per(500)
    try:
        for run_id, finished_at in ids:
            if finished_at is None:
                continue
            scope = db.scalar(
                select(ScreeningRun.settings_snapshot["scope"]).where(ScreeningRun.id == run_id)
            )
            if not isinstance(scope, dict) or not isinstance(scope.get("party_hashes"), dict):
                continue
            members = scope.get("party_ids")
            if not isinstance(members, list):
                members = db.scalars(select(Hit.party_id).where(Hit.run_id == run_id).distinct())
            for party_id in members:
                if (
                    party_id in hashes
                    and scope["party_hashes"].get(party_id) == hashes[party_id]
                    and (party_id not in latest or finished_at > latest[party_id])
                ):
                    latest[party_id] = finished_at
    finally:
        ids.close()
    return latest


def current_screening_coverage(db: Session, histories=None):
    """Prove complete current-list coverage by unioning actual immutable inputs.

    A one-entry delta cannot establish whole-list coverage for a newly added party.
    Older full comparisons plus current deltas can establish coverage, including
    no-hit comparisons. Input manifests also survive metadata-only list versions.
    """
    # The optional histories argument is retained for internal callers, but
    # current coverage is proven from the database, not an earlier projection.
    # Never retain a second copy of every historical full scope manifest.
    settings = load_settings(db)
    required_query = select(CSLEntry).where(CSLEntry.removed_version.is_(None))
    if settings.enabled_source_lists is not None:
        required_query = required_query.where(
            CSLEntry.source_list.in_(settings.enabled_source_lists)
        )
    required = dict(
        db.execute(required_query.with_only_columns(CSLEntry.id, CSLEntry.entry_hash)).all()
    )
    from app.screening.engine import current_scoring_context

    current_context = current_scoring_context(db)
    party_hashes = dict(db.execute(select(Party.id, Party.record_hash)).all())
    coverage = dict.fromkeys(party_hashes, False)
    if not required or not party_hashes:
        return coverage
    # A bounded integer bitmap costs about 3 KiB per distinct partial party
    # state at 25k entries, instead of a Python set of 25k strings per party.
    positions = {entry_id: position for position, entry_id in enumerate(required)}
    complete = (1 << len(required)) - 1
    pending = dict.fromkeys(party_hashes, 0)
    relevant = (
        select(RunPartyEvidence.run_id)
        .join(Party)
        .where(RunPartyEvidence.party_hash == Party.record_hash)
    )
    legacy = ~exists(select(RunPartyEvidence.id).where(RunPartyEvidence.run_id == ScreeningRun.id))
    runs = db.execute(
        select(ScreeningRun.id)
        .where(
            ScreeningRun.finished_at.is_not(None),
            ScreeningRun.settings_snapshot["scoring_context"].as_string() == current_context,
            or_(ScreeningRun.id.in_(relevant), legacy),
        )
        .order_by(ScreeningRun.finished_at.desc(), ScreeningRun.id.desc())
    ).yield_per(500)
    try:
        for (run_id,) in runs:
            has_evidence, pending_evidence = db.execute(
                select(
                    exists(select(RunPartyEvidence.id).where(RunPartyEvidence.run_id == run_id)),
                    exists(
                        select(RunPartyEvidence.id).where(
                            RunPartyEvidence.run_id == run_id,
                            RunPartyEvidence.party_id.in_(pending),
                        )
                    ),
                )
            ).one()
            if has_evidence and not pending_evidence:
                continue
            scope, version_id = db.execute(
                select(
                    ScreeningRun.settings_snapshot["scope"],
                    ScreeningRun.csl_version_id,
                ).where(ScreeningRun.id == run_id)
            ).one()
            if not isinstance(scope, dict) or not isinstance(scope.get("entry_ids"), list):
                continue
            hashes = scope.get("entry_hashes")
            if not isinstance(hashes, dict):
                # Legacy interpretation uses immutable revision hashes, never
                # reconstructed current hashes; retain only one version's map.
                hashes = dict(
                    db.execute(
                        select(
                            CSLEntryRevision.entry_id,
                            CSLEntryRevision.snapshot["entry_hash"].as_string(),
                        ).where(CSLEntryRevision.version_id == version_id)
                    ).all()
                )
            run_mask = 0
            for entry_id in scope["entry_ids"]:
                if entry_id in required and hashes.get(entry_id) == required[entry_id]:
                    run_mask |= 1 << positions[entry_id]
            matching_hashes = scope.get("party_hashes")
            if not run_mask or not isinstance(matching_hashes, dict):
                continue
            members = scope.get("party_ids")
            if not isinstance(members, list):
                members = list(
                    db.scalars(select(Hit.party_id).where(Hit.run_id == run_id).distinct())
                )
            for party_id in members:
                if (
                    party_id not in pending
                    or matching_hashes.get(party_id) != party_hashes[party_id]
                ):
                    continue
                mask = pending[party_id] | run_mask
                if mask == complete:
                    coverage[party_id] = True
                    del pending[party_id]
                else:
                    pending[party_id] = mask
            if not pending:
                break
    finally:
        runs.close()
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
    screenings = latest_current_screenings(db)
    stale = 0
    coverage = current_screening_coverage(db)
    covered = sum(coverage.get(party.id, False) for party in parties)
    for party in parties:
        latest_current = screenings.get(party.id)
        if latest_current is None or latest_current < now - timedelta(hours=24):
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
    # Slider totals count immutable system decisions, consistently with each run
    # and the morning report. Repeated eligible observations are separate decisions.
    cleared_observations = auto_clear_observations(db)
    return {
        "open_items": len(queue),
        "likely_matches": sum(h.bucket == "likely" for h in queue),
        "auto_cleared_since_report": len(cleared_observations),
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
