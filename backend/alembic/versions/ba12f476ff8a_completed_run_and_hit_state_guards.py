"""Keep completed run provenance and closed hit states immutable.

Revision ID: ba12f476ff8a
Revises: e2c958197b30
"""

from alembic import op

revision = "ba12f476ff8a"
down_revision = "e2c958197b30"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TRIGGER completed_run_no_update BEFORE UPDATE ON screening_runs
        WHEN OLD.finished_at IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'completed screening run provenance is immutable'); END""")
    op.execute("""CREATE TRIGGER screening_runs_no_delete BEFORE DELETE ON screening_runs
        BEGIN SELECT RAISE(ABORT, 'screening run provenance is immutable'); END""")
    op.execute("""CREATE TRIGGER screening_runs_no_replace BEFORE INSERT ON screening_runs
        WHEN EXISTS (SELECT 1 FROM screening_runs WHERE id=NEW.id)
        BEGIN SELECT RAISE(ABORT, 'screening run provenance is immutable'); END""")
    op.execute("""CREATE TRIGGER hit_status_transition BEFORE UPDATE OF status ON hits
        WHEN NEW.status IS NOT OLD.status AND (
            OLD.status != 'open' OR NEW.status != 'dispositioned'
            OR NOT EXISTS (SELECT 1 FROM dispositions WHERE hit_id=OLD.id))
        BEGIN SELECT RAISE(ABORT, 'hit closure requires a persisted disposition'); END""")
    op.execute("""CREATE TRIGGER hit_initial_state BEFORE INSERT ON hits
        WHEN NEW.status = 'dispositioned' OR (
            NEW.status = 'suppressed' AND NOT EXISTS (
                SELECT 1 FROM suppressions
                WHERE party_id=NEW.party_id AND csl_entry_id=NEW.csl_entry_id
                AND party_hash=NEW.party_hash AND entry_hash=NEW.entry_hash))
        BEGIN SELECT RAISE(ABORT, 'new hit requires open state or exact suppression'); END""")


def downgrade():
    raise RuntimeError("Historical evidence guards cannot be downgraded; restore a reviewed backup")
