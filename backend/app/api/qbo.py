"""Admin QBO configuration, session-bound OAuth and anonymous signed signals."""

from dataclasses import replace
from typing import Literal
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.api.deps import DB, AdminUser, AuthSession
from app.api.users import validate_admin
from app.audit import audit
from app.connectors.qbo_auth import (
    QBOAuthorizationError,
    accept_callback,
    begin,
    start_oauth,
    state_source,
    webhook_signals,
)
from app.connectors.qbo_common import (
    ENVIRONMENTS,
    QBOConfigurationError,
    connection_payload,
    load_app_config,
    load_source_credentials,
    public_app_settings,
    public_origin,
    save_app_config,
    save_source_credentials,
    source_environment,
)
from app.models import Source, utcnow
from app.scheduler.leases import (
    OperationAuthorityError,
    OperationBusyError,
    operation_is_busy,
    operation_session,
    protected_operation,
)
from app.scheduler.runtime import current_runtime

router = APIRouter()


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AppChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")
    environment: Literal["sandbox", "production"]
    client_id: SecretStr | None = Field(default=None, min_length=1, max_length=4096)
    client_secret: SecretStr | None = Field(default=None, min_length=1, max_length=8192)
    verifier_token: SecretStr | None = Field(default=None, min_length=1, max_length=4096)
    clear_credentials: bool = False
    clear_verifier_token: bool = False


class ConnectionChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")
    webhooks_enabled: bool


def qbo_source(db, source_id):
    source = db.get(Source, source_id, populate_existing=True)
    if source is None or source.kind != "qbo":
        raise HTTPException(404, "QuickBooks source not found")
    return source


def reconcile():
    runtime = current_runtime()
    if runtime:
        runtime.reconcile()
        runtime.wake()


@router.get("/settings/connectors/qbo")
def get_app_settings(request: Request, user: AdminUser, db: DB):
    return public_app_settings(db, request.app.state.config)


@router.patch("/settings/connectors/qbo")
def patch_app_settings(body: AppChanges, request: Request, user: AdminUser, db: DB):
    actor_id = user.id
    begin(db)
    validate_admin(db, actor_id)
    config = request.app.state.config
    old = load_app_config(db, config, body.environment)
    before = public_app_settings(db, config)
    relevant = [
        source
        for source in db.scalars(select(Source).where(Source.kind == "qbo"))
        if source_environment(source) == body.environment
    ]
    if any(operation_is_busy(db, "source_sync", source.id) for source in relevant):
        raise HTTPException(409, "A QuickBooks operation is running; retry after it finishes")
    if (body.clear_credentials and (body.client_id or body.client_secret)) or (
        body.clear_verifier_token and body.verifier_token
    ):
        raise HTTPException(422, "Choose replacement credentials or clear, not both")
    values = {name: getattr(old, name) for name in ("client_id", "client_secret", "verifier_token")}
    for name in values:
        value = getattr(body, name)
        if value is not None:
            values[name] = value.get_secret_value()
    if body.clear_credentials:
        values["client_id"], values["client_secret"] = "", ""
    if body.clear_verifier_token:
        values["verifier_token"] = None
    if bool(values["client_id"]) != bool(values["client_secret"]):
        raise HTTPException(422, "Provide both QuickBooks client ID and secret")
    if values["client_id"] != old.client_id and any(source.credentials_enc for source in relevant):
        raise HTTPException(409, "Disconnect connected companies before replacing the app identity")
    if not values["verifier_token"] and any(
        source.config.get("webhooks_enabled") for source in relevant
    ):
        raise HTTPException(409, "Disable source webhooks before clearing the verifier token")
    if any(values[name] != getattr(old, name) for name in values):
        save_app_config(db, config, replace(old, **values, generation=old.generation + 1))
        for source in relevant:
            if source.credentials_enc:
                credentials = load_source_credentials(source, config)
                if credentials.get("client_id") == values["client_id"]:
                    credentials["app_generation"] = old.generation + 1
                    save_source_credentials(db, source, config, credentials)
        audit(
            db,
            "qbo.app_settings_changed",
            "settings",
            before=before,
            after=public_app_settings(db, config),
        )
    db.commit()
    return public_app_settings(db, config)


@router.get("/sources/{source_id}/qbo/connection")
def connection(source_id: str, request: Request, user: AdminUser, db: DB):
    return connection_payload(qbo_source(db, source_id), request.app.state.config)


@router.patch("/sources/{source_id}/qbo/connection")
def change_connection(
    source_id: str, body: ConnectionChanges, request: Request, user: AdminUser, db: DB
):
    actor_id = user.id
    begin(db)
    validate_admin(db, actor_id)
    source = qbo_source(db, source_id)
    app = load_app_config(db, request.app.state.config, source_environment(source))
    if body.webhooks_enabled:
        origin = urlsplit(public_origin(db))
        if (
            not app.verifier_token
            or origin.scheme != "https"
            or not origin.hostname
            or source.config.get("connection_status") != "connected"
        ):
            raise HTTPException(
                422, "Connect the company and configure a verifier token and public HTTPS URL first"
            )
    source.config = {**source.config, "webhooks_enabled": body.webhooks_enabled}
    audit(
        db, "qbo.webhooks_changed", "sources", source.id, after={"enabled": body.webhooks_enabled}
    )
    db.commit()
    return connection_payload(source, request.app.state.config)


