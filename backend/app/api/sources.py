"""Source health and validated CSV/XLSX imports; no credentials are returned."""

import json
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from app.api.deps import DB, AdminUser, CurrentUser
from app.audit import audit
from app.connectors.csv import UploadValidationError, preview_upload
from app.connectors.sync import sync_csv
from app.models import Source, utcnow
from app.notifications.service import dispatch_outbox
from app.scheduler.leases import (
    OperationAuthorityError,
    OperationBusyError,
    operation_session,
    protected_operation,
)
from app.screening.engine import ScreeningTasksFailedError, process_pending_tasks

router = APIRouter(prefix="/sources", tags=["sources"])
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class SourceCreate(BaseModel):
    kind: Literal["csv"] = "csv"
    label: str = Field(min_length=1, max_length=200)

    @field_validator("label")
    @classmethod
    def meaningful_label(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Enter a source label")
        return value.strip()


def source_payload(source: Source) -> dict[str, Any]:
    next_sync = None
    if source.kind != "csv" and source.last_synced_at:
        next_sync = source.last_synced_at + timedelta(minutes=15)
    health = "not_synced"
    if source.last_error:
        health = "error"
    elif source.last_synced_at:
        health = "stale" if utcnow() - source.last_synced_at > timedelta(hours=24) else "healthy"
    return {
        "id": source.id,
        "kind": source.kind,
        "label": source.label,
        "record_count": source.record_count,
        "last_synced_at": source.last_synced_at,
        "next_sync_at": next_sync,
        "health": health,
        "last_error": source.last_error,
        "column_mapping": source.config.get("column_mapping", {}),
    }


def get_source(db, source_id: str) -> Source:
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(404, "Source not found")
    return source


def upload_bytes(file: UploadFile) -> tuple[bytes, str]:
    try:
        data = file.file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Uploads must be 10 MiB or smaller")
        if not data:
            raise HTTPException(422, "Choose a non-empty CSV or XLSX file")
        return data, file.filename or ""
    finally:
        file.file.close()


@router.get("")
def sources(db: DB, user: CurrentUser):
    return [source_payload(row) for row in db.scalars(select(Source).order_by(Source.label))]


@router.post("", status_code=201)
def create_source(body: SourceCreate, db: DB, user: AdminUser):
    source = Source(kind=body.kind, label=body.label, config={})
    db.add(source)
    db.commit()
    return source_payload(source)


@router.get("/demo.csv")
def demo_csv(db: DB, user: AdminUser):
    sample = Path(__file__).resolve().parents[3] / "fixtures" / "parties.csv"
    if not sample.is_file():
        sample = Path.cwd().parent / "fixtures" / "parties.csv"
    if not sample.is_file():
        raise HTTPException(503, "The synthetic sample file is not installed")
    audit(db, "sources.demo_download", "sources", after={"synthetic": True})
    db.commit()
    return FileResponse(sample, media_type="text/csv", filename="screening-tool-demo-parties.csv")


@router.post("/preview")
def preview(
    db: DB,
    user: AdminUser,
    file: Annotated[UploadFile, File()],
    source_id: Annotated[str | None, Form()] = None,
):
    source = get_source(db, source_id) if source_id else None
    data, filename = upload_bytes(file)
    try:
        result = preview_upload(data, filename)
    except UploadValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        **result,
        "source_id": source_id,
        "column_mapping": source.config.get("column_mapping", {}) if source else {},
    }


@router.get("/{source_id}")
def source_detail(source_id: str, db: DB, user: CurrentUser):
    return source_payload(get_source(db, source_id))


@router.patch("/{source_id}")
def rename_source(source_id: str, body: SourceCreate, db: DB, user: AdminUser):
    source = get_source(db, source_id)
    if source.kind != body.kind:
        raise HTTPException(422, "A source's connector kind cannot be changed")
    source.label = body.label
    db.commit()
    return source_payload(source)


@router.post("/{source_id}/upload")
def upload(
    source_id: str,
    request: Request,
    db: DB,
    user: AdminUser,
    file: Annotated[UploadFile, File()],
    mapping: Annotated[str | None, Form()] = None,
):
    source = get_source(db, source_id)
    data, filename = upload_bytes(file)
    selected = None
    if mapping is not None:
        if len(mapping) > 16_384:
            raise HTTPException(422, "The column mapping is too large")
        try:
            selected = json.loads(mapping)
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, "The column mapping must be a JSON object") from exc
        if not isinstance(selected, dict):
            raise HTTPException(422, "The column mapping must be a JSON object")
    actor_id = user.id
    db.rollback()
    factory = request.app.state.session_factory
    try:
        with protected_operation(
            factory, "source_import", source_id=source_id, actor_user_id=actor_id
        ) as execution:
            with operation_session(factory, execution, actor_id) as operation_db:
                source = get_source(operation_db, source_id)
                result = sync_csv(operation_db, source, data, filename, selected)
                imported_source = source_payload(source)
            execution.complete(
                {
                    "source_id": source_id,
                    "applied": result.applied,
                    "added": result.added,
                    "changed": result.changed,
                    "unchanged": result.unchanged,
                    "deleted": result.deleted,
                    "task_ids": result.task_ids,
                }
            )
    except UploadValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    except OperationBusyError:
        raise HTTPException(409, "This source is syncing or the database is busy") from None
    except OperationAuthorityError:
        raise HTTPException(403, "Active Admin role required") from None
    screening_errors = []
    runs = []
    if result.applied:
        try:
            with protected_operation(
                factory, "screen_changed", actor_user_id=actor_id
            ) as execution:
                with operation_session(factory, execution, actor_id) as operation_db:
                    try:
                        runs = process_pending_tasks(operation_db)
                    except ScreeningTasksFailedError as exc:
                        execution.complete(
                            {
                                "run_ids": [run.id for run in exc.completed_runs],
                                "failed_task_ids": [row["task_id"] for row in exc.failures],
                            }
                        )
                        raise
                execution.complete({"run_ids": [run.id for run in runs]})
        except ScreeningTasksFailedError as exc:
            runs, screening_errors = exc.completed_runs, exc.failures
        except (OperationBusyError, OperationAuthorityError):
            screening_errors = [{"error": "Screening is deferred; monitoring will retry."}]
        # Committed evidence always survives a notification failure.
        try:
            dispatch_outbox(factory, request.app.state.config)
        except Exception:
            pass  # Outbox/operation health reports delivery failures independently.
    return {
        **result.payload(),
        "source": imported_source,
        "screening_run_ids": [run.id for run in runs],
        "screening_errors": screening_errors,
        "screening_pending": bool(screening_errors),
    }
