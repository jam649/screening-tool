"""CSL fetch provenance, immutable listing revisions and durable screening tasks.

Revision ID: 32dfcc79a042
Revises: caa3a3b7e05a
"""

import sqlalchemy as sa

from alembic import op

revision = "32dfcc79a042"
down_revision = "caa3a3b7e05a"
branch_labels = None
depends_on = None


def upgrade():
    # SQLite's unnamed UNIQUE(file_sha256) requires rebuilding this parent table.
    # Disable FK enforcement outside a transaction for the rebuild, then verify all
    # relationships before re-enabling it. This preserves populated baseline databases.
    with op.get_context().autocommit_block():
        op.execute("PRAGMA foreign_keys=OFF")
    try:
        with op.batch_alter_table(
            "csl_versions", naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"}
        ) as batch:
            batch.drop_constraint("uq_csl_versions_file_sha256", type_="unique")
            batch.add_column(sa.Column("semantic_sha256", sa.String(64), nullable=True))
            batch.create_index("ix_csl_versions_file_sha256", ["file_sha256"])
            batch.create_index("ix_csl_versions_semantic_sha256", ["semantic_sha256"])
        with op.batch_alter_table(
            "csl_entries", naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"}
        ) as batch:
            batch.drop_constraint("uq_csl_entries_csl_id", type_="unique")
            batch.create_unique_constraint("uq_csl_source_id", ["source_list", "csl_id"])
        violations = op.get_bind().exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("Foreign key validation failed during CSL migration")
    finally:
        with op.get_context().autocommit_block():
            op.execute("PRAGMA foreign_keys=ON")

    op.create_table(
        "csl_fetches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime()),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("file_sha256", sa.String(64)),
        sa.Column("semantic_sha256", sa.String(64)),
        sa.Column("version_id", sa.String(36), sa.ForeignKey("csl_versions.id")),
        sa.Column("source_list_stamps", sa.JSON(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.CheckConstraint(
            "status IN ('fetching','success','failed')", name="valid_csl_fetch_status"
        ),
    )
    op.create_index("ix_csl_fetches_fetched_at", "csl_fetches", ["fetched_at"])
    op.create_index("ix_csl_fetches_status", "csl_fetches", ["status"])
    op.create_table(
        "csl_entry_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("version_id", sa.String(36), sa.ForeignKey("csl_versions.id"), nullable=False),
        sa.Column("entry_id", sa.String(36), sa.ForeignKey("csl_entries.id"), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.UniqueConstraint("version_id", "entry_id", name="uq_csl_version_entry"),
    )
    op.create_index("ix_csl_entry_revisions_version_id", "csl_entry_revisions", ["version_id"])
    op.create_index("ix_csl_entry_revisions_entry_id", "csl_entry_revisions", ["entry_id"])
    for operation in ("UPDATE", "DELETE"):
        op.execute(f"""CREATE TRIGGER csl_revision_no_{operation.lower()}
            BEFORE {operation} ON csl_entry_revisions
            BEGIN SELECT RAISE(ABORT, 'CSL revision evidence is immutable'); END""")
    op.execute("""CREATE TRIGGER csl_revision_no_replace BEFORE INSERT ON csl_entry_revisions
        WHEN EXISTS (SELECT 1 FROM csl_entry_revisions WHERE id=NEW.id
            OR (version_id=NEW.version_id AND entry_id=NEW.entry_id))
        BEGIN SELECT RAISE(ABORT, 'CSL revision evidence is immutable'); END""")
    op.create_table(
        "screening_tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("trigger", sa.String(30), nullable=False),
        sa.Column(
            "csl_version_id", sa.String(36), sa.ForeignKey("csl_versions.id"), nullable=False
        ),
        sa.Column("entry_ids", sa.JSON(), nullable=False),
        sa.Column("party_ids", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("last_error", sa.Text()),
        sa.CheckConstraint(
            "trigger IN ('nightly','list_change','party_change','manual')",
            name="valid_task_trigger",
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','complete','failed')", name="valid_task_status"
        ),
    )
    op.create_index("ix_screening_tasks_status", "screening_tasks", ["status"])


def downgrade():
    # Removing immutable evidence or reinstating a hash uniqueness rule can lose
    # real refresh history. Restore a pre-migration backup for rollback instead.
    raise RuntimeError("CSL provenance migration cannot be downgraded without losing evidence")
