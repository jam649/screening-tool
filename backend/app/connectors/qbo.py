"""Verified Intuit HTTPS contracts; synthetic fixtures do not imply live validation.

CompanyName identifies a company; otherwise explicit GivenName/FamilyName identify
a person. DisplayName is the fallback company name, never a guessed person type.
DisplayName/PrintOnCheckName are aliases; FullyQualifiedName/contact names stay raw.
BillAddr is the primary locality, falling back to ShipAddr. Both street addresses
are retained for comparison and the complete unmodified vendor object stays raw.
"""

import re
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import text

from app.audit import audit
from app.connectors.base import PartyInput, SyncBatch
from app.connectors.qbo_common import (
    API_BASES,
    REVOKE_URL,
    TOKEN_URL,
    AppConfig,
    load_app_config,
    load_source_credentials,
    save_source_credentials,
    source_environment,
    source_generation,
)
from app.connectors.sync import apply_sync
from app.models import Source, User, utcnow
from app.screening.normalize import normalize_country, normalize_name, party_record_hash

MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_PAGES = 100
CDC_OVERLAP = timedelta(minutes=5)
ENTITIES = ("Customer", "Vendor")


class QBOError(ValueError):
    def __init__(self, code: str):
        self.code = code
        self.reconnect = code in {"invalid_grant", "refresh_expired"}
        super().__init__(f"QuickBooks operation failed ({code}); retry or reconnect if required")


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,50}", value):
        raise QBOError("invalid_identifier")
    return value


def timestamp(value):
    try:
        if not isinstance(value, str):
            raise ValueError
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        raise QBOError("invalid_timestamp") from None


def token_credentials(response, *, realm_id, app_generation, source_generation, now):
    """Use each actual returned rotating refresh token and its supplied lifetime."""
    if not isinstance(response, dict) or str(response.get("token_type", "")).casefold() != "bearer":
        raise QBOError("invalid_token_response")
    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (app_generation, source_generation)
        )
    ):
        raise QBOError("invalid_token_response")
    for key in ("access_token", "refresh_token"):
        if (
            not isinstance(response.get(key), str)
            or not response[key]
            or any(not 33 <= ord(c) <= 126 for c in response[key])
        ):
            raise QBOError("invalid_token_response")
    fields = {"expires_in": "access_expires_at", "x_refresh_token_expires_in": "refresh_expires_at"}
    if "x_refresh_token_hard_expires_in" in response:
        fields["x_refresh_token_hard_expires_in"] = "refresh_hard_expires_at"
    result = {
        "status": "connected",
        "realm_id": identifier(realm_id),
        "app_generation": app_generation,
        "source_generation": source_generation,
        "access_token": response["access_token"],
        "refresh_token": response["refresh_token"],
    }
    for supplied, stored in fields.items():
        seconds = response.get(supplied)
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, int)
            or not 0 < seconds <= 315360000
        ):
            raise QBOError("invalid_token_response")
        result[stored] = (now.astimezone(UTC) + timedelta(seconds=seconds)).isoformat()
    return result


