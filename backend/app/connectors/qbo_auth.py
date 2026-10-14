"""Session-bound OAuth and signed webhook signals; never trusts notification data."""

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlsplit

from sqlalchemy import func, literal_column, select, text

from app.audit import audit
from app.connectors.qbo_common import (
    ACCOUNTING_SCOPE,
    AUTHORIZATION_URL,
    CALLBACK_PATH,
    AppConfig,
    load_app_config,
    load_source_credentials,
    public_origin,
    save_source_credentials,
    source_environment,
    source_generation,
)
from app.models import AuditLog, Source, User, UserSession, utcnow
from app.models.oauth import OAuthState
from app.security import digest


class QBOAuthorizationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def active_session(db, user_id, session_id):
    user = db.get(User, user_id, populate_existing=True)
    session = db.get(UserSession, session_id, populate_existing=True)
    if (
        user is None
        or not user.active
        or user.role != "admin"
        or session is None
        or session.user_id != user_id
        or session.expires_at <= utcnow()
    ):
        raise QBOAuthorizationError("admin_session_required")


def begin(db):
    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))


def callback_uri(db):
    origin = public_origin(db)
    parsed = urlsplit(origin)
    if (
        not origin
        or not parsed.hostname
        or not (
            parsed.scheme == "https"
            or (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"})
        )
    ):
        raise QBOAuthorizationError("public_url_required")
    return origin + CALLBACK_PATH


def start_oauth(db, config, source_id, user_id, session_id):
    begin(db)
    active_session(db, user_id, session_id)
    source = db.get(Source, source_id, populate_existing=True)
    if source is None:
        raise QBOAuthorizationError("source_not_found")
    app = load_app_config(db, config, source_environment(source))
    if not app.configured:
        raise QBOAuthorizationError("app_credentials_required")
    if pending_revocations(load_source_credentials(source, config)):
        raise QBOAuthorizationError("revocation_cleanup_pending")
    if source.config.get("connection_status") == "disconnecting":
        raise QBOAuthorizationError("disconnect_pending")
    uri = callback_uri(db)
    token = secrets.token_urlsafe(32)
    row = OAuthState(
        state_hash=digest(token),
        source_id=source.id,
        user_id=user_id,
        session_id=session_id,
        app_generation=app.generation,
        source_generation=source_generation(source),
        redirect_uri=uri,
        expires_at=utcnow() + timedelta(minutes=10),
    )
    db.add(row)
    db.flush()
    audit(
        db,
        "qbo.oauth_started",
        "sources",
        source.id,
        after={"environment": app.environment, "expires_at": row.expires_at.isoformat()},
    )
    db.commit()
    return {
        "authorization_url": AUTHORIZATION_URL
        + "?"
        + urlencode(
            {
                "client_id": app.client_id,
                "response_type": "code",
                "scope": ACCOUNTING_SCOPE,
                "redirect_uri": uri,
                "state": token,
            }
        ),
        "expires_at": row.expires_at,
    }


def state_source(db, state, user_id, session_id):
    if not isinstance(state, str) or not 40 <= len(state) <= 100:
        raise QBOAuthorizationError("invalid_oauth_state")
    row = db.scalar(select(OAuthState).where(OAuthState.state_hash == digest(state)))
    if (
        row is None
        or row.user_id != user_id
        or row.session_id != session_id
        or row.consumed_at is not None
        or row.expires_at <= utcnow()
    ):
        raise QBOAuthorizationError("invalid_oauth_state")
    return row.source_id


def _accept_callback(
    db, config, *, state, code, realm_id, user_id, session_id, transport=None, error=None
):
    from app.connectors.qbo import QBOClient, token_credentials
    from app.scheduler.service import enqueue_source_disconnect, enqueue_source_poll

    begin(db)
    active_session(db, user_id, session_id)
    source_id = state_source(db, state, user_id, session_id)
    row = db.scalar(select(OAuthState).where(OAuthState.state_hash == digest(state)))
    source = db.get(Source, source_id, populate_existing=True)
    app = load_app_config(db, config, source_environment(source))
    if app.generation != row.app_generation or source_generation(source) != row.source_generation:
        raise QBOAuthorizationError("oauth_configuration_changed")
    row.consumed_at = utcnow()
    db.flush()
    if error:
        audit(
            db, "qbo.oauth_cancelled", "sources", source.id, after={"reason": "provider_declined"}
        )
        db.commit()
        raise QBOAuthorizationError("authorization_declined")
    if (
        not code
        or len(code) > 4096
        or not isinstance(realm_id, str)
        or not realm_id.isascii()
        or not realm_id.isdecimal()
        or len(realm_id) > 50
    ):
        db.commit()
        raise QBOAuthorizationError("invalid_oauth_response")
    cancelled_at = source.config.get("disconnect_requested_at")
    if cancelled_at and datetime.fromisoformat(cancelled_at) >= row.created_at:
        audit(
            db,
            "qbo.oauth_cancelled",
            "sources",
            source.id,
            after={"reason": "disconnect_requested"},
        )
        db.commit()
        raise QBOAuthorizationError("authorization_cancelled")
    if pending_revocations(load_source_credentials(source, config)):
        db.commit()
        raise QBOAuthorizationError("revocation_cleanup_pending")
    bound_realm = source.config.get("realm_id")
    if bound_realm and bound_realm != realm_id:
        audit(
            db,
            "qbo.oauth_rejected",
            "sources",
            source.id,
            after={"reason": "company_binding_mismatch"},
        )
        db.commit()
        raise QBOAuthorizationError("company_binding_mismatch")
    duplicate = db.scalar(
        select(Source.id).where(
            Source.kind == "qbo",
            Source.id != source.id,
            func.coalesce(Source.config["environment"].as_string(), "sandbox") == app.environment,
            Source.config["realm_id"].as_string() == realm_id,
        )
    )
    if duplicate:
        db.commit()
        raise QBOAuthorizationError("company_already_connected")
    uri, generation, started_at = row.redirect_uri, row.source_generation, row.created_at
    db.commit()
    # No transaction holds the SQLite writer while exchanging the authorization code.
    client = QBOClient(app, transport=transport)
    try:
        response = client.exchange_code(code, uri)
    finally:
        client.close()
    stored = False
    try:
        values = token_credentials(
            response,
            realm_id=realm_id,
            app_generation=app.generation,
            source_generation=generation + 1,
            now=utcnow(),
        )
        begin(db)
        active_session(db, user_id, session_id)
        source = db.get(Source, source_id, populate_existing=True)
        current_app = load_app_config(db, config, source_environment(source))
        if current_app.generation != app.generation or source_generation(source) != generation:
            raise QBOAuthorizationError("oauth_configuration_changed")
        if db.scalar(
            select(Source.id).where(
                Source.kind == "qbo",
                Source.id != source.id,
                func.coalesce(Source.config["environment"].as_string(), "sandbox")
                == app.environment,
                Source.config["realm_id"].as_string() == realm_id,
            )
        ):
            raise QBOAuthorizationError("company_already_connected")
        values["client_id"] = app.client_id
        cancelled_at = source.config.get("disconnect_requested_at")
        cancelled = bool(cancelled_at and datetime.fromisoformat(cancelled_at) >= started_at)
        source.config = {
            **source.config,
            "realm_id": realm_id,
            "connection_generation": generation + 1,
            "connection_status": "disconnecting" if cancelled else "connected",
        }
        save_source_credentials(db, source, config, values)
        source.cursor = None
        source.last_error = None
        audit(
            db,
            "qbo.connected",
            "sources",
            source.id,
            after={"environment": app.environment, "realm_id": realm_id, "cancelled": cancelled},
        )
        if cancelled:
            enqueue_source_disconnect(db, source.id, actor_user_id=None)
        else:
            enqueue_source_poll(db, source.id, mode="full", actor_user_id=user_id)
        db.commit()
        stored = True
    except Exception:
        db.rollback()
        if not stored:
            cleanup_rejected_grant(db, config, source_id, app, response, transport=transport)
        raise
    if cancelled:
        raise QBOAuthorizationError("authorization_cancelled")
    return source_id


def accept_callback(db, config, **kwargs):
    try:
        return _accept_callback(db, config, **kwargs)
    except Exception:
        db.rollback()
        raise


def webhook_signals(db, config, environment, raw, signature):
    from app.scheduler.service import enqueue_source_poll

    app = load_app_config(db, config, environment)
    expected = base64.b64encode(
        hmac.new((app.verifier_token or "").encode(), raw, hashlib.sha256).digest()
    ).decode()
    if (
        not app.verifier_token
        or not isinstance(signature, str)
        or not hmac.compare_digest(expected.encode(), signature.encode())
    ):
        raise QBOAuthorizationError("invalid_webhook_signature")
    try:
        events = json.loads(raw)
        if not isinstance(events, list) or len(events) > 10000:
            raise ValueError
        for event in events:
            if (
                not isinstance(event, dict)
                or not isinstance(event.get("id"), str)
                or not 1 <= len(event["id"]) <= 256
                or not isinstance(event.get("type"), str)
                or not isinstance(event.get("intuitaccountid"), str)
                or not isinstance(event.get("source"), str)
                or not 1 <= len(event["source"]) <= 4096
                or event.get("specversion") != "1.0"
            ):
                raise ValueError
    except (ValueError, TypeError, UnicodeDecodeError):
        raise QBOAuthorizationError("invalid_webhook_payload") from None
    begin(db)
    current = load_app_config(db, config, environment)
    if current.generation != app.generation:
        raise QBOAuthorizationError("webhook_configuration_changed")
    sources = {
        source.config.get("realm_id"): source
        for source in db.scalars(select(Source).where(Source.kind == "qbo"))
        if source_environment(source) == environment
        and source.config.get("connection_status") == "connected"
        and source.config.get("webhooks_enabled", False)
    }
    for event in events:
        entity = event["type"].split(".")
        if (
            len(entity) != 4
            or entity[0] != "qbo"
            or entity[1] not in {"customer", "vendor"}
            or entity[2] not in {"created", "updated", "deleted", "merged"}
            or entity[3] != "v1"
        ):
            continue
        source = sources.get(event["intuitaccountid"])
        if source is None:
            continue
        key = digest(
            json.dumps(
                [
                    environment,
                    digest(app.client_id),
                    event["source"],
                    event["intuitaccountid"],
                    event["id"],
                ],
                separators=(",", ":"),
            )
        )
        if db.scalar(
            select(AuditLog.id).where(
                AuditLog.action == "qbo.webhook_received",
                func.json_extract(AuditLog.after, literal_column("'$.event_key'")) == key,
            )
        ):
            continue
        attempt = enqueue_source_poll(db, source.id)
        audit(
            db,
            "qbo.webhook_received",
            "sources",
            source.id,
            after={"event_key": key, "event_type": event["type"], "attempt_id": attempt.id},
        )
        db.flush()
    db.commit()


MAX_PENDING_REVOCATIONS = 32


def pending_revocations(values):
    items = values.get("pending_revocations", [])
    if not isinstance(items, list) or len(items) > MAX_PENDING_REVOCATIONS:
        raise QBOAuthorizationError("invalid_revocation_backlog")
    if any(
        not isinstance(item, dict)
        or item.get("environment") not in {"sandbox", "production"}
        or any(
            not isinstance(item.get(key), str) or not item[key]
            for key in ("client_id", "client_secret", "token")
        )
        for item in items
    ):
        raise QBOAuthorizationError("invalid_revocation_backlog")
    return items


def revoke_grant(app, token, transport):
    from app.connectors.qbo import QBOClient

    client = None
    try:
        client = QBOClient(app, transport=transport)
        client.revoke(token)
        return True
    except Exception:
        return False
    finally:
        if client is not None:
            client.close()


def cleanup_rejected_grant(db, config, source_id, app, response, *, transport=None):
    from app.scheduler.service import enqueue_source_disconnect

    token = response.get("refresh_token") or response.get("access_token")
    if not isinstance(token, str) or not token:
        return
    db.rollback()
    revoked = revoke_grant(app, token, transport)
    begin(db)
    original_actor = db.info.get("actor_user_id")
    db.info["actor_user_id"] = None
    try:
        source = db.get(Source, source_id, populate_existing=True)
        execution = db.info.get("operation_execution")
        if execution:
            execution.check_fence(db)
        if not revoked:
            values = load_source_credentials(source, config)
            items = pending_revocations(values)
            if not any(
                item["environment"] == app.environment
                and item["client_id"] == app.client_id
                and item["token"] == token
                for item in items
            ):
                # Start/callback refuse ANY existing backlog under the same source
                # lease, so an accepted grant cannot grow an unbounded backlog.
                if len(items) >= MAX_PENDING_REVOCATIONS:
                    raise QBOAuthorizationError("revocation_cleanup_capacity")
                items = [
                    *items,
                    {
                        "environment": app.environment,
                        "client_id": app.client_id,
                        "client_secret": app.client_secret,
                        "token": token,
                    },
                ]
            save_source_credentials(db, source, config, {**values, "pending_revocations": items})
            source.config = {
                **source.config,
                "revocation_status": "failed",
                "revocation_error_code": "qbo_revocation_failed",
            }
            enqueue_source_disconnect(db, source.id, actor_user_id=None)
        audit(
            db,
            "qbo.rejected_grant_cleanup",
            "sources",
            source.id,
            after={
                "revocation_status": "revoked" if revoked else "failed",
                "queued": not revoked,
                "request_actor_id": original_actor,
            },
        )
        db.commit()
    finally:
        db.info["actor_user_id"] = original_actor


def disconnect_source(db, config, source, *, transport=None):
    source_id, generation, credential_cipher = (
        source.id,
        source_generation(source),
        source.credentials_enc,
    )
    app = load_app_config(db, config, source_environment(source))
    values = load_source_credentials(source, config)
    pending = pending_revocations(values)
    cleanup_only = source.config.get("connection_status") not in {"disconnecting", "disconnected"}
    token = values.get("refresh_token") or values.get("access_token")
    requests = [
        (
            AppConfig(item["environment"], item["client_id"], item["client_secret"]),
            item["token"],
            item,
        )
        for item in pending
    ]
    if not cleanup_only and token:
        requests.append((app, token, None))
    db.rollback()
    remaining, current_failed = [], False
    for grant_app, grant_token, item in requests:
        if not revoke_grant(grant_app, grant_token, transport):
            if item is None:
                current_failed = True
            else:
                remaining.append(item)
    error = "qbo_revocation_failed" if remaining or current_failed else None
    outcome = "failed" if error else "revoked" if requests else "not_required"
    begin(db)
    source = db.get(Source, source_id, populate_existing=True)
    execution = db.info.get("operation_execution")
    if execution:
        execution.check_fence(db)
    if source_generation(source) != generation or source.credentials_enc != credential_cipher:
        db.rollback()
        raise QBOAuthorizationError("connection_changed_during_revocation")
    if cleanup_only:
        credentials = {key: value for key, value in values.items() if key != "pending_revocations"}
    elif current_failed:
        credentials = {key: value for key, value in values.items() if key != "pending_revocations"}
    else:
        credentials = {}
    if remaining:
        credentials["pending_revocations"] = remaining
    if credentials:
        save_source_credentials(db, source, config, credentials)
    else:
        source.credentials_enc = None
    source.config = {**source.config, "revocation_status": outcome, "revocation_error_code": error}
    if not cleanup_only:
        source.config = {
            **source.config,
            "connection_status": "disconnected",
            "webhooks_enabled": False,
        }
        if not error:
            source.config = {**source.config, "connection_generation": generation + 1}
    audit(
        db,
        "qbo.grant_cleanup" if cleanup_only else "qbo.disconnected",
        "sources",
        source.id,
        after={
            "revocation_status": outcome,
            "error_code": error,
            "remaining_grants": len(remaining),
            "cleanup_only": cleanup_only,
        },
    )
    db.commit()
    return {
        "source_id": source_id,
        "connection_status": source.config.get("connection_status", "configured"),
        "revocation_status": outcome,
        "error_code": error,
    }
