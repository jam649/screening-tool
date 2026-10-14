"""Frozen, single-use connector authorization attempts; raw state is never stored."""

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Created, Identity, UTCDateTime


class OAuthState(Identity, Created, Base):
    __tablename__ = "oauth_states"
    __table_args__ = (
        CheckConstraint(
            "app_generation >= 0 AND source_generation >= 0", name="valid_oauth_generation"
        ),
    )
    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    # Logout normally deletes its session. Retain the opaque historical ID without
    # an FK that would block logout; callbacks still require that exact LIVE session.
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    app_generation: Mapped[int] = mapped_column(Integer)
    source_generation: Mapped[int] = mapped_column(Integer, default=0)
    redirect_uri: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
