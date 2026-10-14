from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

from email_validator import EmailNotValidError, validate_email
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictInt, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Config
from app.models import Setting
from app.security import decrypt_json, encrypt_json

PUBLIC_DEFAULTS = {
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_tls": "starttls",
    "smtp_from": "",
    "report_recipients": [],
    "report_time": "07:00",
    "likely_alerts_enabled": False,
    "public_base_url": "",
}


class NotificationConfigurationError(ValueError):
    pass


def canonical_email(value: str) -> str:
    if any(c in value for c in "\r\n\x00"):
        raise ValueError("Enter a valid email address")
    local = value.strip().casefold().endswith(".local")
    candidate = value.strip()[:-6] + ".test" if local else value.strip()
    try:
        parsed = validate_email(candidate, check_deliverability=False, test_environment=True)
    except EmailNotValidError:
        raise ValueError("Enter a valid email address") from None
    normalized = parsed.ascii_email or parsed.normalized
    return (normalized[:-5] + ".local" if local else normalized).casefold()


def validate_time(value: str) -> str:
    if (
        len(value) != 5
        or value[2] != ":"
        or not value[:2].isdigit()
        or not value[3:].isdigit()
        or int(value[:2]) > 23
        or int(value[3:]) > 59
    ):
        raise ValueError("Choose a time as HH:MM")
    return value


class NotificationChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")
    smtp_host: str | None = Field(default=None, max_length=253)
    smtp_port: StrictInt | None = Field(default=None, ge=1, le=65535)
    smtp_tls: Literal["starttls", "ssl", "none"] | None = None
    smtp_from: str | None = Field(default=None, max_length=320)
    report_recipients: list[str] | None = Field(default=None, max_length=100)
    report_time: str | None = None
    likely_alerts_enabled: bool | None = None
    public_base_url: str | None = Field(default=None, max_length=2000)
    smtp_username: SecretStr | None = Field(default=None, min_length=1, max_length=320)
    smtp_password: SecretStr | None = Field(default=None, min_length=1, max_length=1024)
    clear_credentials: bool = False

    @field_validator("smtp_host")
    @classmethod
    def host(cls, value):
        if value is None or value == "":
            return value
        if any(c.isspace() or c in "/@?#" or ord(c) < 32 for c in value):
            raise ValueError("Enter a hostname or IP address without a URL")
        return value.strip()

    @field_validator("smtp_from")
    @classmethod
    def sender(cls, value):
        return canonical_email(value) if value else value

    @field_validator("report_recipients")
    @classmethod
    def recipients(cls, value):
        return list(dict.fromkeys(canonical_email(v) for v in value)) if value is not None else None

    @field_validator("report_time")
    @classmethod
    def time(cls, value):
        return validate_time(value) if value is not None else value

    @field_validator("public_base_url")
    @classmethod
    def base_url(cls, value):
        if not value:
            return value
        try:
            parsed = urlsplit(value)
            _ = parsed.port
            allowed_scheme = parsed.scheme == "https" or (
                parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            )
            if (
                not allowed_scheme
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or parsed.path not in {"", "/"}
                or any(c.isspace() for c in value)
            ):
                raise ValueError
        except ValueError:
            raise ValueError("Use an HTTPS origin without credentials, query or path") from None
        return value.rstrip("/")


@dataclass(frozen=True)
class SMTPSettings:
    host: str
    port: int
    tls: str
    sender: str
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)

    @property
    def configured(self):
        return bool(self.host and self.sender)


def public_settings(db: Session):
    values = dict(PUBLIC_DEFAULTS)
    for row in db.scalars(select(Setting).where(Setting.key.in_(PUBLIC_DEFAULTS))):
        if not row.encrypted:
            values[row.key] = row.value
    return values


def credentials(db: Session, config: Config):
    row = db.scalar(select(Setting).where(Setting.key == "smtp_credentials"))
    if row is None:
        return {"username": None, "password": None}
    if not row.encrypted or not isinstance(row.value, str):
        raise NotificationConfigurationError("SMTP credentials must be encrypted")
    try:
        result = decrypt_json(row.value, config)
        if not isinstance(result, dict):
            raise ValueError
        if any(
            result.get(key) is not None and not isinstance(result.get(key), str)
            for key in ("username", "password")
        ):
            raise ValueError
        if bool(result.get("username")) != bool(result.get("password")):
            raise ValueError
        return {"username": result.get("username"), "password": result.get("password")}
    except Exception:
        raise NotificationConfigurationError("SMTP credentials could not be read") from None


def load_smtp_settings(db: Session, config: Config):
    values, secret = public_settings(db), credentials(db, config)
    return SMTPSettings(
        host=values["smtp_host"],
        port=values["smtp_port"],
        tls=values["smtp_tls"],
        sender=values["smtp_from"],
        **secret,
    )


def notification_settings_payload(db: Session, config: Config):
    secret = credentials(db, config)
    return {
        **public_settings(db),
        "smtp_username_configured": bool(secret["username"]),
        "smtp_password_configured": bool(secret["password"]),
    }


def save_notification_settings(db: Session, config: Config, body: NotificationChanges):
    values, secret = public_settings(db), credentials(db, config)
    changes = body.model_dump(
        exclude_none=True, exclude={"smtp_username", "smtp_password", "clear_credentials"}
    )
    values.update(changes)
    if body.clear_credentials:
        if body.smtp_username or body.smtp_password:
            raise NotificationConfigurationError(
                "Choose replacement credentials or clear, not both"
            )
        secret = {"username": None, "password": None}
    else:
        for name in ("username", "password"):
            value = getattr(body, "smtp_" + name)
            if value is not None:
                secret[name] = value.get_secret_value()
    if bool(secret["username"]) != bool(secret["password"]):
        raise NotificationConfigurationError("Provide both SMTP username and password")
    if values["smtp_tls"] == "none" and (
        values["smtp_host"] not in {"localhost", "127.0.0.1", "::1", ""} or secret["username"]
    ):
        raise NotificationConfigurationError(
            "Plain SMTP is only available for anonymous loopback fixtures"
        )
    if values["likely_alerts_enabled"] and not all(
        values[key] for key in ("smtp_host", "smtp_from", "report_recipients", "public_base_url")
    ):
        raise NotificationConfigurationError(
            "Configure SMTP, recipients and public URL before enabling likely alerts"
        )
    for key, value in changes.items():
        row = db.scalar(select(Setting).where(Setting.key == key))
        if row:
            row.value, row.encrypted = value, False
        else:
            db.add(Setting(key=key, value=value, encrypted=False))
    if body.clear_credentials or body.smtp_username is not None or body.smtp_password is not None:
        row = db.scalar(select(Setting).where(Setting.key == "smtp_credentials"))
        encrypted = encrypt_json(secret, config)
        if row:
            row.value, row.encrypted = encrypted, True
        else:
            db.add(Setting(key="smtp_credentials", value=encrypted, encrypted=True))
    db.flush()
