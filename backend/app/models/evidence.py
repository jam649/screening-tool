"""Immutable participation evidence for every screened party, including zero hits."""

from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Created, Identity


class RunPartyEvidence(Identity, Created, Base):
    __tablename__ = "run_party_evidence"
    __table_args__ = (
        UniqueConstraint("run_id", "party_id", name="uq_run_party_evidence"),
        CheckConstraint("candidates_considered >= 0", name="valid_evidence_candidates"),
        CheckConstraint(
            "hits_recorded >= 0 AND hits_recorded <= candidates_considered",
            name="valid_evidence_hits",
        ),
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("screening_runs.id"), index=True)
    party_id: Mapped[str] = mapped_column(ForeignKey("parties.id"), index=True)
    party_hash: Mapped[str] = mapped_column(String(64))
    party_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    source_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    scoring_context: Mapped[str] = mapped_column(String(64))
    decision_context: Mapped[str] = mapped_column(String(64))
    candidates_considered: Mapped[int] = mapped_column(Integer)
    hits_recorded: Mapped[int] = mapped_column(Integer)
    result_counts: Mapped[dict[str, int]] = mapped_column(JSON)
