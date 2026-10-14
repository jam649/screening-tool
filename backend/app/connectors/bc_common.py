"""Private global Entra app and source-scoped Business Central state."""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from app.models import Setting, utcnow
from app.security import decrypt_json, encrypt_json

APP_SETTING = "bc_app"
API_ORIGIN = "https://api.businesscentral.dynamics.com"
TOKEN_ORIGIN = "https://login.microsoftonline.com"
TOKEN_SCOPE = API_ORIGIN + "/.default"
WEBHOOK_PATH = "/api/v1/webhooks/bc/"
RESOURCES = ("customers", "vendors")
MAX_CLEANUP = 32


class BCConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class BCAppConfig:
    client_id: str = field(default="", repr=False)
    client_secret: str = field(default="", repr=False)
    generation: int = 0

    @property
    def configured(self):
        return bool(self.client_id and self.client_secret)


@dataclass(frozen=True)
class BCConfig:
    tenant_id: str
    environment: str
    company_id: str | None
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    app_generation: int
    source_generation: int

    @property
    def configured(self):
        return bool(self.client_id and self.client_secret)


def canonical_uuid(value):
    try:
        if not isinstance(value, str):
            raise ValueError
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise BCConfigurationError("A valid UUID is required") from None


def valid_environment(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,28}", value):
        raise BCConfigurationError("A valid Business Central environment is required")
    return value


def source_generation(source):
    value = source.config.get("connection_generation", 0)
    if type(value) is not int or value < 0:
        raise BCConfigurationError("Business Central connection generation is invalid")
    return value


def source_identity(source):
    if source.kind != "bc":
        raise BCConfigurationError("Choose a Business Central source")
    values = source.config
    company = values.get("company_id")
    return (
        canonical_uuid(values.get("tenant_id")),
        valid_environment(values.get("bc_environment")),
        canonical_uuid(company) if company else None,
    )


def load_app_config(db, config) -> BCAppConfig:
    row = db.scalar(select(Setting).where(Setting.key == APP_SETTING))
    if row is None:
        return BCAppConfig()
    try:
        if not row.encrypted:
            raise ValueError
        values = decrypt_json(row.value, config)
        client_id, secret = values.get("client_id", ""), values.get("client_secret", "")
        generation = values.get("generation", 0)
        if (
            not isinstance(client_id, str)
            or not isinstance(secret, str)
            or bool(client_id) != bool(secret)
            or type(generation) is not int
            or generation < 0
        ):
            raise ValueError
        if client_id:
            client_id = canonical_uuid(client_id)
        return BCAppConfig(client_id, secret, generation)
    except Exception:
        raise BCConfigurationError("Business Central app configuration could not be read") from None


def save_app_config(db, config, app: BCAppConfig):
    payload = encrypt_json(
        {
            "client_id": app.client_id,
            "client_secret": app.client_secret,
            "generation": app.generation,
        },
        config,
    )
    row = db.scalar(select(Setting).where(Setting.key == APP_SETTING))
    if row:
        row.value, row.encrypted = payload, True
    else:
        db.add(Setting(key=APP_SETTING, value=payload, encrypted=True))
    db.flush()


def load_bc_config(db, config, source) -> BCConfig:
    tenant, environment, company = source_identity(source)
    app = load_app_config(db, config)
    return BCConfig(
        tenant,
        environment,
        company,
        app.client_id,
        app.client_secret,
        app.generation,
        source_generation(source),
    )


def load_source_credentials(source, config) -> dict:
    if not source.credentials_enc:
        return {}
    try:
        value = decrypt_json(source.credentials_enc, config)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except Exception:
        raise BCConfigurationError("Business Central credentials could not be read") from None


def save_source_credentials(db, source, config, values):
    source.credentials_enc = encrypt_json(values, config) if values else None
    db.flush()


def utc_time(value):
    try:
        if not isinstance(value, str):
            raise ValueError
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(UTC)
    except (ValueError, TypeError):
        raise BCConfigurationError("Business Central timestamp is invalid") from None