class QBOClient:
    def __init__(self, app: AppConfig, *, transport=None, pause=time.sleep):
        if app.environment not in API_BASES or not app.configured:
            raise QBOError("app_not_configured")
        self.app, self.pause = app, pause
        self.http = httpx.Client(
            transport=transport, timeout=20, trust_env=False, follow_redirects=False
        )

    def close(self):
        self.http.close()

    def _request(self, method, url, **kwargs):
        # GET retries are bounded. A token POST is never blindly replayed after
        # uncertain transport/5xx failure: its grant may already have been consumed.
        for attempt in range(3 if method == "GET" else 1):
            try:
                response = self.http.request(method, url, **kwargs)
            except httpx.HTTPError:
                if method == "GET" and attempt < 2:
                    self.pause(2**attempt)
                    continue
                raise QBOError("transport_failed") from None
            if (
                method == "GET"
                and (response.status_code == 429 or response.status_code >= 500)
                and attempt < 2
            ):
                try:
                    delay = min(5, max(0, float(response.headers.get("retry-after", 2**attempt))))
                    if delay != delay:
                        delay = float(2**attempt)
                except ValueError:
                    delay = float(2**attempt)
                self.pause(delay)
                continue
            if response.status_code == 401:
                raise QBOError("unauthorized")
            if response.status_code < 200 or response.status_code >= 300:
                try:
                    error = response.json().get("error")
                except (ValueError, AttributeError):
                    error = None
                raise QBOError("invalid_grant" if error == "invalid_grant" else "vendor_rejected")
            if len(response.content) > MAX_RESPONSE_BYTES:
                raise QBOError("response_too_large")
            return response
        raise QBOError("transport_failed")

    def _json(self, response):
        try:
            payload = response.json()
        except ValueError:
            raise QBOError("invalid_response") from None
        if not isinstance(payload, dict) or "Fault" in payload:
            raise QBOError("invalid_response")
        return payload

    def exchange_code(self, code, redirect_uri):
        return self._json(
            self._request(
                "POST",
                TOKEN_URL,
                auth=httpx.BasicAuth(self.app.client_id, self.app.client_secret),
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
                headers={"Accept": "application/json"},
            )
        )

    def refresh(self, refresh_token):
        return self._json(
            self._request(
                "POST",
                TOKEN_URL,
                auth=httpx.BasicAuth(self.app.client_id, self.app.client_secret),
                data={"grant_type": "refresh_token", "refresh_token": refresh_token},
                headers={"Accept": "application/json"},
            )
        )

    def revoke(self, token):
        self._request(
            "POST",
            REVOKE_URL,
            auth=httpx.BasicAuth(self.app.client_id, self.app.client_secret),
            json={"token": token},
            headers={"Accept": "application/json"},
        )

    def data_json(self, realm_id, access_token, endpoint, params):
        if endpoint not in {"query", "cdc"}:
            raise QBOError("invalid_endpoint")
        url = f"{API_BASES[self.app.environment]}/v3/company/{identifier(realm_id)}/{endpoint}"
        return self._json(
            self._request(
                "GET",
                url,
                params=params,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
        )


def _string(raw, key):
    value = raw.get(key)
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > 10000 or "\x00" in value:
        raise QBOError("invalid_party_record")
    return value.strip()


def normalize_entity(entity, raw):
    if entity not in ENTITIES or not isinstance(raw, dict):
        raise QBOError("invalid_party_record")
    id = identifier(raw.get("Id"))
    status = raw.get("status")
    if status is not None and status != "Deleted":
        raise QBOError("invalid_party_record")
    if status != "Deleted" and not isinstance(raw.get("Active"), bool):
        raise QBOError("invalid_party_record")
    role = "customer" if entity == "Customer" else "vendor"
    if status == "Deleted" or raw.get("Active") is False:
        return PartyInput(
            external_id=f"{entity}:{id}", name="", role=role, raw=deepcopy(raw), deleted=True
        )
    company = _string(raw, "CompanyName")
    personal = " ".join(
        value
        for key in ("GivenName", "MiddleName", "FamilyName", "Suffix")
        if (value := _string(raw, key))
    )
    name = company or personal or _string(raw, "DisplayName")
    if not name or not normalize_name(name).tokens:
        raise QBOError("invalid_party_record")
    kind = "individual" if not company and personal else "company"
    aliases = list(
        dict.fromkeys(
            value
            for key in ("DisplayName", "PrintOnCheckName")
            if (value := _string(raw, key)) and value != name
        )
    )
    addresses = []
    for key in ("BillAddr", "ShipAddr"):
        address = raw.get(key)
        if address is not None and not isinstance(address, dict):
            raise QBOError("invalid_party_record")
        if address:
            addresses.append(address)
    primary = addresses[0] if addresses else {}
    try:
        country = normalize_country(_string(primary, "Country"), strict=True)
    except ValueError:
        raise QBOError("invalid_country") from None
    fields: dict[str, Any] = {
        "name": name,
        "party_type": kind,
        "role": role,
        "alt_names": aliases,
        "address_lines": list(
            dict.fromkeys(
                line
                for address in addresses
                for key in ("Line1", "Line2", "Line3", "Line4", "Line5")
                if (line := _string(address, key))
            )
        ),
        "city": _string(primary, "City") or None,
        "region": _string(primary, "CountrySubDivisionCode") or None,
        "postal_code": _string(primary, "PostalCode") or None,
        "country_iso2": country,
    }
    return PartyInput(
        external_id=f"{entity}:{id}",
        **fields,
        raw=deepcopy(raw),
        record_hash=party_record_hash(fields),
    )


class QBOConnector:
    def __init__(
        self, client, realm_id, access_token, *, poll_started_at=None, page_size=1000, request=None
    ):
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= 1000
        ):
            raise QBOError("invalid_page_size")
        self.client, self.realm_id, self.access_token = client, identifier(realm_id), access_token
        self.started = poll_started_at or utcnow()
        self.page_size, self.request = page_size, request

    def _data(self, endpoint, params):
        return (
            self.request(endpoint, params)
            if self.request
            else self.client.data_json(self.realm_id, self.access_token, endpoint, params)
        )

    def normalize(self, raw):
        return normalize_entity("Customer", raw)

    def full_sync(self):
        parties, seen = [], set()
        for entity in ENTITIES:
            start = 1
            expected_total = None
            entity_count = 0
            for _ in range(MAX_PAGES):
                payload = self._data(
                    "query",
                    {
                        "query": f"SELECT * FROM {entity} WHERE Active IN (true, false) "
                        f"STARTPOSITION {start} MAXRESULTS {self.page_size}"
                    },
                )
                page = payload.get("QueryResponse")
                if not isinstance(page, dict) or any(
                    key in page for key in ("Fault", *(name for name in ENTITIES if name != entity))
                ):
                    raise QBOError("invalid_query_response")
                if set(page) - {entity, "startPosition", "maxResults", "totalCount"}:
                    raise QBOError("invalid_query_response")
                rows = page.get(entity, [])
                if not isinstance(rows, list) or len(rows) > self.page_size:
                    raise QBOError("invalid_query_response")
                if "totalCount" in page:
                    reported = page["totalCount"]
                    if (
                        isinstance(reported, bool)
                        or not isinstance(reported, int)
                        or reported < 0
                        or (expected_total is not None and reported != expected_total)
                    ):
                        raise QBOError("invalid_query_response")
                    expected_total = reported
                for key, expected in (("startPosition", start), ("maxResults", len(rows))):
                    if key in page and (
                        isinstance(page[key], bool)
                        or not isinstance(page[key], int)
                        or page[key] != expected
                    ):
                        raise QBOError("invalid_query_response")
                for raw in rows:
                    party = normalize_entity(entity, raw)
                    if party.external_id in seen:
                        raise QBOError("duplicate_entity")
                    seen.add(party.external_id)
                    parties.append(party)
                entity_count += len(rows)
                if len(rows) < self.page_size:
                    if expected_total is not None and entity_count != expected_total:
                        raise QBOError("incomplete_query_response")
                    break
                start += len(rows)
            else:
                raise QBOError("query_page_limit")
        return SyncBatch(parties, cursor=self.started.isoformat(), complete=True)

    def incremental_sync(self, cursor):
        try:
            recorded = timestamp(cursor)
            since = recorded - CDC_OVERLAP
        except QBOError:
            return self.full_sync()
        if recorded > self.started or self.started - since >= timedelta(days=30):
            return self.full_sync()
        payload = self._data(
            "cdc", {"entities": "customer,vendor", "changedSince": since.isoformat()}
        )
        wrappers = payload.get("CDCResponse")
        if (
            not isinstance(wrappers, list)
            or len(wrappers) != 1
            or not isinstance(wrappers[0], dict)
        ):
            raise QBOError("invalid_cdc_response")
        pages = wrappers[0].get("QueryResponse")
        if (
            not isinstance(pages, list)
            or not pages
            or any(not isinstance(page, dict) or "Fault" in page for page in pages)
        ):
            raise QBOError("invalid_cdc_response")
        parties, seen, uncertain = [], set(), False
        counts = []
        for page in pages:
            if set(page) - {"Customer", "Vendor", "startPosition", "maxResults", "totalCount"}:
                raise QBOError("invalid_cdc_response")
            count = 0
            for entity in ENTITIES:
                rows = page.get(entity, [])
                if not isinstance(rows, list):
                    raise QBOError("invalid_cdc_response")
                count += len(rows)
                for raw in rows:
                    party = normalize_entity(entity, raw)
                    if party.external_id in seen:
                        raise QBOError("duplicate_entity")
                    seen.add(party.external_id)
                    parties.append(party)
            counts.append(count)
            for key in ("startPosition", "maxResults", "totalCount"):
                if key in page and (
                    isinstance(page[key], bool) or not isinstance(page[key], int) or page[key] < 0
                ):
                    raise QBOError("invalid_cdc_response")
            uncertain |= page.get("startPosition", 1) != 1 or page.get("maxResults", count) != count
        total = len(parties)
        for page, count in zip(pages, counts, strict=True):
            uncertain |= page.get("totalCount", count) not in {count, total}
        if uncertain or total >= 1000:
            return self.full_sync()
        if "time" in payload:
            timestamp(payload["time"])
        return SyncBatch(parties, cursor=self.started.isoformat(), complete=False)


