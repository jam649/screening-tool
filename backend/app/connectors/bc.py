"""Fixed Microsoft contracts; synthetic transports do not establish live readiness.

Customer Person/Company supplies type; standard Vendor defaults to company.
Posting restrictions never mean deletion. Preserve original vendor objects and
the unchanged matching-hash no-write rule in the shared atomic ingestion path.
"""

import math
import re
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urljoin, urlsplit
from uuid import UUID

import httpx
from sqlalchemy import text

from app.audit import audit
from app.connectors.base import PartyInput, SyncBatch
from app.connectors.bc_common import (
    MAX_CLEANUP,
    BCConfig,
    BCConfigurationError,
    load_app_config,
    load_bc_config,
    load_source_credentials,
    pending_cleanup,
    private_subscriptions,
    save_source_credentials,
    source_generation,
    source_identity,
)
from app.connectors.sync import apply_sync
from app.models import Source, User, utcnow
from app.screening.normalize import normalize_country, normalize_name, party_record_hash

API_ORIGIN = "https://api.businesscentral.dynamics.com"
TOKEN_ORIGIN = "https://login.microsoftonline.com"
SCOPE = API_ORIGIN + "/.default"
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_PAGES = 1000
MAX_RECORDS = 500000
MAX_PAGE_RECORDS = 20000
OVERLAP = timedelta(minutes=5)
FUTURE_SKEW = timedelta(minutes=5)
FULL_INTERVAL = timedelta(days=1)
MAX_TOKEN_ACQUISITIONS = 4
ENTITIES = ("customers", "vendors")
TOKEN_FIELDS = (
    "tenant_id",
    "client_id",
    "app_generation",
    "source_generation",
    "access_token",
    "access_expires_at",
)


class BCError(ValueError):
    def __init__(self, code: str, status_code: int | None = None):
        self.code, self.status_code = code, status_code
        self.reconnect = code in {"invalid_client", "invalid_scope", "unauthorized"}
        super().__init__(
            f"Business Central operation failed ({code}); retry or reconnect if required"
        )


@dataclass(frozen=True)
class BCResponse:
    body: dict[str, Any] | None = field(repr=False)
    etag: str | None
    status_code: int


def identifier(value):
    # UUID-only routing is a product restriction; Entra also supports domains.
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value
    ):
        raise BCError("invalid_identifier")
    return str(UUID(value))


def timestamp(value):
    try:
        if not isinstance(value, str):
            raise ValueError
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        raise BCError("invalid_timestamp") from None


def _bearer(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 65536
        or any(not 33 <= ord(c) <= 126 for c in value)
    ):
        raise BCError("invalid_token_response")
    return value


def token_credentials(response, *, tenant_id, client_id, app_generation, source_generation, now):
    if (
        not isinstance(response, dict)
        or str(response.get("token_type", "")).casefold() != "bearer"
        or not isinstance(now, datetime)
        or now.tzinfo is None
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (app_generation, source_generation)
        )
    ):
        raise BCError("invalid_token_response")
    seconds = response.get("expires_in")
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not 0 < seconds <= 315360000:
        raise BCError("invalid_token_response")
    return {
        "tenant_id": identifier(tenant_id),
        "client_id": identifier(client_id),
        "app_generation": app_generation,
        "source_generation": source_generation,
        "access_token": _bearer(response.get("access_token")),
        "access_expires_at": (now.astimezone(UTC) + timedelta(seconds=seconds)).isoformat(),
    }


