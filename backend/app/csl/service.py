"""Atomic CSL refreshes with immutable source evidence and durable delta work."""

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.audit import audit
from app.config import Config
from app.csl.fetch import CSLDownloadError, download_bulk, download_stamps
from app.csl.parser import BULK_URL, MAX_DOWNLOAD_BYTES, CSLValidationError, ParsedCSL, parse_bulk
from app.models import (
    CSLEntry,
    CSLEntryRevision,
    CSLFetch,
    CSLVersion,
    Disposition,
    Hit,
    ScreeningTask,
    Setting,
    utcnow,
)
from app.security import decrypt_json


class CSLBusyError(RuntimeError):
    pass


@dataclass(frozen=True)
class RefreshResult:
    version: CSLVersion
    fetch: CSLFetch
    changed: bool
    pending_task_id: str | None
    closed_hits: int


def configured_api_key(db: Session, config: Config) -> str | None:
    setting = db.scalar(select(Setting).where(Setting.key == "csl_api_key"))
    if setting is None:
        return None
    if not setting.encrypted or not isinstance(setting.value, str):
        raise CSLValidationError("The optional CSL API key must be stored encrypted")
    value = decrypt_json(setting.value, config).get("api_key")
    if not isinstance(value, str) or not value.strip():
        raise CSLValidationError("The optional CSL API key setting is invalid")
    return value


def current_version(db: Session) -> CSLVersion | None:
    return db.scalar(
        select(CSLVersion).order_by(CSLVersion.fetched_at.desc(), CSLVersion.id.desc())
    )


def version_payload(version: CSLVersion | None) -> dict[str, Any] | None:
    if version is None:
        return None
    return {
        "id": version.id,
        "fetched_at": version.fetched_at.isoformat(),
        "file_sha256": version.file_sha256,
        "semantic_sha256": version.semantic_sha256,
        "entry_count": version.entry_count,
        "added": version.added,
        "changed": version.changed,
        "removed": version.removed,
        "source_list_stamps": version.source_list_stamps,
    }


def fetch_payload(fetch: CSLFetch | None) -> dict[str, Any] | None:
    if fetch is None:
        return None
    return {
        "id": fetch.id,
        "fetched_at": fetch.fetched_at.isoformat(),
        "completed_at": fetch.completed_at.isoformat() if fetch.completed_at else None,
        "status": fetch.status,
        "version_id": fetch.version_id,
        "file_sha256": fetch.file_sha256,
        "error": fetch.error,
        "source_list_stamps": fetch.source_list_stamps,
        "warnings": fetch.warnings,
    }


def status_payload(db: Session) -> dict[str, Any]:
    last_fetch = db.scalar(
        select(CSLFetch).order_by(CSLFetch.fetched_at.desc(), CSLFetch.id.desc())
    )
    return {
        "current_version": version_payload(current_version(db)),
        "last_fetch": fetch_payload(last_fetch),
        "pending_screenings": db.scalar(
            select(func.count()).select_from(ScreeningTask).where(ScreeningTask.status == "pending")
        )
        or 0,
    }


def _start_fetch(db: Session, source: str) -> CSLFetch:
    try:
        db.execute(text("BEGIN IMMEDIATE"))
        for active in db.scalars(select(CSLFetch).where(CSLFetch.status == "fetching")):
            if active.fetched_at > utcnow() - timedelta(minutes=10):
                db.rollback()
                raise CSLBusyError("A CSL refresh is already running")
            active.status = "failed"
            active.completed_at = utcnow()
            active.error = "Previous refresh stopped before completion"
            audit(
                db, "csl.fetch_interrupted", "csl_fetches", active.id, after={"error": active.error}
            )
        fetch = CSLFetch(source=source)
        db.add(fetch)
        db.flush()
        audit(db, "csl.fetch_started", "csl_fetches", fetch.id, after={"source": source})
        db.commit()
        return fetch
    except OperationalError as exc:
        db.rollback()
        raise CSLBusyError("The database is busy; retry the CSL refresh") from exc


