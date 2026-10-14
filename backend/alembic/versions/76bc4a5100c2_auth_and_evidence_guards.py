"""Protect all historical hit fields and enforce attributed, immutable decisions.

Revision ID: 76bc4a5100c2
Revises: 32dfcc79a042
"""

from alembic import op

revision = "76bc4a5100c2"
down_revision = "32dfcc79a042"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DROP TRIGGER hit_evidence_no_update")
    columns = [
        "id",
        "created_at",
        "run_id",
        "party_id",
        "party_hash",
        "csl_entry_id",
        "entry_hash",
        "det_score",
        "model_prob",
        "final_confidence",
        "bucket",
        "explanation",
        "party_snapshot",
        "entry_snapshot",
    ]
    changes = " OR ".join(f"NEW.{column} IS NOT OLD.{column}" for column in columns)
    op.execute(f"""CREATE TRIGGER hit_evidence_no_update BEFORE UPDATE ON hits
        WHEN {changes}
        BEGIN SELECT RAISE(ABORT, 'hit evidence is immutable'); END""")
    op.execute("""CREATE TRIGGER hits_no_delete BEFORE DELETE ON hits
        BEGIN SELECT RAISE(ABORT, 'hit evidence is immutable'); END""")
    op.execute("""CREATE TRIGGER hits_no_replace BEFORE INSERT ON hits
        WHEN EXISTS (SELECT 1 FROM hits WHERE id=NEW.id
            OR (run_id=NEW.run_id AND party_id=NEW.party_id AND csl_entry_id=NEW.csl_entry_id))
        BEGIN SELECT RAISE(ABORT, 'hit evidence is immutable'); END""")
    for operation in ("UPDATE", "DELETE"):
        op.execute(f"""CREATE TRIGGER dispositions_no_{operation.lower()}
            BEFORE {operation} ON dispositions
            BEGIN SELECT RAISE(ABORT, 'dispositions are insert-only'); END""")
    op.execute("""CREATE TRIGGER dispositions_no_replace BEFORE INSERT ON dispositions
        WHEN EXISTS (SELECT 1 FROM dispositions WHERE id=NEW.id)
        BEGIN SELECT RAISE(ABORT, 'dispositions are insert-only'); END""")
    op.execute("""CREATE TRIGGER dispositions_valid_actor BEFORE INSERT ON dispositions
        WHEN (NEW.kind='auto_cleared') != (NEW.actor_user_id IS NULL)
        BEGIN SELECT RAISE(ABORT,
            'human disposition requires an actor; auto-clear is system-only'); END""")
    # Match Python's strip() whitespace handling for raw SQL writes too.
    whitespace = " || ".join(
        f"char({code})"
        for code in (
            9,
            10,
            11,
            12,
            13,
            28,
            29,
            30,
            31,
            32,
            133,
            160,
            5760,
            *range(8192, 8203),
            8232,
            8233,
            8239,
            8287,
            12288,
        )
    )
    op.execute(f"""CREATE TRIGGER dispositions_valid_note BEFORE INSERT ON dispositions
        WHEN NEW.kind IN ('confirmed_match','escalated')
            AND length(trim(NEW.note, {whitespace}))=0
        BEGIN SELECT RAISE(ABORT, 'confirmed matches and escalations require a note'); END""")
    # Legacy rows mixed successful/failed attempts and used guessable SHA256 identifiers.
    # They cannot participate in the new keyed, failure-only limiter.
    op.execute("DELETE FROM login_attempts")


def downgrade():
    raise RuntimeError("Evidence protections cannot be downgraded; restore a reviewed backup")