class BCClient:
    def __init__(self, config: BCConfig, *, transport=None, pause=time.sleep):
        tenant, client_id = identifier(config.tenant_id), identifier(config.client_id)
        if (
            tenant != config.tenant_id
            or client_id != config.client_id
            or not isinstance(config.environment, str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,28}", config.environment)
            or not isinstance(config.client_secret, str)
            or not config.client_secret
        ):
            raise BCError("invalid_configuration")
        if config.company_id is not None:
            identifier(config.company_id)
        self.config, self.pause = config, pause
        self.base = f"{API_ORIGIN}/v2.0/{tenant}/{config.environment}/api/v2.0"
        self.http = httpx.Client(
            transport=transport, timeout=20, trust_env=False, follow_redirects=False
        )

    def close(self):
        self.http.close()

    def _request(self, method, url, *, expected=(200,), **kwargs):
        # Token/subscribe writes are never replayed after ambiguous remote results.
        for attempt in range(3 if method == "GET" else 1):
            try:
                request = self.http.build_request(method, url, **kwargs)
                response = self.http.send(request, stream=True)
                try:
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise BCError("response_too_large")
                        chunks.append(chunk)
                    result = httpx.Response(
                        response.status_code,
                        headers=response.headers,
                        content=b"".join(chunks),
                        request=request,
                    )
                finally:
                    response.close()
            except httpx.HTTPError:
                if method == "GET" and attempt < 2:
                    self.pause(2**attempt)
                    continue
                raise BCError("transport_failed") from None
            if method == "GET" and (result.status_code == 429 or result.status_code >= 500):
                if attempt < 2:
                    try:
                        delay = float(result.headers.get("retry-after", 2**attempt))
                        delay = min(5, max(0, delay)) if math.isfinite(delay) else 2**attempt
                    except ValueError:
                        delay = 2**attempt
                    self.pause(delay)
                    continue
            if result.status_code not in expected:
                code = {
                    401: "unauthorized",
                    403: "forbidden",
                    404: "not_found",
                    409: "conflict",
                    412: "precondition_failed",
                }.get(result.status_code, "vendor_rejected")
                if url.startswith(TOKEN_ORIGIN + "/"):
                    try:
                        error = result.json().get("error")
                    except (ValueError, AttributeError):
                        error = None
                    if error in {"invalid_client", "invalid_scope"}:
                        code = error
                raise BCError(code, result.status_code)
            return result
        raise BCError("transport_failed")

    def _json(self, response):
        try:
            payload = response.json()
        except ValueError:
            raise BCError("invalid_response") from None
        if not isinstance(payload, dict) or "error" in payload:
            raise BCError("invalid_response")
        return payload

    def _headers(self, access_token):
        return {
            "Authorization": f"Bearer {_bearer(access_token)}",
            "Accept": "application/json",
            "Prefer": "odata.maxpagesize=300",
        }

    def _continuation(self, initial, next_link):
        if next_link is None:
            return initial
        if (
            not isinstance(next_link, str)
            or not 1 <= len(next_link) <= 16384
            or any(ord(c) < 33 or ord(c) == 127 for c in next_link)
            or "\\" in next_link
        ):
            raise BCError("untrusted_continuation")
        try:
            target = urljoin(initial, next_link)
            parsed, expected = urlsplit(target), urlsplit(initial)
            if (
                parsed.scheme != "https"
                or parsed.netloc != expected.netloc
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port is not None
                or parsed.fragment
                or parsed.path != expected.path
            ):
                raise BCError("untrusted_continuation")
        except ValueError:
            raise BCError("untrusted_continuation") from None
        return target

    def acquire_token(self):
        return self._json(
            self._request(
                "POST",
                f"{TOKEN_ORIGIN}/{self.config.tenant_id}/oauth2/v2.0/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "scope": SCOPE,
                },
                headers={"Accept": "application/json"},
            )
        )

    def company_page(self, access_token, *, next_link=None):
        return self._json(
            self._request(
                "GET",
                self._continuation(self.base + "/companies", next_link),
                headers=self._headers(access_token),
            )
        )

    def get_company(self, access_token, company_id):
        selected = identifier(company_id)
        if self.config.company_id is not None and selected != self.config.company_id:
            raise BCError("company_changed")
        payload = self._json(
            self._request(
                "GET", f"{self.base}/companies({selected})", headers=self._headers(access_token)
            )
        )
        if identifier(payload.get("id")) != selected:
            raise BCError("company_changed")
        return payload

    def collection_page(self, entity, access_token, *, params=None, next_link=None):
        initial = self._collection_url(entity)
        if next_link is not None and params is not None:
            raise BCError("untrusted_continuation")
        return self._json(
            self._request(
                "GET",
                self._continuation(initial, next_link),
                params=params,
                headers=self._headers(access_token),
            )
        )

    def _collection_url(self, entity):
        if entity not in ENTITIES or self.config.company_id is None:
            raise BCError("invalid_endpoint")
        return f"{self.base}/companies({identifier(self.config.company_id)})/{entity}"

    def subscription_request(
        self,
        method,
        access_token,
        *,
        subscription_id=None,
        payload=None,
        etag=None,
        params=None,
        next_link=None,
    ):
        if method not in {"GET", "POST", "PATCH", "DELETE"}:
            raise BCError("invalid_endpoint")
        initial = self.base + "/subscriptions"
        if subscription_id is not None:
            if (
                not isinstance(subscription_id, str)
                or not 1 <= len(subscription_id) <= 1024
                or any(ord(c) < 32 or ord(c) == 127 for c in subscription_id)
            ):
                raise BCError("invalid_subscription_id")
            key = quote(subscription_id.replace("'", "''"), safe="")
            initial += f"('{key}')"
        if (
            (method in {"PATCH", "DELETE"} and subscription_id is None)
            or (method == "POST" and subscription_id is not None)
            or (next_link is not None and (method != "GET" or subscription_id is not None))
            or (next_link is not None and params is not None)
            or (method != "GET" and params is not None)
            or (method in {"GET", "DELETE"} and payload is not None)
            or (method in {"POST", "PATCH"} and not isinstance(payload, dict))
        ):
            raise BCError("invalid_subscription_request")
        headers = self._headers(access_token)
        if method in {"PATCH", "DELETE"}:
            if (
                not isinstance(etag, str)
                or not 1 <= len(etag) <= 4096
                or any(ord(c) < 32 or ord(c) == 127 for c in etag)
            ):
                raise BCError("invalid_etag")
            headers["If-Match"] = etag
        kwargs = {"headers": headers}
        if payload is not None:
            kwargs["json"] = payload
        if params is not None:
            kwargs["params"] = params
        response = self._request(
            method,
            self._continuation(initial, next_link),
            expected=({"GET": 200, "POST": 201, "PATCH": 200, "DELETE": 204}[method],),
            **kwargs,
        )
        body = None if method == "DELETE" else self._json(response)
        actual_etag = response.headers.get("etag") or (body or {}).get("@odata.etag")
        if (
            not isinstance(actual_etag, str)
            or not actual_etag
            or any(ord(c) < 32 or ord(c) == 127 for c in actual_etag)
        ):
            actual_etag = None
        # A remotely accepted create without ETag still returns its actual ID;
        # lifecycle code must preserve/reconcile it rather than duplicating POST.
        return BCResponse(body, actual_etag, response.status_code)


