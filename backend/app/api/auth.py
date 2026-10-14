from datetime import timedelta
from typing import Literal
from uuid import uuid4

from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, or_, select, text

from app.api.deps import DB, AuthSession, check_origin
from app.audit import audit
from app.config import DISCLAIMER, DISCLAIMER_URL, PRODUCT_NAME
from app.models import LoginAttempt, User, UserSession, utcnow
from app.security import COOKIE_NAME, PASSWORDS, create_session, login_identifier, verify_password

router = APIRouter()


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    email: str
    display_name: str
    role: str
    active: bool


class LoginBody(BaseModel):
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1024)

    @field_validator("email")
    @classmethod
    def canonical_email(cls, value: str) -> str:
        return value.strip().casefold()


class SetupBody(LoginBody):
    display_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=12, max_length=1024)
    disclaimer_acknowledged: Literal[True]

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        try:
            # On-prem deployments legitimately use special-use .local and .test domains.
            local_domain = value.endswith(".local")
            candidate = value[:-6] + ".test" if local_domain else value
            parsed = validate_email(candidate, check_deliverability=False, test_environment=True)
            if local_domain:
                # email-validator permits .test explicitly, but always rejects .local.
                # Substitute only the suffix for syntax validation; preserve the identity.
                if parsed.ascii_domain is None or len(parsed.ascii_domain) + 1 > 253:
                    raise ValueError("Enter a valid email address")
                return (parsed.normalized[:-5] + ".local").casefold()
            return parsed.normalized.casefold()
        except EmailNotValidError as exc:
            raise ValueError("Enter a valid email address") from exc

    @field_validator("display_name")
    @classmethod
    def nonblank_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Enter a display name")
        return value.strip()


class SessionOut(BaseModel):
    user: UserOut
    csrf_token: str


def session_response(
    user: User, session: UserSession, token: str, response: Response, hours: int
) -> SessionOut:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=hours * 3600,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return SessionOut(user=UserOut.model_validate(user), csrf_token=session.csrf_token)


@router.get("/setup")
def setup_status(db: DB):
    return {
        "needs_setup": db.scalar(select(func.count()).select_from(User)) == 0,
        "product_name": PRODUCT_NAME,
        "disclaimer": DISCLAIMER,
        "disclaimer_url": DISCLAIMER_URL,
    }


@router.post("/setup", status_code=201, response_model=SessionOut)
def setup(body: SetupBody, request: Request, response: Response, db: DB):
    check_origin(request)
    # Serialize the check+insert so two concurrent setup requests cannot create two Admins.
    db.execute(text("BEGIN IMMEDIATE"))
    if db.scalar(select(func.count()).select_from(User)):
        raise HTTPException(409, "Setup has already been completed")
    user = User(
        id=str(uuid4()),
        email=str(body.email),
        display_name=body.display_name,
        role="admin",
        password_hash=PASSWORDS.hash(body.password),
        active=True,
    )
    db.info["actor_user_id"] = user.id
    db.add(user)
    db.flush()
    audit(
        db,
        "setup.disclaimer_acknowledged",
        "users",
        user.id,
        after={"disclaimer": DISCLAIMER, "source_url": DISCLAIMER_URL},
    )
    token, session = create_session(db, user, request.app.state.config)
    db.commit()
    return session_response(user, session, token, response, request.app.state.config.session_hours)


@router.post("/auth/login", response_model=SessionOut)
def login(body: LoginBody, request: Request, response: Response, db: DB):
    check_origin(request)
    config = request.app.state.config
    address = request.client.host if request.client else "unknown"
    address_hash = login_identifier(address, "address", config)
    identity_hash = login_identifier(body.email, "identity", config)
    since = utcnow() - timedelta(seconds=config.login_window_seconds)
    # Persistent rate limiting works after restarts and across worker processes.
    db.execute(text("BEGIN IMMEDIATE"))
    db.execute(delete(LoginAttempt).where(LoginAttempt.created_at < since))
    count = (
        db.scalar(
            select(func.count())
            .select_from(LoginAttempt)
            .where(
                LoginAttempt.created_at >= since,
                or_(
                    LoginAttempt.address_hash == address_hash,
                    LoginAttempt.identity_hash == identity_hash,
                ),
            )
        )
        or 0
    )
    if count >= config.login_attempt_limit:
        raise HTTPException(
            429,
            "Too many sign-in attempts. Try again later.",
            headers={"Retry-After": str(config.login_window_seconds)},
        )
    user = db.scalar(select(User).where(User.email == str(body.email)))
    valid = verify_password(body.password, user.password_hash if user else None)
    if not valid or user is None or not user.active:
        db.add(LoginAttempt(address_hash=address_hash, identity_hash=identity_hash))
        audit(
            db,
            "auth.login_failed",
            "users",
            user.id if user else None,
            after={"identity_hash": identity_hash, "address_hash": address_hash},
        )
        db.commit()
        raise HTTPException(401, "Email or password is incorrect")
    db.execute(
        delete(LoginAttempt).where(
            LoginAttempt.address_hash == address_hash,
            LoginAttempt.identity_hash == identity_hash,
        )
    )
    db.info["actor_user_id"] = user.id
    if PASSWORDS.check_needs_rehash(user.password_hash):
        user.password_hash = PASSWORDS.hash(body.password)
    token, session = create_session(db, user, config)
    audit(db, "auth.login", "users", user.id)
    db.commit()
    return session_response(user, session, token, response, config.session_hours)


@router.get("/auth/me", response_model=SessionOut)
def me(session: AuthSession, db: DB, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return SessionOut(
        user=UserOut.model_validate(db.get(User, session.user_id)), csrf_token=session.csrf_token
    )


@router.post("/auth/logout", status_code=204)
def logout(session: AuthSession, db: DB, response: Response):
    audit(db, "auth.logout", "users", session.user_id)
    db.delete(session)
    db.commit()
    response.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="lax")
