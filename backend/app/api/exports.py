"""Authenticated downloads; large archives stream from private owned storage."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response
from starlette.types import Receive, Scope, Send

from app.api.deps import DB, CurrentUser
from app.reports.export import (
    ExportArtifact,
    ExportNotFound,
    export_full_zip,
    export_parties_csv,
    export_party,
    export_run,
)

router = APIRouter()


class ExportFileResponse(FileResponse):
    """Own the private archive throughout streaming, including failed ASGI sends."""

    def __init__(self, artifact: ExportArtifact, headers: dict[str, str]):
        assert artifact.path is not None
        self.artifact = artifact
        super().__init__(
            artifact.path,
            media_type=artifact.media_type,
            filename=artifact.filename,
            headers=headers,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # BackgroundTask only runs after successful FileResponse streaming.
            self.artifact.close()


def download(artifact: ExportArtifact) -> Response:
    headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "X-Export-SHA256": artifact.sha256,
    }
    if artifact.path is not None:
        return ExportFileResponse(artifact, headers)
    headers["Content-Disposition"] = f'attachment; filename="{artifact.filename}"'
    return Response(artifact.body, media_type=artifact.media_type, headers=headers)


@router.get("/parties/{party_id}/export")
def party_export(party_id: str, user: CurrentUser, db: DB, format: Literal["pdf", "csv"] = "pdf"):
    try:
        artifact = export_party(db, party_id, format, actor_user_id=user.id)
    except ExportNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    return download(artifact)


@router.get("/runs/{run_id}/export")
def run_export(run_id: str, user: CurrentUser, db: DB, format: Literal["pdf", "csv"] = "pdf"):
    try:
        artifact = export_run(db, run_id, format, actor_user_id=user.id)
    except ExportNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    return download(artifact)


@router.get("/exports/parties.csv")
def parties_export(
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
):
    return download(
        export_parties_csv(
            db,
            actor_user_id=user.id,
            filters={
                "q": q,
                "role": role,
                "source_id": source_id,
                "country": country,
                "status": status,
                "stale": stale,
                "auto_cleared": auto_cleared,
                "include_deleted": include_deleted,
            },
        )
    )


@router.get("/exports/full.zip")
def full_export(user: CurrentUser, db: DB):
    return download(export_full_zip(db, actor_user_id=user.id))