def _string(raw, key):
    value = raw.get(key)
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > 10000 or "\x00" in value:
        raise BCError("invalid_party_record")
    return value.strip()


def normalize_entity(entity, raw):
    if entity not in ENTITIES or not isinstance(raw, dict):
        raise BCError("invalid_party_record")
    external_id = identifier(raw.get("id"))
    name = _string(raw, "displayName")
    if not name or not normalize_name(name).tokens:
        raise BCError("invalid_party_record")
    party_type = "company"
    if entity == "customers":
        kind = raw.get("type")
        if kind not in {"Company", "Person"}:
            raise BCError("invalid_party_record")
        party_type = "individual" if kind == "Person" else "company"
    try:
        country = normalize_country(_string(raw, "country"), strict=True)
    except ValueError:
        raise BCError("invalid_country") from None
    timestamp(raw.get("lastModifiedDateTime"))
    fields = {
        "name": name,
        "party_type": party_type,
        "role": "customer" if entity == "customers" else "vendor",
        "alt_names": [],
        "address_lines": list(
            dict.fromkeys(
                line for key in ("addressLine1", "addressLine2") if (line := _string(raw, key))
            )
        ),
        "city": _string(raw, "city") or None,
        "region": _string(raw, "state") or None,
        "postal_code": _string(raw, "postalCode") or None,
        "country_iso2": country,
    }
    return PartyInput(
        external_id=f"{'Customer' if entity == 'customers' else 'Vendor'}:{external_id}",
        **fields,
        raw=deepcopy(raw),
        record_hash=party_record_hash(fields),
    )


