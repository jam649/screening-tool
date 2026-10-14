"""Validated atomic party ingestion shared by CSV and future ERP adapters."""

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.audit import audit
from app.connectors.base import RejectedRow, SyncBatch
from app.connectors.csv import CSVConnector, UploadValidationError
from app.csl.service import current_version
from app.models import Party, ScreeningTask, Source, utcnow


@dataclass(frozen=True)
class SyncResult:
    source_id: str
    added: int = 0
    changed: int = 0
    unchanged: int = 0
    deleted: int = 0
    rejected: list[RejectedRow] = field(default_factory=list)
    rejected_count: int = 0
    task_ids: list[str] = field(default_factory=list)
    applied: bool = True

    def payload(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "added": self.added,
            "changed": self.changed,
            "unchanged": self.unchanged,
            "deleted": self.deleted,
            "rejected": [row.payload() for row in self.rejected],
            "rejected_count": self.rejected_count,
            "omitted_rejection_details": max(0, self.rejected_count - len(self.rejected)),
            "task_ids": self.task_ids,
            "applied": self.applied,
        }


def apply_sync(
    db: Session,
    source: Source,
    batch: SyncBatch,
    *,
    mapping: dict[str, str | list[str]] | None = None,
) -> SyncResult:
    source_id = source.id
    if source_id is None:
        raise UploadValidationError("Save a source before applying a sync")
    # Any rejection aborts the full snapshot. It must never make a previously known
    # rejected identity appear absent and silently soft-delete it.
    if batch.rejected or batch.rejected_count:
        source.last_error = (
            f"Import rejected: {batch.rejected_count or len(batch.rejected)} invalid rows"
        )
        audit(
            db,
            "sources.sync_rejected",
            "sources",
            source_id,
            after={
                "rejected_count": batch.rejected_count or len(batch.rejected),
                "rows": [row.payload() for row in batch.rejected],
            },
        )
        db.commit()
        return SyncResult(
            source_id,
            rejected=batch.rejected,
            rejected_count=batch.rejected_count or len(batch.rejected),
            applied=False,
        )
    ids = [party.external_id for party in batch.parties]
    if len(ids) != len(set(ids)):
        raise UploadValidationError("Duplicate party identifiers are not accepted")
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        existing = {
            party.external_id: party
            for party in db.scalars(select(Party).where(Party.source_id == source_id))
        }
        added = changed = unchanged = deleted = 0
        screen_ids = []
        now = utcnow()
        for index, incoming in enumerate(batch.parties):
            row = existing.get(incoming.external_id)
            if incoming.deleted:
                if row and row.deleted_at is None:
                    row.deleted_at = now
                    deleted += 1
                continue
            if row is None:
                row = Party(source_id=source_id, **incoming.fields())
                db.add(row)
                from uuid import uuid4

                row.id = str(uuid4())
                added += 1
                screen_ids.append(row.id)
            elif row.record_hash == incoming.record_hash and row.deleted_at is None:
                # Phone-only/raw metadata differences do not rewrite screening evidence.
                unchanged += 1
                continue
            else:
                for key, value in incoming.fields().items():
                    setattr(row, key, value)
                row.deleted_at = None
                row.last_seen_at = now
                changed += 1
                screen_ids.append(row.id)
            if index % 500 == 499:
                db.flush()
        if batch.complete:
            incoming_ids = set(ids)
            for external_id, row in existing.items():
                if external_id not in incoming_ids and row.deleted_at is None:
                    row.deleted_at = now
                    deleted += 1
        db.flush()
        tasks = []
        version = current_version(db)
        if screen_ids and version:
            # One durable scoped task can contain a sync batch; only changed/new parties run.
            task = ScreeningTask(
                trigger="party_change", csl_version_id=version.id, party_ids=screen_ids
            )
            db.add(task)
            db.flush()
            tasks.append(task.id)
            audit(
                db,
                "screening.enqueued",
                "screening_tasks",
                task.id,
                after={"trigger": "party_change", "party_ids": screen_ids, "source_id": source_id},
            )
        source.last_synced_at = now
        source.cursor = batch.cursor
        source.record_count = (
            db.scalar(
                select(func.count())
                .select_from(Party)
                .where(Party.source_id == source_id, Party.deleted_at.is_(None))
            )
            or 0
        )
        source.last_error = None
        if mapping is not None:
            source.config = {**source.config, "column_mapping": mapping}
        audit(
            db,
            "sources.sync_completed",
            "sources",
            source_id,
            after={
                "added": added,
                "changed": changed,
                "unchanged": unchanged,
                "deleted": deleted,
                "record_count": source.record_count,
                "screening_pending_without_list": bool(screen_ids and version is None),
            },
        )
        db.commit()
        return SyncResult(source_id, added, changed, unchanged, deleted, task_ids=tasks)
    except Exception:
        db.rollback()
        raise


def sync_csv(
    db: Session,
    source: Source,
    data: bytes,
    filename: str,
    mapping: dict[str, str | list[str]] | None = None,
) -> SyncResult:
    if source.kind != "csv":
        raise UploadValidationError("Select a CSV source for file uploads")
    selected = mapping or source.config.get("column_mapping")
    if not isinstance(selected, dict):
        raise UploadValidationError("Map the upload columns before importing")
    connector = CSVConnector(data, filename, selected)
    return apply_sync(db, source, connector.full_sync(), mapping=selected)
