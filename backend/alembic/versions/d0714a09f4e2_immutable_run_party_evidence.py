"""Immutable original participation evidence, including zero-hit parties.

Revision ID: d0714a09f4e2
Revises: ba12f476ff8a
"""

import sqlalchemy as sa

from alembic import op

revision = "d0714a09f4e2"
down_revision = "ba12f476ff8a"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "run_party_evidence",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("screening_runs.id"), nullable=False),
        sa.Column("party_id", sa.String(36), sa.ForeignKey("parties.id"), nullable=False),
        sa.Column("party_hash", sa.String(64), nullable=False),
        sa.Column("party_snapshot", sa.JSON(), nullable=False),
        sa.Column("source_snapshot", sa.JSON(), nullable=False),
        sa.Column("scoring_context", sa.String(64), nullable=False),
        sa.Column("decision_context", sa.String(64), nullable=False),
        sa.Column("candidates_considered", sa.Integer(), nullable=False),
        sa.Column("hits_recorded", sa.Integer(), nullable=False),
        sa.Column("result_counts", sa.JSON(), nullable=False),
        sa.UniqueConstraint("run_id", "party_id", name="uq_run_party_evidence"),
        sa.CheckConstraint("candidates_considered >= 0", name="valid_evidence_candidates"),
        sa.CheckConstraint(
            "hits_recorded >= 0 AND hits_recorded <= candidates_considered",
            name="valid_evidence_hits",
        ),
    )
    for column in ("run_id", "party_id"):
        op.create_index(f"ix_run_party_evidence_{column}", "run_party_evidence", [column])
    for operation in ("UPDATE", "DELETE"):
        op.execute(f"""CREATE TRIGGER run_party_evidence_no_{operation.lower()}
            BEFORE {operation} ON run_party_evidence
            BEGIN SELECT RAISE(ABORT, 'run-party evidence is immutable'); END""")
    op.execute("""CREATE TRIGGER run_party_evidence_no_replace BEFORE INSERT
        ON run_party_evidence WHEN EXISTS (SELECT 1 FROM run_party_evidence
            WHERE id=NEW.id OR (run_id=NEW.run_id AND party_id=NEW.party_id))
        BEGIN SELECT RAISE(ABORT, 'run-party evidence is immutable'); END""")


def downgrade():
    raise RuntimeError("Run-party evidence cannot be downgraded; restore a reviewed backup")
