"""Private QBO configuration shared by OAuth and the verified HTTP connector."""

from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import select

from app.models import Setting, Source
from app.security import decrypt_json, encrypt_json

QBOEnvironment = Literal["sandbox", "production"]
ENVIRONMENTS = ("sandbox", "production")
AUTHORIZATION_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REVOKE_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
ACCOUNTING_SCOPE = "com.intuit.quickbooks.accounting"
API_BASES = {
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
    "production": "https://quickbooks.api.intuit.com",
}
CALLBACK_PATH = "/api/v1/connectors/qbo/oauth/callback"
WEBHOOK_PATH = "/api/v1/webhooks/qbo/"


class QBOConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class AppConfig:
    environment: QBOEnvironment
    client_id: str = field(default="", repr=False)
    client_secret: str = field(default="", repr=False)
    verifier_token: str | None = field(default=None, repr=False)
    generation: int = 0

    @property
    def configured(self):
        return bool(self.client_id and self.client_secret)


def app_setting_key(environment):
    if environment not in ENVIRONMENTS:
        raise QBOConfigurationError("Choose sandbox or production")
    return f"qbo_app_{environment}"


def source_environment(source: Source) -> QBOEnvironment:
    value = source.config.get("environment", "sandbox")
    if source.kind != "qbo" or value not in ENVIRONMENTS:
        raise QBOConfigurationError("Choose a QuickBooks source with a valid environment")
    return value


def load_app_config(db, config, environment) -> AppConfig:
    key = app_setting_key(environment)
    row = db.scalar(select(Setting).where(Setting.key == key))
    if row is None:
        return AppConfig(environment)
    try:
        if not row.encrypted or not isinstance(row.value, str):
            raise ValueError
        values = decrypt_json(row.value, config)
        if not isinstance(values, dict):
            raise ValueError
        for name in ("client_id", "client_secret"):
            if not isinstance(values.get(name, ""), str):
                raise ValueError
        if values.get("verifier_token") is not None and not isinstance(
            values["verifier_token"], str
        ):
            raise ValueError
        generation = values.get("generation", 0)
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise ValueError
        return AppConfig(
            environment,
            values.get("client_id", ""),
            values.get("client_secret", ""),
            values.get("verifier_token"),
            generation,
        )
    except Exception:
        raise QBOConfigurationError("QuickBooks app configuration could not be read") from None


def save_app_config(db, config, app: AppConfig):
    value = encrypt_json(
        {
            "client_id": app.client_id,
            "client_secret": app.client_secret,
            "verifier_token": app.verifier_token,
            "generation": app.generation,
        },
        config,
    )
    row = db.scalar(select(Setting).where(Setting.key == app_setting_key(app.environment)))
    if row:
        row.value, row.encrypted = value, True
    else:
        db.add(Setting(key=app_setting_key(app.environment), value=value, encrypted=True))
    db.flush()


def load_source_credentials(source, config) -> dict:
    if not source.credentials_enc:
        return {}
    try:
        values = decrypt_json(source.credentials_enc, config)
        if not isinstance(values, dict):
            raise ValueError
        return values
    except Exception:
        raise QBOConfigurationError("QuickBooks connection credentials could not be read") from None


def save_source_credentials(db, source, config, payload) -> None:
    source.credentials_enc = encrypt_json(payload, config)
    db.flush()


def source_generation(source) -> int:
    value = source.config.get("connection_generation", 0)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise QBOConfigurationError("QuickBooks connection generation is invalid")
    return value


def public_origin(db):
    row = db.scalar(select(Setting).where(Setting.key == "public_base_url"))
    return row.value if row and not row.encrypted and isinstance(row.value, str) else ""


def app_flags(app):
    return {
        "client_id_configured": bool(app.client_id),
        "client_secret_configured": bool(app.client_secret),
        "verifier_token_configured": bool(app.verifier_token),
        "configured": app.configured,
    }


def public_app_settings(db, config):
    origin = public_origin(db)
    return {
        **{env: app_flags(load_app_config(db, config, env)) for env in ENVIRONMENTS},
        "redirect_uri": origin + CALLBACK_PATH if origin else None,
        "webhook_urls": {
            env: origin + WEBHOOK_PATH + env if origin else None for env in ENVIRONMENTS
        },
    }


def connection_payload(source, config):
    values = load_source_credentials(source, config)
    return {
        "source_id": source.id,
        "environment": source_environment(source),
        "status": source.config.get("connection_status", "configured"),
        "realm_id": source.config.get("realm_id") or values.get("realm_id"),
        "access_expires_at": values.get("access_expires_at"),
        "refresh_expires_at": values.get("refresh_expires_at"),
        "webhooks_enabled": bool(source.config.get("webhooks_enabled", False)),
        "revocation_status": source.config.get("revocation_status"),
        "revocation_error_code": source.config.get("revocation_error_code"),
    }
