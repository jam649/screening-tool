"""Source health and validated CSV/XLSX imports; no credentials are returned."""

import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from app.api.deps import DB, AdminUser, CurrentUser
from app.api.review import begin_write
from app.api.users import validate_admin
from app.audit import audit
from app.connectors.csv import UploadValidationError, preview_upload
from app.connectors.qbo_common import source_environment
from app.connectors.sync import sync_csv
from app.models import Source, utcnow
from app.notifications.service import dispatch_outbox
from app.scheduler.leases import (
    OperationAuthorityError,
    OperationBusyError,
    operation_session,
    protected_operation,
)
from app.scheduler.service import dispatch_attempt, enqueue_source_poll
from app.screening.engine import ScreeningTasksFailedError, process_pending_tasks

router = APIRouter(prefix="/sources", tags=["sources"])
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class SourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["csv", "qbo", "bc"] = "csv"
    label: str = Field(min_length=1, max_length=200)
    environment: Literal["sandbox", "production"] | None = None
    tenant_id: str | None = None
    bc_environment: str | None = None

    @field_validator("tenant_id")
    @classmethod
    def bc_tenant(cls, value):
        if value is None:
            return None
        if not re.fullmatch(r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}", value):
            raise ValueError("Enter a tenant UUID")
        return str(UUID(value))

    @field_validator("bc_environment")
    @classmethod
    def bc_environment_name(cls, value):
        if value is not None and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,28}", value):
            raise ValueError("Enter an environment name of fewer than 30 ASCII characters")
        return value

    @field_validator("label")
    @classmethod
    def meaningful_label(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Enter a source label")
        return value.strip()


class SyncBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["incremental", "full"] = "incremental"


def source_payload(source: Source, runtime=None) -> dict[str, Any]:
    next_sync = None
    if source.kind != "csv" and runtime is not None:
        job = runtime.scheduler.get_job("source-" + source.id)
        if job is not None:
            next_sync = job.next_run_time
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
        "environment": source_environment(source) if source.kind == "qbo" else None,
        "bc_environment": source.config.get("bc_environment") if source.kind == "bc" else None,
        "connection_status": source.config.get("connection_status")
        if source.kind in {"qbo", "bc"}
        else None,
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
def sources(request: Request, db: DB, user: CurrentUser):
    runtime = request.app.state.scheduler
    return [
        source_payload(row, runtime) for row in db.scalars(select(Source).order_by(Source.label))
    ]


@router.post("", status_code=201)
def create_source(body: SourceCreate, db: DB, user: AdminUser):
    actor_id = user.id
    begin_write(db)
    validate_admin(db, actor_id)
    if body.kind == "bc":
        if body.environment is not None or body.tenant_id is None or body.bc_environment is None:
            raise HTTPException(422, "Choose a tenant UUID and Business Central environment")
    elif body.tenant_id is not None or body.bc_environment is not None:
        raise HTTPException(422, "Tenant and Business Central environment belong to BC sources")
    elif (body.kind == "qbo" and body.environment is None) or (
        body.kind == "csv" and body.environment is not None
    ):
        raise HTTPException(422, "Choose an environment for QuickBooks sources only")
    values = {}
    if body.kind == "qbo":
        values = {
            "environment": body.environment,
            "connection_status": "configured",
            "connection_generation": 0,
            "webhooks_enabled": False,
        }
    elif body.kind == "bc":
        values = {
            "tenant_id": body.tenant_id,
            "bc_environment": body.bc_environment,
            "connection_status": "configured",
            "connection_generation": 0,
            "webhooks_enabled": False,
        }
    source = Source(kind=body.kind, label=body.label, config=values)
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
def source_detail(source_id: str, request: Request, db: DB, user: CurrentUser):
    return source_payload(get_source(db, source_id), request.app.state.scheduler)


@router.patch("/{source_id}")
def rename_source(source_id: str, body: SourceCreate, request: Request, db: DB, user: AdminUser):
    actor_id = user.id
    begin_write(db)
    validate_admin(db, actor_id)
    source = get_source(db, source_id)
    if source.kind != body.kind:
        raise HTTPException(422, "A source's connector kind cannot be changed")
    if body.environment is not None and (
        source.kind != "qbo" or source_environment(source) != body.environment
    ):
        raise HTTPException(422, "A source's environment cannot be changed")
    source.label = body.label
    db.commit()
    return source_payload(source, request.app.state.scheduler)


@router.post("/{source_id}/sync", status_code=202)
def sync_source(
    source_id: str,
    body: SyncBody,
    request: Request,
    background: BackgroundTasks,
    db: DB,
    user: AdminUser,
):
    actor_id = user.id
    begin_write(db)
    validate_admin(db, actor_id)
    source = get_source(db, source_id)
    if source.kind not in {"qbo", "bc"}:
        raise HTTPException(422, "Use a file upload to sync a CSV source")
    if source.config.get("connection_status") != "connected" or not source.credentials_enc:
        raise HTTPException(409, "Connect this source before requesting a sync")
    attempt = enqueue_source_poll(db, source.id, mode=body.mode, actor_user_id=actor_id)
    db.commit()
    result = {"attempt_id": attempt.id, "operation": "source_sync", "status": "queued"}
    background.add_task(
        dispatch_attempt, request.app.state.session_factory, request.app.state.config, attempt.id
    )
    runtime = request.app.state.scheduler
    if runtime:
        runtime.wake()
    return result


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