class BCConnector:
    def __init__(self, client, access_token, *, poll_started_at=None, request=None):
        self.client, self.access_token = client, access_token
        self.started = poll_started_at or utcnow()
        self.request = request

    def normalize(self, raw):
        return normalize_entity("customers", raw)

    def _fetch(self, *, since=None):
        parties, seen = [], set()
        for entity in ENTITIES:
            next_link, total, entity_count = None, None, 0
            params = {"$filter": f"lastModifiedDateTime gt {since.isoformat()}"} if since else None
            initial = self.client._collection_url(entity)
            visited = {str(httpx.URL(initial, params=params))}
            for _ in range(MAX_PAGES):
                page = (
                    self.request(entity, params=params, next_link=next_link)
                    if self.request
                    else self.client.collection_page(
                        entity, self.access_token, params=params, next_link=next_link
                    )
                )
                params = None
                rows = page.get("value") if isinstance(page, dict) else None
                if not isinstance(rows, list) or len(rows) > MAX_PAGE_RECORDS:
                    raise BCError("invalid_collection_response")
                if "@odata.count" in page:
                    count = page["@odata.count"]
                    if (
                        isinstance(count, bool)
                        or not isinstance(count, int)
                        or count < 0
                        or (total is not None and count != total)
                    ):
                        raise BCError("invalid_collection_response")
                    total = count
                observed = utcnow()
                for raw in rows:
                    party = normalize_entity(entity, raw)
                    if party.external_id in seen:
                        raise BCError("duplicate_entity")
                    modified = timestamp(raw["lastModifiedDateTime"])
                    if modified > observed + FUTURE_SKEW:
                        raise BCError("future_timestamp")
                    if since is not None and modified <= since:
                        raise BCError("invalid_incremental_response")
                    seen.add(party.external_id)
                    parties.append(party)
                    if len(parties) > MAX_RECORDS:
                        raise BCError("record_limit")
                entity_count += len(rows)
                if "@odata.nextLink" not in page:
                    if total is not None and total != entity_count:
                        raise BCError("incomplete_collection_response")
                    break
                next_link = page["@odata.nextLink"]
                if not isinstance(next_link, str) or not next_link:
                    raise BCError("invalid_continuation")
                resolved = self.client._continuation(initial, next_link)
                if resolved in visited:
                    raise BCError("invalid_continuation")
                visited.add(resolved)
            else:
                raise BCError("page_limit")
        return SyncBatch(parties, cursor=self.started.isoformat(), complete=since is None)

    def full_sync(self):
        return self._fetch()

    def incremental_sync(self, cursor):
        try:
            recorded = timestamp(cursor)
        except BCError:
            return self.full_sync()
        if recorded > self.started:
            return self.full_sync()
        return self._fetch(since=recorded - OVERLAP)


