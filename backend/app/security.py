import hashlib
import hmac
import json
import secrets
from datetime import timedelta
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import Config
from app.models import Setting, Source, User, UserSession, utcnow

PASSWORDS = PasswordHasher()
DUMMY_HASH = PASSWORDS.hash(secrets.token_urlsafe(32))
COOKIE_NAME = "screening_session"
KEY_CANARY = "screening-tool:key-binding:v1"


class ConfiguredTrustedHostMiddleware:
    """Read trusted hosts after lifespan has loaded the required deployment secret."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"}:
            middleware = TrustedHostMiddleware(
                self.app,
                allowed_hosts=scope["app"].state.config.allowed_hosts,
                www_redirect=False,
            )
            await middleware(scope, receive, send)
        else:
            await self.app(scope, receive, send)


def validate_key_binding(db: Session, config: Config, *, bind: bool = True) -> None:
    """Verify existing encryption; bind an absent canary only when explicitly permitted."""
    canary = db.scalar(select(Setting).where(Setting.key == "__encryption_key_canary"))
    try:
        if canary is not None:
            if decrypt_json(canary.value, config) != KEY_CANARY:
                raise InvalidToken
            return
        # Upgrade old stores safely before binding a key to their existing secrets.
        for value in db.scalars(
            select(Source.credentials_enc).where(Source.credentials_enc.is_not(None))
        ):
            if value is not None:
                decrypt_json(value, config)
        for setting in db.scalars(select(Setting).where(Setting.encrypted.is_(True))):
            decrypt_json(setting.value, config)
    except (InvalidToken, ValueError, TypeError) as exc:
        raise RuntimeError(
            "SCREENING_TOOL_SECRET_KEY does not match the database encryption key; "
            "restore the original key before starting"
        ) from exc
    if not bind:
        return
    db.add(
        Setting(
            key="__encryption_key_canary", value=encrypt_json(KEY_CANARY, config), encrypted=True
        )
    )
    db.commit()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def login_identifier(value: str, kind: str, config: Config) -> str:
    """Persist pseudonyms that cannot be guessed without the deployment secret."""
    return hmac.new(
        config.secret_key.get_secret_value().encode(),
        f"login:{kind}:{value}".encode(),
        hashlib.sha256,
    ).hexdigest()


def verify_password(password: str, stored: str | None) -> bool:
    try:
        return PASSWORDS.verify(stored or DUMMY_HASH, password) and stored is not None
    except (VerificationError, InvalidHashError):
        return False


def create_session(db: Session, user: User, config: Config) -> tuple[str, UserSession]:
    token = secrets.token_urlsafe(48)
    row = UserSession(
        user_id=user.id,
        token_hash=digest(token),
        csrf_token=secrets.token_urlsafe(32),
        expires_at=utcnow() + timedelta(hours=config.session_hours),
    )
    db.add(row)
    return token, row


def encrypt_json(value: Any, config: Config) -> str:
    return (
        Fernet(config.secret_key.get_secret_value().encode())
        .encrypt(json.dumps(value, sort_keys=True).encode())
        .decode()
    )


def decrypt_json(value: str, config: Config) -> Any:
    return json.loads(Fernet(config.secret_key.get_secret_value().encode()).decrypt(value.encode()))
