"""Frozen single-use OAuth attempts, source realm binding and webhook receipts.

Revision ID: 64a12ef079bb
Revises: 0319e85bd430
"""

import sqlalchemy as sa

from alembic import op

revision = "64a12ef079bb"
down_revision = "0319e85bd430"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "oauth_states",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("state_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("source_id", sa.String(36), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("app_generation", sa.Integer(), nullable=False),
        sa.Column("source_generation", sa.Integer(), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "app_generation >= 0 AND source_generation >= 0", name="valid_oauth_generation"
        ),
    )
    op.create_index("ix_oauth_states_source_id", "oauth_states", ["source_id"])
    op.create_index("ix_oauth_states_session_id", "oauth_states", ["session_id"])
    op.execute("""CREATE TRIGGER oauth_states_frozen_inputs BEFORE UPDATE ON oauth_states
        WHEN OLD.id IS NOT NEW.id OR
            OLD.created_at IS NOT NEW.created_at OR
            OLD.state_hash IS NOT NEW.state_hash OR
            OLD.source_id IS NOT NEW.source_id OR
            OLD.user_id IS NOT NEW.user_id OR
            OLD.session_id IS NOT NEW.session_id OR
            OLD.app_generation IS NOT NEW.app_generation OR
            OLD.source_generation IS NOT NEW.source_generation OR
            OLD.redirect_uri IS NOT NEW.redirect_uri OR
            OLD.expires_at IS NOT NEW.expires_at OR
        (OLD.consumed_at IS NOT NEW.consumed_at AND
            (OLD.consumed_at IS NOT NULL OR NEW.consumed_at IS NULL))
        BEGIN SELECT RAISE(ABORT, 'OAuth inputs are frozen and state is single-use'); END""")
    op.execute("""CREATE TRIGGER oauth_states_no_delete BEFORE DELETE ON oauth_states
        BEGIN SELECT RAISE(ABORT, 'OAuth provenance cannot be deleted'); END""")
    op.execute("""CREATE TRIGGER oauth_states_no_replace BEFORE INSERT ON oauth_states
        WHEN EXISTS (SELECT 1 FROM oauth_states WHERE id=NEW.id OR state_hash=NEW.state_hash)
        BEGIN SELECT RAISE(ABORT, 'OAuth provenance cannot be replaced'); END""")
    op.execute("""CREATE UNIQUE INDEX uq_qbo_webhook_receipt
        ON audit_log(json_extract("after", '$.event_key'))
        WHERE action='qbo.webhook_received'""")
    op.execute("""CREATE UNIQUE INDEX uq_qbo_source_realm
        ON sources(COALESCE(json_extract(config, '$.environment'), 'sandbox'),
            json_extract(config, '$.realm_id'))
        WHERE kind='qbo' AND json_type(config, '$.realm_id')='text'
            AND length(json_extract(config, '$.realm_id')) > 0""")


def downgrade():
    raise RuntimeError("Refusing to erase permanent OAuth and webhook provenance")
