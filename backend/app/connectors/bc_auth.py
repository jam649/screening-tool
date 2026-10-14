"""Leased BC company discovery and explicit activation with late authority checks."""

from sqlalchemy import func, select, text

from app.audit import audit
from app.connectors.bc_common import (
    BCConfigurationError,
    canonical_uuid,
    load_bc_config,
    load_source_credentials,
    pending_cleanup,
    save_source_credentials,
    source_generation,
    utc_time,
)
from app.models import Source, User, UserSession, utcnow


class BCOperationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def begin(db):
    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))


def require_authority(db, *, required=False):
    actor = db.info.get("actor_user_id")
    if actor:
        user = db.get(User, actor, populate_existing=True)
        if user is None or not user.active or user.role != "admin":
            raise BCOperationError("admin_required")
        session_id = db.info.get("initiating_session_id")
        if session_id:
            session = db.get(UserSession, session_id, populate_existing=True)
            if session is None or session.user_id != actor or session.expires_at <= utcnow():
                raise BCOperationError("admin_required")
    elif required:
        raise BCOperationError("admin_required")


def bc_source(db, source_id):
    row = db.get(Source, source_id, populate_existing=True)
    if row is None or row.kind != "bc":
        raise BCOperationError("source_not_found")
    return row


def check_snapshot(db, config, source, snapshot, *, authority=True):
    if authority:
        require_authority(db)
    execution = db.info.get("operation_execution")
    if execution:
        execution.check_fence(db)
    current = load_bc_config(db, config, source)
    if current != snapshot:
        raise BCOperationError("connection_changed")


def acquire_source_token(db, config, source, *, transport=None, force=False):
    """Copy state, release reads, and cache a fresh token under a short fence writer."""
    from app.connectors.bc import BCClient, token_credentials

    snapshot = load_bc_config(db, config, source)
    if not snapshot.configured:
        raise BCOperationError("app_credentials_required")
    values = load_source_credentials(source, config)
    source_id = source.id
    usable = (
        not force
        and source.config.get("connection_status") != "reconnect_required"
        and values.get("tenant_id") == snapshot.tenant_id
        and values.get("client_id") == snapshot.client_id
        and values.get("app_generation") == snapshot.app_generation
        and values.get("source_generation") == snapshot.source_generation
        and isinstance(values.get("access_token"), str)
    )
    if usable:
        try:
            usable = (utc_time(values.get("access_expires_at")) - utcnow()).total_seconds() > 60
        except BCConfigurationError:
            usable = False
    db.rollback()
    if usable:
        return snapshot, values["access_token"]
    client = BCClient(snapshot, transport=transport)
    try:
        response = client.acquire_token()
    finally:
        client.close()
    token = token_credentials(
        response,
        tenant_id=snapshot.tenant_id,
        client_id=snapshot.client_id,
        app_generation=snapshot.app_generation,
        source_generation=snapshot.source_generation,
        now=utcnow(),
    )
    try:
        begin(db)
        source = bc_source(db, source_id)
        check_snapshot(db, config, source, snapshot)
        latest = load_source_credentials(source, config)
        save_source_credentials(db, source, config, {**latest, **token})
        audit(
            db,
            "bc.token_cached",
            "sources",
            source_id,
            after={"expires_at": token["access_expires_at"]},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return snapshot, token["access_token"]


def available_source(db, config, source):
    require_authority(db, required=True)
    if source.config.get("connection_status") == "disconnecting":
        raise BCOperationError("disconnect_pending")
    if pending_cleanup(load_source_credentials(source, config)):
        raise BCOperationError("cleanup_pending")


def discover_companies(db, config, source, *, transport=None):
    from app.connectors.bc import BCClient

    source_id = source.id
    available_source(db, config, source)
    snapshot, token = acquire_source_token(db, config, source, transport=transport)
    client = BCClient(snapshot, transport=transport)
    companies = []
    seen: set[str] = set()
    links: set[str] = set()
    next_link = None
    reacquired = False
    try:
        for _ in range(1000):
            try:
                response = client.company_page(token, next_link=next_link)
            except Exception as error:
                if reacquired or getattr(error, "status_code", None) != 401:
                    raise
                reacquired = True
                source = bc_source(db, source_id)
                snapshot, token = acquire_source_token(
                    db, config, source, transport=transport, force=True
                )
                response = client.company_page(token, next_link=next_link)
            rows = response.get("value")
            if not isinstance(rows, list):
                raise BCOperationError("invalid_company_response")
            for row in rows:
                if not isinstance(row, dict):
                    raise BCOperationError("invalid_company_response")
                company_id = canonical_uuid(row.get("id"))
                display_name = row.get("displayName")
                if (
                    company_id in seen
                    or len(seen) >= 20000
                    or not isinstance(display_name, str)
                    or len(display_name) > 1000
                ):
                    raise BCOperationError("invalid_company_response")
                seen.add(company_id)
                companies.append({"id": company_id, "display_name": display_name})
            next_link = response.get("@odata.nextLink")
            if not next_link:
                break
            if not isinstance(next_link, str) or next_link in links:
                raise BCOperationError("invalid_company_response")
            links.add(next_link)
        else:
            raise BCOperationError("invalid_company_response")
    finally:
        client.close()
    try:
        begin(db)
        source = bc_source(db, source_id)
        check_snapshot(db, config, source, snapshot)
        available_source(db, config, source)
        audit(db, "bc.companies_discovered", "sources", source_id, after={"count": len(companies)})
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"items": companies}