def _record_failure(db: Session, fetch_id: str, error: str, raw_sha: str | None = None) -> None:
    db.rollback()
    fetch = db.get(CSLFetch, fetch_id)
    if fetch is None:
        return
    fetch.status = "failed"
    fetch.completed_at = utcnow()
    fetch.error = error
    if raw_sha:
        fetch.file_sha256 = raw_sha
    audit(db, "csl.fetch_failed", "csl_fetches", fetch.id, after={"error": error})
    db.commit()


def _apply(db: Session, parsed: ParsedCSL, fetch_id: str, warnings: list[str]) -> RefreshResult:
    db.execute(text("BEGIN IMMEDIATE"))
    fetch = db.get(CSLFetch, fetch_id, populate_existing=True)
    if fetch is None or fetch.status != "fetching":
        raise CSLBusyError("This CSL refresh was interrupted; retry")
    fetch.file_sha256 = parsed.file_sha256
    fetch.semantic_sha256 = parsed.semantic_sha256
    fetch.source_list_stamps = parsed.source_list_stamps
    fetch.warnings = warnings
    previous = current_version(db)
    if previous and previous.semantic_sha256 == parsed.semantic_sha256:
        fetch.version_id = previous.id
        fetch.status = "success"
        fetch.completed_at = utcnow()
        audit(
            db,
            "csl.fetch_unchanged",
            "csl_fetches",
            fetch.id,
            after={
                "version_id": previous.id,
                "file_sha256": parsed.file_sha256,
                "semantic_sha256": parsed.semantic_sha256,
            },
        )
        db.commit()
        return RefreshResult(previous, fetch, False, None, 0)

    existing = {(entry.source_list, entry.csl_id): entry for entry in db.scalars(select(CSLEntry))}
    active = {key: entry for key, entry in existing.items() if entry.removed_version is None}
    incoming = {entry.key for entry in parsed.entries}
    added = [entry for entry in parsed.entries if entry.key not in active]
    changed = [
        entry
        for entry in parsed.entries
        if entry.key in active and active[entry.key].entry_hash != entry.entry_hash
    ]
    removed = [entry for key, entry in active.items() if key not in incoming]
    if len(active) >= 20 and len(removed) > max(5, len(active) // 4):
        # Complete official snapshots can legitimately shrink. Cardinality is a
        # health signal, never authority to retain listings removed by the source.
        warning = (
            f"The complete CSL snapshot removed {len(removed)} of {len(active)} active listings"
        )
        fetch.warnings = [*warnings, warning]
        audit(
            db,
            "csl.large_removal",
            "csl_fetches",
            fetch.id,
            after={
                "active_before": len(active),
                "removed": len(removed),
                "remaining": len(parsed.entries),
                "warning": warning,
            },
        )
    version = CSLVersion(
        fetched_at=fetch.fetched_at,
        file_sha256=parsed.file_sha256,
        semantic_sha256=parsed.semantic_sha256,
        entry_count=len(parsed.entries),
        added=len(added),
        changed=len(changed),
        removed=len(removed),
        source_list_stamps=parsed.source_list_stamps,
    )
    db.add(version)
    db.flush()
    revisions = []
    delta_ids = []
    delta_listing_ids = {entry.key for entry in added + changed}
    for index, incoming_entry in enumerate(parsed.entries):
        row = existing.get(incoming_entry.key)
        before_hash = row.entry_hash if row else None
        was_removed = row is not None and row.removed_version is not None
        if row is None:
            row = CSLEntry(
                **incoming_entry.fields(),
                first_seen_version=version.id,
                last_seen_version=version.id,
            )
            db.add(row)
        else:
            for key, value in incoming_entry.fields().items():
                setattr(row, key, value)
            row.last_seen_version = version.id
            row.removed_version = None
        # Flush IDs without an O(n) flush of the entire growing batch per listing.
        if row.id is None:
            from uuid import uuid4

            row.id = str(uuid4())
        revisions.append(
            CSLEntryRevision(
                version_id=version.id,
                entry_id=row.id,
                snapshot={"id": row.id, **incoming_entry.fields()},
            )
        )
        if (row.source_list, row.csl_id) in delta_listing_ids:
            delta_ids.append(row.id)
            action = "reappeared" if was_removed else "added" if before_hash is None else "changed"
            audit(
                db,
                f"csl.entry_{action}",
                "csl_entries",
                row.id,
                before={"entry_hash": before_hash} if before_hash else None,
                after={
                    "csl_id": row.csl_id,
                    "entry_hash": row.entry_hash,
                    "version_id": version.id,
                },
            )
        if index % 500 == 499:
            db.flush()
    for row in removed:
        row.removed_version = version.id
        audit(
            db,
            "csl.entry_removed",
            "csl_entries",
            row.id,
            before={"entry_hash": row.entry_hash},
            after={"removed_version": version.id},
        )
    # Flush listing insertions before their immutable revisions (no ORM relationship).
    db.flush()
    db.add_all(revisions)
    db.flush()
    closed_hits = 0
    if removed:
        actor = db.info.pop("actor_user_id", None)
        try:
            removed_ids = [entry.id for entry in removed]
            # Chunk ID predicates below SQLite's historical bind-parameter limit.
            for offset in range(0, len(removed_ids), 500):
                removed_hits = list(
                    db.scalars(
                        select(Hit).where(
                            Hit.csl_entry_id.in_(removed_ids[offset : offset + 500]),
                            Hit.status == "open",
                        )
                    )
                )
                db.add_all(
                    Disposition(
                        hit_id=hit.id,
                        kind="auto_cleared",
                        actor_user_id=None,
                        note=f"entry removed from list in version {version.id}",
                    )
                    for hit in removed_hits
                )
                # Persist each system decision before its permitted status transition.
                db.flush()
                for hit in removed_hits:
                    hit.status = "dispositioned"
                    audit(
                        db,
                        "csl.hit_closed",
                        "hits",
                        hit.id,
                        before={"status": "open"},
                        after={"status": "dispositioned", "version_id": version.id},
                    )
                    closed_hits += 1
            db.flush()
        finally:
            if actor is not None:
                db.info["actor_user_id"] = actor
    task = None
    if delta_ids:
        task = ScreeningTask(trigger="list_change", csl_version_id=version.id, entry_ids=delta_ids)
        db.add(task)
        db.flush()
        audit(
            db,
            "screening.enqueued",
            "screening_tasks",
            task.id,
            after={
                "trigger": "list_change",
                "version_id": version.id,
                "entry_count": len(delta_ids),
            },
        )
    fetch.version_id = version.id
    fetch.status = "success"
    fetch.completed_at = utcnow()
    audit(
        db,
        "csl.version_created",
        "csl_versions",
        version.id,
        after={**(version_payload(version) or {}), "closed_hits": closed_hits},
    )
    db.commit()
    return RefreshResult(version, fetch, True, task.id if task else None, closed_hits)


def refresh_csl(
    db: Session,
    *,
    file: Path | None = None,
    data: bytes | None = None,
    api_key: str | None = None,
) -> RefreshResult:
    """Refresh from the official endpoint or an explicitly supplied operator fixture.

    Validation precedes every listing mutation. Fetch start/failure rows commit
    independently, while version/diff/closures/tasks commit in one transaction.
    """
    import hashlib

    source = "operator file" if file is not None or data is not None else BULK_URL
    fetch = _start_fetch(db, source)
    raw_sha = None
    try:
        if file is not None:
            with file.open("rb") as handle:
                payload = handle.read(MAX_DOWNLOAD_BYTES + 1)
        else:
            payload = data if data is not None else download_bulk()
        raw_sha = hashlib.sha256(payload).hexdigest()
        parsed = parse_bulk(payload, require_envelope=source == BULK_URL)
        warnings = []
        if api_key:
            try:
                extra_stamps = download_stamps(api_key)
                # Keep independently observed API metadata without discarding bulk stamps.
                parsed.source_list_stamps["sources_api"] = extra_stamps
            except CSLDownloadError as exc:
                warnings.append(str(exc))
        return _apply(db, parsed, fetch.id, warnings)
    except (CSLValidationError, CSLDownloadError, CSLBusyError) as exc:
        _record_failure(db, fetch.id, str(exc), raw_sha)
        raise
    except OSError as exc:
        _record_failure(db, fetch.id, "Unable to read the operator CSL file", raw_sha)
        raise CSLDownloadError("Unable to read the operator CSL file") from exc
    except Exception:
        _record_failure(
            db, fetch.id, "CSL refresh failed; the previous list remains active", raw_sha
        )
        raise
