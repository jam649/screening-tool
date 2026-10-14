"""One-use invitations without plaintext invitation secrets.

Revision ID: e2c958197b30
Revises: 76bc4a5100c2
"""

import sqlalchemy as sa

from alembic import op

revision = "e2c958197b30"
down_revision = "76bc4a5100c2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_invitations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("invited_by_user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime()),
        sa.Column("revoked_at", sa.DateTime()),
        sa.CheckConstraint("role IN ('admin','reviewer')", name="valid_invitation_role"),
    )
    op.create_index("ix_user_invitations_email", "user_invitations", ["email"])


def downgrade():
    raise RuntimeError("Invitation audit history must be retained; restore a backup to roll back")
