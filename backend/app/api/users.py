"""Administrator user management and anonymous, one-use invitation acceptance."""

import secrets
from datetime import timedelta
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import delete, func, select

from app.api.auth import SessionOut, SetupBody, UserOut, session_response
from app.api.deps import DB, AdminUser, check_origin
from app.api.review import begin_write
from app.audit import audit
from app.models import User, UserSession, utcnow
from app.models.invitations import UserInvitation
from app.security import PASSWORDS, create_session, digest

router = APIRouter()


class InvitationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=1, max_length=320)
    role: Literal["admin", "reviewer"] = "reviewer"

    @field_validator("email")
    @classmethod
    def canonical_email(cls, value):
        return SetupBody.valid_email(value.strip().casefold())


class TokenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: SecretStr = Field(min_length=40, max_length=100)


class AcceptBody(TokenBody):
    display_name: str = Field(min_length=1, max_length=200)
    password: SecretStr = Field(min_length=12, max_length=1024)

    @field_validator("display_name")
    @classmethod
    def nonblank_name(cls, value):
        return SetupBody.nonblank_name(value)


class UserChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["admin", "reviewer"] | None = None
    active: bool | None = None


def invitation_payload(row):
    return {
        "id": row.id,
        "email": row.email,
        "role": row.role,
        "expires_at": row.expires_at,
        "created_at": row.created_at,
        "used_at": row.used_at,
        "revoked_at": row.revoked_at,
    }


def validate_admin(db, actor_id):
    actor = db.get(User, actor_id)
    if actor is None or not actor.active or actor.role != "admin":
        raise HTTPException(403, "Active Admin role required")
    return actor


def valid_invitation(db, token):
    row = db.scalar(select(UserInvitation).where(UserInvitation.token_hash == digest(token)))
    if row is None or row.used_at or row.revoked_at or row.expires_at <= utcnow():
        raise HTTPException(
            400, "This invitation is invalid or expired; request another invitation"
        )
    issuer = db.get(User, row.invited_by_user_id)
    if issuer is None or not issuer.active or issuer.role != "admin":
        raise HTTPException(
            400, "This invitation is invalid or expired; request another invitation"
        )
    return row


@router.get("/users")
def users(user: AdminUser, db: DB):
    return {
        "items": [
            UserOut.model_validate(row) for row in db.scalars(select(User).order_by(User.email))
        ],
        "invitations": [
            invitation_payload(row)
            for row in db.scalars(
                select(UserInvitation).where(
                    UserInvitation.used_at.is_(None),
                    UserInvitation.revoked_at.is_(None),
                    UserInvitation.expires_at > utcnow(),
                )
            )
        ],
    }


@router.post("/users/invitations", status_code=201)
def invite(body: InvitationBody, user: AdminUser, db: DB):
    actor_id = user.id
    begin_write(db)
    validate_admin(db, actor_id)
    if db.scalar(select(User).where(User.email == body.email)):
        raise HTTPException(409, "This email already belongs to a user")
    for prior in db.scalars(
        select(UserInvitation).where(
            UserInvitation.email == body.email,
            UserInvitation.used_at.is_(None),
            UserInvitation.revoked_at.is_(None),
        )
    ):
        prior.revoked_at = utcnow()
        audit(
            db,
            "users.invitation_revoked",
            "user_invitations",
            prior.id,
            after={"email": prior.email, "reason": "replacement invitation"},
        )
    token = secrets.token_urlsafe(32)
    row = UserInvitation(
        id=str(uuid4()),
        email=body.email,
        role=body.role,
        token_hash=digest(token),
        invited_by_user_id=actor_id,
        expires_at=utcnow() + timedelta(hours=72),
    )
    db.add(row)
    db.flush()
    audit(
        db,
        "users.invited",
        "user_invitations",
        row.id,
        after={
            key: value.isoformat() if hasattr(value, "isoformat") else value
            for key, value in invitation_payload(row).items()
        },
    )
    db.commit()
    return {
        **invitation_payload(row),
        "invite_url": f"/accept-invite#token={token}",
        "delivery": "manual",
        "delivery_pending": True,
        "delivery_message": "Copy this invitation link for the recipient. SMTP is not configured.",
    }


