"""Admin BC S2S controls and anonymous secret-verified subscription callbacks."""

from dataclasses import replace
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.api.deps import DB, AdminUser, AuthSession
from app.api.users import validate_admin
from app.audit import audit
from app.connectors.bc_auth import (
    BCOperationError,
    begin,
    connect_company,
    discover_companies,
)
from app.connectors.bc_common import (
    BCConfigurationError,
    canonical_uuid,
    connection_payload,
    load_app_config,
    load_source_credentials,
    public_app_settings,
    public_origin,
    save_app_config,
    save_source_credentials,
)
from app.connectors.bc_subscriptions import webhook_signals
from app.models import Source, utcnow
from app.scheduler.leases import (
    OperationAuthorityError,
    OperationBusyError,
    operation_is_busy,
    operation_session,
    protected_operation,
)
from app.scheduler.runtime import current_runtime
from app.scheduler.service import enqueue_bc_subscriptions, enqueue_source_disconnect

router = APIRouter()


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AppChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_id: SecretStr | None = Field(default=None, min_length=1, max_length=100)
    client_secret: SecretStr | None = Field(default=None, min_length=1, max_length=8192)
    clear_credentials: bool = False


class CompanyChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company_id: UUID


class ConnectionChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")
    webhooks_enabled: bool


def source_row(db, source_id):
    source = db.get(Source, source_id, populate_existing=True)
    if source is None or source.kind != "bc":
        raise HTTPException(404, "Business Central source not found")
    return source


def payload(db, config, source):
    return connection_payload(source, config, origin=public_origin(db))


def wake():
    runtime = current_runtime()
    if runtime:
        runtime.reconcile()
        runtime.wake()


def operation_error(error):
    code = getattr(error, "code", "")
    if isinstance(error, (OperationAuthorityError,)) or code == "admin_required":
        return HTTPException(403, "Active Admin role required")
    if isinstance(error, OperationBusyError) or code in {
        "connection_changed",
        "company_binding_mismatch",
        "company_already_connected",
        "disconnect_pending",
        "cleanup_pending",
    }:
        return HTTPException(409, "Finish pending operations or use the already bound company")
    if code == "source_not_found":
        return HTTPException(404, "Business Central source not found")
    return HTTPException(422, "Check the Business Central app, company and permissions")


@router.get("/settings/connectors/bc")
def get_settings(request: Request, user: AdminUser, db: DB):
    try:
        return public_app_settings(db, request.app.state.config)
    except BCConfigurationError:
        raise HTTPException(503, "Business Central configuration unavailable") from None


@router.patch("/settings/connectors/bc")
def patch_settings(body: AppChanges, request: Request, user: AdminUser, db: DB):
    begin(db)
    validate_admin(db, user.id)
    config = request.app.state.config
    old = load_app_config(db, config)
    sources = list(db.scalars(select(Source).where(Source.kind == "bc")))
    if any(operation_is_busy(db, "source_sync", source.id) for source in sources):
        raise HTTPException(409, "A Business Central operation is running; retry after it finishes")
    if body.clear_credentials and (body.client_id is not None or body.client_secret is not None):
        raise HTTPException(422, "Choose replacement credentials or clear")
    client_id, secret = old.client_id, old.client_secret
    if body.client_id is not None:
        try:
            client_id = canonical_uuid(body.client_id.get_secret_value())
        except BCConfigurationError:
            raise HTTPException(422, "Enter an Entra application UUID") from None
    if body.client_secret is not None:
        secret = body.client_secret.get_secret_value()
        if any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in secret):
            raise HTTPException(422, "Client secret must not contain control characters")
    if body.clear_credentials:
        client_id, secret = "", ""
    if bool(client_id) != bool(secret):
        raise HTTPException(422, "Provide an application ID and secret")
    if client_id != old.client_id and any(source.credentials_enc for source in sources):
        raise HTTPException(
            409, "Disconnect companies and finish cleanup before changing the app identity"
        )
    if client_id != old.client_id or secret != old.client_secret:
        before = public_app_settings(db, config)
        save_app_config(
            db,
            config,
            replace(old, client_id=client_id, client_secret=secret, generation=old.generation + 1),
        )
        for source in sources:
            if source.credentials_enc:
                values = load_source_credentials(source, config)
                if values.get("client_id") == client_id:
                    values["app_generation"] = old.generation + 1
                    save_source_credentials(db, source, config, values)
        audit(
            db,
            "bc.app_settings_changed",
            "settings",
            before=before,
            after=public_app_settings(db, config),
        )
    db.commit()
    return public_app_settings(db, config)


@router.get("/sources/{source_id}/bc/connection")
def connection(source_id: str, request: Request, user: AdminUser, db: DB):
    return payload(db, request.app.state.config, source_row(db, source_id))