def sync_qbo_source(db, config, source, *, mode="incremental", client=None, transport=None):
    """Copy inputs, release SQLite before HTTP, persist rotation, then apply atomically."""
    if mode not in {"incremental", "full"}:
        raise QBOError("invalid_sync_mode")
    source_id, environment = source.id, source_environment(source)
    generation, public = source_generation(source), deepcopy(source.config)
    credentials = deepcopy(load_source_credentials(source, config))
    app = load_app_config(db, config, environment)
    realm = identifier(credentials.get("realm_id"))
    if (
        public.get("connection_status") != "connected"
        or public.get("realm_id") != realm
        or credentials.get("source_generation") != generation
        or credentials.get("app_generation") != app.generation
        or credentials.get("client_id") != app.client_id
    ):
        raise QBOError("connection_changed")
    cursor, started = source.cursor, utcnow()
    db.rollback()
    owned_client = client is None
    client = client or QBOClient(app, transport=transport)
    refreshed = False

    def locked_state(session, row, *, rotation=False):
        session.refresh(row)
        execution = session.info.get("operation_execution")
        if execution:
            execution.check_fence(session)
        actor_id = session.info.get("actor_user_id")
        actor = session.get(User, actor_id, populate_existing=True) if actor_id else None
        if (
            not rotation
            and actor_id
            and (actor is None or not actor.active or actor.role != "admin")
        ):
            raise QBOError("authority_changed")
        current_app = load_app_config(session, config, environment)
        if (
            source_generation(row) != generation
            or row.config.get("realm_id") != realm
            or source_environment(row) != environment
            or current_app.client_id != app.client_id
            or current_app.generation != app.generation
        ):
            raise QBOError("connection_changed")
        allowed = {"connected", "disconnecting"} if rotation else {"connected"}
        if row.config.get("connection_status") not in allowed:
            raise QBOError("connection_changed")
        if not rotation and current_app.generation != credentials["app_generation"]:
            raise QBOError("connection_changed")
        return current_app

    def rotate():
        nonlocal credentials, refreshed
        if refreshed:
            raise QBOError("unauthorized")
        for field in ("refresh_expires_at", "refresh_hard_expires_at"):
            if field in credentials and timestamp(credentials[field]) <= utcnow():
                raise QBOError("refresh_expired")
        response = client.refresh(credentials["refresh_token"])
        rotated = token_credentials(
            response,
            realm_id=realm,
            app_generation=app.generation,
            source_generation=generation,
            now=utcnow(),
        )
        if "refresh_hard_expires_at" not in rotated and "refresh_hard_expires_at" in credentials:
            rotated["refresh_hard_expires_at"] = credentials["refresh_hard_expires_at"]
        db.execute(text("BEGIN IMMEDIATE"))
        requested_actor = db.info.pop("actor_user_id", None)
        try:
            current = db.get(Source, source_id, populate_existing=True)
            if current is None:
                raise QBOError("connection_changed")
            current_app = locked_state(db, current, rotation=True)
            # Rejected grants may have queued private cleanup while this request
            # was in flight. Copy the CURRENT backlog under the same fence;
            # rebuilding token fields must never erase those accepted grants.
            from app.connectors.qbo_auth import QBOAuthorizationError, pending_revocations

            try:
                backlog = pending_revocations(load_source_credentials(current, config))
            except QBOAuthorizationError:
                raise QBOError("invalid_revocation_backlog") from None
            if backlog:
                rotated["pending_revocations"] = deepcopy(backlog)
            rotated["app_generation"] = current_app.generation
            rotated["client_id"] = current_app.client_id
            save_source_credentials(db, current, config, rotated)
            audit(
                db,
                "qbo.tokens_rotated",
                "sources",
                source_id,
                after={
                    "connection_generation": generation,
                    "operation_attempt_id": getattr(
                        db.info.get("operation_execution"), "attempt_id", None
                    ),
                    "request_actor_id": requested_actor,
                },
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            if requested_actor is not None:
                db.info["actor_user_id"] = requested_actor
        credentials, refreshed = rotated, True

    def authorized(endpoint, params):
        try:
            return client.data_json(realm, credentials["access_token"], endpoint, params)
        except QBOError as error:
            if error.code != "unauthorized":
                raise
            rotate()
            return client.data_json(realm, credentials["access_token"], endpoint, params)

    try:
        if timestamp(credentials.get("access_expires_at")) <= started + timedelta(seconds=60):
            rotate()
        connector = QBOConnector(
            client, realm, credentials["access_token"], poll_started_at=started, request=authorized
        )
        batch = connector.full_sync() if mode == "full" else connector.incremental_sync(cursor)
        db.rollback()
        current = db.get(Source, source_id, populate_existing=True)
        if current is None:
            raise QBOError("connection_changed")
        db.rollback()
        result = apply_sync(db, current, batch, before_apply=locked_state)
        return {
            **result.payload(),
            "record_count": current.record_count,
            "mode": "full" if batch.complete else "incremental",
            "pending_task_id": result.task_ids[0] if result.task_ids else None,
        }
    except QBOError as error:
        db.rollback()
        db.execute(text("BEGIN IMMEDIATE"))
        try:
            current = db.get(Source, source_id, populate_existing=True)
            if (
                current
                and source_generation(current) == generation
                and current.config.get("realm_id") == realm
                and current.config.get("connection_status") == "connected"
            ):
                execution = db.info.get("operation_execution")
                if execution:
                    execution.check_fence(db)
                current.last_error = str(error)
                if error.reconnect:
                    current.config = {
                        **current.config,
                        "connection_status": "reconnect_required",
                        "webhooks_enabled": False,
                    }
                audit(
                    db,
                    "qbo.sync_failed",
                    "sources",
                    source_id,
                    after={"error_code": error.code, "reconnect_required": error.reconnect},
                )
                db.commit()
            else:
                db.rollback()
        except Exception:
            db.rollback()
            raise
        raise
    finally:
        if owned_client:
            client.close()