@router.post("/sources/{source_id}/qbo/oauth/start")
def oauth_start(
    source_id: str, body: EmptyBody, request: Request, user: AdminUser, session: AuthSession, db: DB
):
    actor_id, session_id = user.id, session.id
    qbo_source(db, source_id)
    db.rollback()
    try:
        with protected_operation(
            request.app.state.session_factory,
            "qbo_oauth_start",
            source_id=source_id,
            actor_user_id=actor_id,
        ) as execution:
            with operation_session(
                request.app.state.session_factory, execution, actor_id
            ) as operation_db:
                result = start_oauth(
                    operation_db, request.app.state.config, source_id, actor_id, session_id
                )
            execution.complete({"source_id": source_id})
        return result
    except OperationAuthorityError:
        raise HTTPException(403, "Active Admin role required") from None
    except OperationBusyError:
        raise HTTPException(409, "This QuickBooks source has an operation running") from None
    except QBOAuthorizationError as exc:
        if exc.code in {"revocation_cleanup_pending", "disconnect_pending"}:
            raise HTTPException(
                409, "Finish pending grant revocation before reconnecting"
            ) from None
        raise HTTPException(
            422, "Configure the QuickBooks app and public application URL before connecting"
        ) from None
    except QBOConfigurationError:
        raise HTTPException(
            422, "Configure the QuickBooks app and public application URL before connecting"
        ) from None


@router.get("/connectors/qbo/oauth/callback")
def oauth_callback(request: Request, user: AdminUser, session: AuthSession, db: DB):
    params = request.scope.get("_qbo_callback_params")
    if params is None:
        params = dict(request.query_params)
    request.scope["query_string"] = b""
    source_id = None
    outcome = {"connection_error": "oauth_failed"}
    actor_id, session_id = user.id, session.id
    try:
        source_id = state_source(db, params.get("state"), actor_id, session_id)
        db.rollback()
        with protected_operation(
            request.app.state.session_factory,
            "qbo_oauth_callback",
            source_id=source_id,
            actor_user_id=actor_id,
        ) as execution:
            with operation_session(
                request.app.state.session_factory, execution, actor_id
            ) as operation_db:
                with protected_operation(
                    request.app.state.session_factory,
                    "qbo_oauth_exchange_" + source_environment(qbo_source(operation_db, source_id)),
                    source_id=source_id,
                    actor_user_id=actor_id,
                ):
                    try:
                        accept_callback(
                            operation_db,
                            request.app.state.config,
                            state=params.get("state"),
                            code=params.get("code"),
                            realm_id=params.get("realmId"),
                            error=params.get("error"),
                            user_id=actor_id,
                            session_id=session_id,
                            transport=getattr(request.app.state, "qbo_transport", None),
                        )
                    except Exception:
                        operation_db.rollback()
                        raise
            execution.complete({"source_id": source_id})
        outcome = {"connection": "connected"}
        reconcile()
    except QBOAuthorizationError as exc:
        outcome = {"connection_error": exc.code}
    except OperationAuthorityError:
        outcome = {"connection_error": "admin_session_required"}
    except OperationBusyError:
        outcome = {"connection_error": "source_busy"}
    except Exception:
        # Never reflect a provider body, authorization code, state or token.
        pass
    if source_id:
        outcome["source_id"] = source_id
    return RedirectResponse(
        "/sources?" + urlencode(outcome),
        status_code=303,
        headers={"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"},
    )


@router.post("/sources/{source_id}/qbo/disconnect", status_code=202)
def disconnect(source_id: str, body: EmptyBody, request: Request, user: AdminUser, db: DB):
    from app.scheduler.service import enqueue_source_disconnect

    actor_id = user.id
    begin(db)
    validate_admin(db, actor_id)
    source = qbo_source(db, source_id)
    source.config = {
        **source.config,
        "connection_status": "disconnecting",
        "webhooks_enabled": False,
        "disconnect_requested_at": utcnow().isoformat(),
        "revocation_status": "pending",
        "revocation_error_code": None,
    }
    attempt = enqueue_source_disconnect(db, source.id, actor_user_id=actor_id)
    audit(db, "qbo.disconnect_requested", "sources", source.id, after={"attempt_id": attempt.id})
    db.commit()
    result = {
        "attempt_id": attempt.id,
        "operation": "source_disconnect",
        "status": "queued",
        "connection": connection_payload(source, request.app.state.config),
    }
    reconcile()
    return result


@router.post("/webhooks/qbo/{environment}")
async def webhook(environment: str, request: Request, db: DB):
    if environment not in ENVIRONMENTS:
        raise HTTPException(404, "Webhook endpoint not found")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 1024 * 1024:
            raise HTTPException(413, "Webhook request is too large")
    try:
        await run_in_threadpool(
            webhook_signals,
            db,
            request.app.state.config,
            environment,
            bytes(raw),
            request.headers.get("intuit-signature"),
        )
    except QBOAuthorizationError as exc:
        status = 401 if exc.code == "invalid_webhook_signature" else 400
        raise HTTPException(status, "Notification signature or payload is invalid") from None
    except QBOConfigurationError:
        raise HTTPException(401, "Notification signature or payload is invalid") from None
    runtime = current_runtime()
    if runtime:
        runtime.wake()
    return {"status": "accepted"}