def connect_company(db, config, source, company_id, *, transport=None):
    from app.connectors.bc import BCClient
    from app.scheduler.service import enqueue_source_poll

    source_id, company_id = source.id, canonical_uuid(company_id)
    available_source(db, config, source)
    snapshot, token = acquire_source_token(db, config, source, transport=transport)
    if snapshot.company_id and snapshot.company_id != company_id:
        raise BCOperationError("company_binding_mismatch")
    client = BCClient(snapshot, transport=transport)
    try:
        try:
            company = client.get_company(token, company_id)
        except Exception as error:
            if getattr(error, "status_code", None) != 401:
                raise
            source = bc_source(db, source_id)
            snapshot, token = acquire_source_token(
                db, config, source, transport=transport, force=True
            )
            company = client.get_company(token, company_id)
        if not isinstance(company, dict) or canonical_uuid(company.get("id")) != company_id:
            raise BCOperationError("invalid_company_response")
    finally:
        client.close()
    try:
        begin(db)
        source = bc_source(db, source_id)
        check_snapshot(db, config, source, snapshot)
        available_source(db, config, source)
        duplicate = db.scalar(
            select(Source.id).where(
                Source.kind == "bc",
                Source.id != source_id,
                Source.config["tenant_id"].as_string() == snapshot.tenant_id,
                func.lower(Source.config["bc_environment"].as_string())
                == snapshot.environment.lower(),
                Source.config["company_id"].as_string() == company_id,
            )
        )
        if duplicate:
            raise BCOperationError("company_already_connected")
        values = load_source_credentials(source, config)
        values["source_generation"] = source_generation(source) + 1
        source.config = {
            **source.config,
            "company_id": company_id,
            "connection_generation": values["source_generation"],
            "connection_status": "connected",
            "cleanup_status": "not_required",
            "cleanup_error_code": None,
        }
        source.config = {
            key: value for key, value in source.config.items() if key != "disconnect_requested_at"
        }
        save_source_credentials(db, source, config, values)
        source.cursor, source.last_error = None, None
        attempt = enqueue_source_poll(
            db, source_id, mode="full", actor_user_id=db.info.get("actor_user_id")
        )
        audit(
            db,
            "bc.connected",
            "sources",
            source_id,
            after={"company_id": company_id, "attempt_id": attempt.id},
        )
        db.commit()
        return attempt.id
    except Exception:
        db.rollback()
        raise