@router.post("/users/invitations/{invitation_id}/revoke")
def revoke_invitation(invitation_id: str, user: AdminUser, db: DB):
    actor_id = user.id
    begin_write(db)
    validate_admin(db, actor_id)
    row = db.get(UserInvitation, invitation_id)
    if row is None:
        raise HTTPException(404, "Invitation not found")
    if row.used_at:
        raise HTTPException(409, "This invitation was accepted; manage the user's active status")
    if row.revoked_at is None:
        row.revoked_at = utcnow()
        audit(
            db,
            "users.invitation_revoked",
            "user_invitations",
            row.id,
            after={
                "email": row.email,
                "role": row.role,
                "revoked_at": row.revoked_at.isoformat(),
                "reason": "Admin withdrew invitation",
            },
        )
        db.commit()
    return invitation_payload(row)


@router.patch("/users/{user_id}")
def update_user(user_id: str, body: UserChanges, user: AdminUser, db: DB):
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(422, "Choose a role or active status to update")
    actor_id = user.id
    begin_write(db)
    validate_admin(db, actor_id)
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "User not found")
    loses_admin = (
        target.active
        and target.role == "admin"
        and (changes.get("active") is False or changes.get("role", target.role) != "admin")
    )
    if loses_admin:
        count = (
            db.scalar(
                select(func.count())
                .select_from(User)
                .where(User.active.is_(True), User.role == "admin")
            )
            or 0
        )
        if count <= 1:
            raise HTTPException(409, "Keep at least one active Admin")
    for key, value in changes.items():
        setattr(target, key, value)
    if not target.active:
        db.execute(delete(UserSession).where(UserSession.user_id == target.id))
    if not target.active or target.role != "admin":
        for invitation in db.scalars(
            select(UserInvitation).where(
                UserInvitation.invited_by_user_id == target.id,
                UserInvitation.used_at.is_(None),
                UserInvitation.revoked_at.is_(None),
            )
        ):
            invitation.revoked_at = utcnow()
            audit(
                db,
                "users.invitation_revoked",
                "user_invitations",
                invitation.id,
                after={
                    "email": invitation.email,
                    "issuer_user_id": target.id,
                    "revoked_at": invitation.revoked_at.isoformat(),
                    "reason": "Issuing Admin was deactivated or demoted",
                },
            )
    db.commit()
    return UserOut.model_validate(target)


@router.post("/auth/invite/validate")
def validate_invitation(body: TokenBody, request: Request, db: DB):
    check_origin(request)
    row = valid_invitation(db, body.token.get_secret_value())
    return {"email": row.email, "role": row.role, "expires_at": row.expires_at}


@router.post("/auth/invite/accept", status_code=201, response_model=SessionOut)
def accept_invitation(body: AcceptBody, request: Request, response: Response, db: DB):
    check_origin(request)
    begin_write(db)
    row = valid_invitation(db, body.token.get_secret_value())
    if db.scalar(select(User).where(User.email == row.email)):
        raise HTTPException(409, "This email already belongs to a user")
    user = User(
        id=str(uuid4()),
        email=row.email,
        role=row.role,
        display_name=body.display_name,
        active=True,
        password_hash=PASSWORDS.hash(body.password.get_secret_value()),
    )
    db.add(user)
    row.used_at = utcnow()
    db.info["actor_user_id"] = user.id
    db.flush()
    audit(
        db,
        "users.invitation_accepted",
        "user_invitations",
        row.id,
        after={"user_id": user.id, "email": user.email, "role": user.role},
    )
    token, session = create_session(db, user, request.app.state.config)
    db.commit()
    return session_response(user, session, token, response, request.app.state.config.session_hours)