def company_operation(operation, source_id, request, db, user, session, company_id=None):
    source_row(db, source_id)
    actor_id, session_id = user.id, session.id
    db.rollback()
    factory, config = request.app.state.session_factory, request.app.state.config
    try:
        with protected_operation(
            factory, operation, source_id=source_id, actor_user_id=actor_id
        ) as execution:
            with operation_session(factory, execution, actor_id) as operation_db:
                operation_db.info["initiating_session_id"] = session_id
                source = source_row(operation_db, source_id)
                options = {"transport": getattr(request.app.state, "bc_transport", None)}
                if operation == "bc_companies":
                    result = discover_companies(operation_db, config, source, **options)
                else:
                    attempt_id = connect_company(
                        operation_db, config, source, company_id, **options
                    )
                    source = source_row(operation_db, source_id)
                    result = {
                        "attempt_id": attempt_id,
                        "operation": "source_sync",
                        "status": "queued",
                        "connection": payload(operation_db, config, source),
                    }
            execution.complete({"source_id": source_id})
        if operation == "bc_connect":
            wake()
        return result
    except (ValueError, OperationAuthorityError, OperationBusyError) as error:
        raise operation_error(error) from None


@router.post("/sources/{source_id}/bc/companies")
def companies(
    source_id: str, body: EmptyBody, request: Request, user: AdminUser, session: AuthSession, db: DB
):
    return company_operation("bc_companies", source_id, request, db, user, session)


@router.post("/sources/{source_id}/bc/connect", status_code=202)
def connect(
    source_id: str,
    body: CompanyChoice,
    request: Request,
    user: AdminUser,
    session: AuthSession,
    db: DB,
):
    return company_operation(
        "bc_connect", source_id, request, db, user, session, str(body.company_id)
    )


def queue_result(db, request, source, attempt):
    result = {
        "attempt_id": attempt.id,
        "operation": attempt.operation,
        "status": "queued",
        "connection": payload(db, request.app.state.config, source),
    }
    db.commit()
    wake()
    return result


@router.patch("/sources/{source_id}/bc/connection", status_code=202)
def change_connection(
    source_id: str, body: ConnectionChanges, request: Request, user: AdminUser, db: DB
):
    from urllib.parse import urlsplit

    actor_id = user.id
    begin(db)
    validate_admin(db, actor_id)
    source = source_row(db, source_id)
    if body.webhooks_enabled:
        parsed = urlsplit(public_origin(db))
        if (
            source.config.get("connection_status") != "connected"
            or not parsed.hostname
            or parsed.scheme != "https"
        ):
            raise HTTPException(422, "Connect the company and configure a public HTTPS URL first")
    source.config = {
        **source.config,
        "webhooks_enabled": body.webhooks_enabled,
        "cleanup_status": source.config.get("cleanup_status", "not_required")
        if body.webhooks_enabled
        else "pending",
    }
    attempt = enqueue_bc_subscriptions(db, source_id, actor_user_id=actor_id)
    audit(
        db,
        "bc.webhooks_changed",
        "sources",
        source_id,
        after={"enabled": body.webhooks_enabled, "attempt_id": attempt.id},
    )
    return queue_result(db, request, source, attempt)


@router.post("/sources/{source_id}/bc/subscriptions/reconcile", status_code=202)
def reconcile(source_id: str, body: EmptyBody, request: Request, user: AdminUser, db: DB):
    actor_id = user.id
    begin(db)
    validate_admin(db, actor_id)
    source = source_row(db, source_id)
    attempt = enqueue_bc_subscriptions(db, source_id, actor_user_id=actor_id)
    return queue_result(db, request, source, attempt)


@router.post("/sources/{source_id}/bc/disconnect", status_code=202)
def disconnect(source_id: str, body: EmptyBody, request: Request, user: AdminUser, db: DB):
    actor_id = user.id
    begin(db)
    validate_admin(db, actor_id)
    source = source_row(db, source_id)
    source.config = {
        **source.config,
        "connection_status": "disconnecting",
        "webhooks_enabled": False,
        "disconnect_requested_at": utcnow().isoformat(),
        "cleanup_status": "pending",
        "cleanup_error_code": None,
    }
    attempt = enqueue_source_disconnect(db, source_id, actor_user_id=actor_id)
    audit(db, "bc.disconnect_requested", "sources", source_id, after={"attempt_id": attempt.id})
    return queue_result(db, request, source, attempt)


@router.post("/webhooks/bc/{source_id}")
async def webhook(source_id: str, request: Request, db: DB):
    params = request.scope.get("_bc_webhook_params", {})
    if not request.scope.get("_bc_webhook_query_valid", False) or set(params) - {"validationToken"}:
        raise HTTPException(400, "Invalid webhook request")
    if "validationToken" in params:
        token = params["validationToken"]
        if not isinstance(token, str) or not 1 <= len(token) <= 8192 or "\x00" in token:
            raise HTTPException(400, "Invalid webhook request")
        await run_in_threadpool(source_row, db, source_id)
        return PlainTextResponse(
            token,
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 1024 * 1024:
            raise HTTPException(413, "Webhook request is too large")
    try:
        await run_in_threadpool(
            webhook_signals, db, request.app.state.config, source_id, bytes(raw)
        )
    except (BCOperationError, BCConfigurationError):
        db.rollback()
        raise HTTPException(400, "Invalid webhook notification") from None
    except Exception:
        db.rollback()
        raise HTTPException(503, "Webhook could not be stored; retry") from None
    runtime = current_runtime()
    if runtime:
        runtime.wake()
    return {"status": "accepted"}
