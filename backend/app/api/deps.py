import secrets
from collections.abc import Iterator
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User, UserSession, utcnow
from app.security import COOKIE_NAME, digest


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as db:
        yield db


DB = Annotated[Session, Depends(get_db)]


def check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    # Browsers always send Origin on cross-origin unsafe requests. CLI clients need not.
    if origin:
        try:
            parsed = urlsplit(origin)
        except ValueError as exc:
            raise HTTPException(403, "Cross-origin request rejected") from exc
        allowed = request.app.state.config.allowed_origins
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.netloc != request.headers.get("host")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or (allowed and origin not in allowed)
        ):
            raise HTTPException(403, "Cross-origin request rejected")


def authenticated_session(request: Request, db: DB) -> UserSession:
    token = request.cookies.get(COOKIE_NAME)
    session = (
        db.scalar(select(UserSession).where(UserSession.token_hash == digest(token)))
        if token
        else None
    )
    if session is None or session.expires_at <= utcnow():
        raise HTTPException(401, "Sign in required")
    user = db.get(User, session.user_id)
    if user is None or not user.active:
        raise HTTPException(401, "Sign in required")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        check_origin(request)
        token = request.headers.get("x-csrf-token", "")
        if not secrets.compare_digest(token.encode(), session.csrf_token.encode()):
            raise HTTPException(403, "CSRF token required")
    db.info["actor_user_id"] = user.id
    return session


AuthSession = Annotated[UserSession, Depends(authenticated_session)]


def current_user(session: AuthSession, db: DB) -> User:
    user = db.get(User, session.user_id)
    assert user is not None
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def admin_user(user: CurrentUser) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Admin role required")
    return user


AdminUser = Annotated[User, Depends(admin_user)]