def private_subscriptions(values):
    rows = values.get("subscriptions", [])
    if not isinstance(rows, list) or len(rows) > 2:
        raise BCConfigurationError("Business Central subscription state is invalid")
    resources = set()
    for row in rows:
        if (
            not isinstance(row, dict)
            or row.get("resource") not in RESOURCES
            or row["resource"] in resources
            or any(
                not isinstance(row.get(key), str) or not row[key]
                for key in ("subscription_id", "client_state", "resource_path", "notification_url")
            )
        ):
            raise BCConfigurationError("Business Central subscription state is invalid")
        resources.add(row["resource"])
    return rows


def pending_cleanup(values):
    rows = values.get("pending_cleanup", [])
    if not isinstance(rows, list) or len(rows) > MAX_CLEANUP:
        raise BCConfigurationError("Business Central cleanup state is invalid")
    for row in rows:
        if not isinstance(row, dict) or any(
            not isinstance(row.get(key), str) or not row[key]
            for key in (
                "tenant_id",
                "environment",
                "client_id",
                "client_secret",
                "client_state",
                "resource_path",
                "notification_url",
            )
        ):
            raise BCConfigurationError("Business Central cleanup state is invalid")
        if row.get("subscription_id") is not None and (
            not isinstance(row["subscription_id"], str) or not row["subscription_id"]
        ):
            raise BCConfigurationError("Business Central cleanup state is invalid")
        canonical_uuid(row["tenant_id"])
        canonical_uuid(row["client_id"])
        valid_environment(row["environment"])
    return rows


def public_origin(db):
    row = db.scalar(select(Setting).where(Setting.key == "public_base_url"))
    return row.value if row and not row.encrypted and isinstance(row.value, str) else ""


def public_app_settings(db, config):
    app, origin = load_app_config(db, config), public_origin(db)
    return {
        "client_id_configured": bool(app.client_id),
        "client_secret_configured": bool(app.client_secret),
        "configured": app.configured,
        "webhook_base_url": origin + WEBHOOK_PATH.rstrip("/") if origin else None,
    }


def connection_payload(source, config, *, origin=""):
    tenant, environment, company = source_identity(source)
    values = load_source_credentials(source, config)
    subscriptions = private_subscriptions(values)
    cleanup = pending_cleanup(values)
    public_rows = []
    for resource in RESOURCES:
        row = next((item for item in subscriptions if item["resource"] == resource), None)
        if row:
            try:
                expired = utc_time(row.get("expiration_date_time")) <= utcnow()
            except BCConfigurationError:
                expired = True
            status = "failed" if row.get("last_error_code") else "expired" if expired else "active"
            url_changed = bool(
                origin and row["notification_url"] != origin + WEBHOOK_PATH + source.id
            )
            if url_changed:
                status = "failed"
            public_rows.append(
                {
                    "resource": resource,
                    "subscription_id": row["subscription_id"],
                    "expiration_date_time": row.get("expiration_date_time"),
                    "status": status,
                    "last_error_code": "bc_webhook_url_changed"
                    if url_changed
                    else row.get("last_error_code"),
                }
            )
    enabled = bool(source.config.get("webhooks_enabled"))
    if cleanup:
        subscription_status = "cleanup_pending"
    elif not enabled:
        subscription_status = "disabled"
    elif len(public_rows) == 2 and all(row["status"] == "active" for row in public_rows):
        subscription_status = "active"
    elif source.config.get("subscription_error_code") or public_rows:
        subscription_status = "degraded"
    else:
        subscription_status = "pending"
    return {
        "source_id": source.id,
        "tenant_id": tenant,
        "bc_environment": environment,
        "company_id": company,
        "status": source.config.get("connection_status", "configured"),
        "access_token_configured": bool(values.get("access_token")),
        "access_expires_at": values.get("access_expires_at"),
        "webhooks_enabled": enabled,
        "webhook_url": origin + WEBHOOK_PATH + source.id if origin else None,
        "subscription_status": subscription_status,
        "cleanup_status": source.config.get(
            "cleanup_status", "pending" if cleanup else "not_required"
        ),
        "cleanup_error_code": source.config.get("cleanup_error_code"),
        "subscriptions": public_rows,
    }
