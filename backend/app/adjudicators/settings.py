"""Encrypted model policy and audited hosted consent; never project credentials."""

from dataclasses import asdict
from ipaddress import ip_address
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adjudicators.contracts import PRIVACY_NOTICE, ModelConfig, ModelConfigurationError
from app.config import Config
from app.models import AuditLog, Setting
from app.security import decrypt_json, encrypt_json

SETTING_KEY = "model_config"
ACK_ACTION = "settings.model_hosted_acknowledged"


def validate_laya_base_url(value: str) -> str:
    """Only this host's loopback or explicit Docker host bridge may receive data."""
    try:
        if (
            not isinstance(value, str)
            or not value
            or any(c.isspace() or ord(c) < 32 for c in value)
        ):
            raise ValueError
        parsed = urlsplit(value)
        host = parsed.hostname
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or "?" in value
            or "#" in value
        ):
            raise ValueError
        if host not in {"localhost", "host.docker.internal"}:
            address = ip_address(host)
            if not address.is_loopback or "%" in host:
                raise ValueError
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        if parsed.netloc.endswith(":"):
            raise ValueError
        authority = f"[{host}]" if ":" in host else host
        if port is not None:
            authority += f":{port}"
        return urlunsplit((parsed.scheme, authority, "", "", ""))
    except (ValueError, TypeError, AttributeError):
        raise ModelConfigurationError("Choose a same-host Laya HTTP endpoint") from None


def model_acknowledgment_valid(db: Session, model: ModelConfig) -> bool:
    reference = model.hosted_acknowledgment_audit_id
    event = db.get(AuditLog, reference) if isinstance(reference, str) else None
    if event is None or event.action != ACK_ACTION or event.actor_user_id is None:
        return False
    proof = event.after or {}
    actor = proof.get("actor", {})
    return (
        proof.get("notice") == PRIVACY_NOTICE
        and proof.get("acknowledgment") == PRIVACY_NOTICE
        and isinstance(actor, dict)
        and actor.get("id") == event.actor_user_id
        and actor.get("role") == "admin"
        and actor.get("active") is True
    )


def load_model_config(db: Session, config: Config) -> ModelConfig:
    row = db.scalar(select(Setting).where(Setting.key == SETTING_KEY))
    if row is None:
        return ModelConfig()
    try:
        if not row.encrypted:
            raise ValueError
        values = decrypt_json(row.value, config)
        if not isinstance(values, dict) or set(values) - set(ModelConfig.__dataclass_fields__):
            raise ValueError
        model = ModelConfig(**values)
        validate_laya_base_url(model.laya_base_url)
        for key in (model.jev_api_key, model.laya_api_key):
            if (
                not isinstance(key, str)
                or len(key) > 8192
                or any(not 32 <= ord(c) <= 126 for c in key)
            ):
                raise ValueError
        if model.jev_api_key and not model_acknowledgment_valid(db, model):
            raise ValueError
        if model.provider == "jev" and not model.jev_api_key:
            raise ValueError
        return model
    except Exception:
        raise ModelConfigurationError("Model configuration unavailable") from None


def save_model_config(db: Session, config: Config, model: ModelConfig) -> None:
    encrypted = encrypt_json(asdict(model), config)
    row = db.scalar(select(Setting).where(Setting.key == SETTING_KEY))
    if row is None:
        db.add(Setting(key=SETTING_KEY, value=encrypted, encrypted=True))
    else:
        row.value, row.encrypted = encrypted, True
    db.flush()


def public_model_settings(db: Session, config: Config) -> dict:
    model = load_model_config(db, config)
    return {
        "provider": model.provider,
        "generation": model.generation,
        "w_det": model.w_det,
        "w_model": model.w_model,
        "jev_model": model.jev_model,
        "laya_base_url": model.laya_base_url,
        "laya_model": model.laya_model,
        "laya_max_tokens": model.laya_max_tokens,
        "jev_api_key_configured": bool(model.jev_api_key),
        "laya_api_key_configured": bool(model.laya_api_key),
        "hosted_acknowledgment_valid": bool(model.jev_api_key)
        and model_acknowledgment_valid(db, model),
        "privacy_notice": PRIVACY_NOTICE,
    }