def sync_bc_source(db, config, source, *, mode="incremental", client=None, transport=None):
    if mode not in {"incremental", "full"}:
        raise BCError("invalid_sync_mode")
    source_id, public = source.id, deepcopy(source.config)
    bc = load_bc_config(db, config, source)
    identity = source_identity(source)
    if public.get("connection_status") != "connected" or bc.company_id is None:
        raise BCError("connection_changed")
    credentials = deepcopy(load_source_credentials(source, config))
    credential_owner = {key: credentials.get(key) for key in TOKEN_FIELDS}
    cursor, started = source.cursor, utcnow()
    try:
        last_full = timestamp(public.get("last_full_sync_at"))
        full_due = last_full > started or started - last_full >= FULL_INTERVAL
    except BCError:
        full_due = True
    db.rollback()
    owned = client is None
    client = client or BCClient(bc, transport=transport)
    acquisitions, unauthorized_reissued = 0, False

    def locked_state(session, row):
        session.refresh(row)
        execution = session.info.get("operation_execution")
        if execution:
            execution.check_fence(session)
        actor_id = session.info.get("actor_user_id")
        actor = session.get(User, actor_id, populate_existing=True) if actor_id else None
        if actor_id and (actor is None or not actor.active or actor.role != "admin"):
            raise BCError("authority_changed")
        current_app = load_app_config(session, config)
        current_values = load_source_credentials(row, config)
        try:
            private_subscriptions(current_values)
            pending_cleanup(current_values)
        except BCConfigurationError:
            raise BCError("invalid_private_state") from None
        if (
            source_identity(row) != identity
            or source_generation(row) != bc.source_generation
            or row.config.get("connection_status") != "connected"
            or current_app.client_id != bc.client_id
            or current_app.client_secret != bc.client_secret
            or current_app.generation != bc.app_generation
            or {key: current_values.get(key) for key in TOKEN_FIELDS} != credential_owner
        ):
            raise BCError("connection_changed")

    def acquire():
        nonlocal credentials, acquisitions, credential_owner
        if acquisitions >= MAX_TOKEN_ACQUISITIONS:
            raise BCError("token_acquisition_limit")
        acquisitions += 1
        response = client.acquire_token()
        token = token_credentials(
            response,
            tenant_id=bc.tenant_id,
            client_id=bc.client_id,
            app_generation=bc.app_generation,
            source_generation=bc.source_generation,
            now=utcnow(),
        )
        db.execute(text("BEGIN IMMEDIATE"))
        try:
            current = db.get(Source, source_id, populate_existing=True)
            if current is None:
                raise BCError("connection_changed")
            locked_state(db, current)
            # Preserve the latest private subscription and cleanup namespaces;
            # token acquisition is not permission to discard lifecycle evidence.
            merged = {**deepcopy(load_source_credentials(current, config)), **token}
            save_source_credentials(db, current, config, merged)
            audit(
                db,
                "bc.token_acquired",
                "sources",
                source_id,
                after={
                    "connection_generation": bc.source_generation,
                    "operation_attempt_id": getattr(
                        db.info.get("operation_execution"), "attempt_id", None
                    ),
                },
            )
            db.commit()
            credential_owner = {key: merged.get(key) for key in TOKEN_FIELDS}
            credentials = merged
        except Exception:
            db.rollback()
            raise

    def authorized(entity, *, params=None, next_link=None):
        nonlocal unauthorized_reissued
        # Long pagination may outlive a cached or newly issued token. Initial
        # issuance/proactive renewal does not consume the distinct 401 budget.
        if timestamp(credentials.get("access_expires_at")) <= utcnow() + timedelta(seconds=60):
            acquire()
        try:
            return client.collection_page(
                entity, credentials["access_token"], params=params, next_link=next_link
            )
        except BCError as error:
            if error.code != "unauthorized":
                raise
            if unauthorized_reissued or acquisitions >= MAX_TOKEN_ACQUISITIONS:
                raise
            unauthorized_reissued = True
            acquire()
            return client.collection_page(
                entity, credentials["access_token"], params=params, next_link=next_link
            )

    try:
        try:
            token_valid = (
                credentials.get("tenant_id") == bc.tenant_id
                and credentials.get("client_id") == bc.client_id
                and credentials.get("app_generation") == bc.app_generation
                and credentials.get("source_generation") == bc.source_generation
                and timestamp(credentials.get("access_expires_at"))
                > started + timedelta(seconds=60)
                and bool(_bearer(credentials.get("access_token")))
            )
        except BCError:
            token_valid = False
        if not token_valid:
            acquire()
        connector = BCConnector(
            client, credentials["access_token"], poll_started_at=started, request=authorized
        )
        batch = (
            connector.full_sync()
            if mode == "full" or full_due
            else connector.incremental_sync(cursor)
        )
        db.rollback()
        current = db.get(Source, source_id, populate_existing=True)
        if current is None:
            raise BCError("connection_changed")
        db.rollback()

        def before_apply(session, row):
            locked_state(session, row)
            if batch.complete:
                row.config = {**row.config, "last_full_sync_at": started.isoformat()}

        result = apply_sync(db, current, batch, before_apply=before_apply)
        return {
            **result.payload(),
            "record_count": current.record_count,
            "mode": "full" if batch.complete else "incremental",
            "pending_task_id": result.task_ids[0] if result.task_ids else None,
        }
    except BCError as error:
        db.rollback()
        db.execute(text("BEGIN IMMEDIATE"))
        try:
            current = db.get(Source, source_id, populate_existing=True)
            if current is None:
                db.rollback()
            else:
                locked_state(db, current)
                current.last_error = str(error)
                if error.reconnect:
                    from app.connectors.bc_subscriptions import cleanup_obligation
                    from app.scheduler.service import enqueue_bc_subscriptions

                    values = deepcopy(load_source_credentials(current, config))
                    obligations = deepcopy(pending_cleanup(values))
                    retained = []
                    for subscription in private_subscriptions(values):
                        obligation = cleanup_obligation(
                            bc, values.get("access_token"), subscription
                        )
                        if obligation in obligations:
                            continue
                        if len(obligations) < MAX_CLEANUP:
                            obligations.append(obligation)
                        else:
                            # Keep exact private custody at capacity; the system
                            # lifecycle worker drains pending obligations first.
                            retained.append(deepcopy(subscription))
                    values["subscriptions"] = retained
                    values["pending_cleanup"] = obligations
                    values.pop("access_token", None)
                    values.pop("access_expires_at", None)
                    save_source_credentials(db, current, config, values)
                    current.config = {
                        **current.config,
                        "connection_status": "reconnect_required",
                        "webhooks_enabled": False,
                        "cleanup_status": "pending" if obligations or retained else "not_required",
                    }
                    if obligations or retained:
                        requested_actor = db.info.pop("actor_user_id", None)
                        try:
                            attempt = enqueue_bc_subscriptions(db, source_id, actor_user_id=None)
                            audit(
                                db,
                                "bc.auth_cleanup_queued",
                                "sources",
                                source_id,
                                after={
                                    "attempt_id": attempt.id,
                                    "pending_cleanup": len(obligations),
                                    "retained_subscriptions": len(retained),
                                    "connection_generation": bc.source_generation,
                                },
                            )
                        finally:
                            if requested_actor is not None:
                                db.info["actor_user_id"] = requested_actor
                audit(
                    db,
                    "bc.sync_failed",
                    "sources",
                    source_id,
                    after={"error_code": error.code, "reconnect_required": error.reconnect},
                )
                db.commit()
        except BCError:
            db.rollback()
        raise
    finally:
        if owned:
            client.close()
