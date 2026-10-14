"""Permanent BC company binding and atomic notification receipt uniqueness.

Revision ID: ab735921c641
Revises: 64a12ef079bb
"""

from alembic import op

revision = "ab735921c641"
down_revision = "64a12ef079bb"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE UNIQUE INDEX uq_bc_source_company
        ON sources(json_extract(config, '$.tenant_id'),
            lower(json_extract(config, '$.bc_environment')), json_extract(config, '$.company_id'))
        WHERE kind='bc' AND json_type(config, '$.company_id')='text'
            AND length(json_extract(config, '$.company_id')) > 0""")
    op.execute("""CREATE UNIQUE INDEX uq_bc_webhook_receipt
        ON audit_log(json_extract("after", '$.event_key'))
        WHERE action='bc.webhook_received'""")
    op.execute("""CREATE TRIGGER bc_source_frozen_identity BEFORE UPDATE ON sources
        WHEN OLD.kind='bc' AND json_type(OLD.config, '$.company_id')='text'
            AND length(json_extract(OLD.config, '$.company_id')) > 0
            AND (NEW.id IS NOT OLD.id OR NEW.kind IS NOT OLD.kind OR
                json_extract(NEW.config, '$.tenant_id') IS NOT
                    json_extract(OLD.config, '$.tenant_id') OR
                json_extract(NEW.config, '$.bc_environment') IS NOT
                    json_extract(OLD.config, '$.bc_environment') OR
                json_extract(NEW.config, '$.company_id') IS NOT
                    json_extract(OLD.config, '$.company_id'))
        BEGIN SELECT RAISE(ABORT, 'Bound BC source identity is permanent'); END""")
    op.execute("""CREATE TRIGGER bc_source_no_delete BEFORE DELETE ON sources
        WHEN OLD.kind='bc' AND json_type(OLD.config, '$.company_id')='text'
            AND length(json_extract(OLD.config, '$.company_id')) > 0
        BEGIN SELECT RAISE(ABORT, 'Bound BC source identity is permanent'); END""")
    op.execute("""CREATE TRIGGER bc_source_no_replace BEFORE INSERT ON sources
        WHEN EXISTS (SELECT 1 FROM sources AS bound WHERE bound.kind='bc'
            AND json_type(bound.config, '$.company_id')='text'
            AND length(json_extract(bound.config, '$.company_id')) > 0
            AND (bound.id=NEW.id OR (NEW.kind='bc' AND
                json_extract(bound.config, '$.tenant_id') IS
                    json_extract(NEW.config, '$.tenant_id') AND
                lower(json_extract(bound.config, '$.bc_environment')) IS
                    lower(json_extract(NEW.config, '$.bc_environment')) AND
                json_extract(bound.config, '$.company_id') IS
                    json_extract(NEW.config, '$.company_id'))))
        BEGIN SELECT RAISE(ABORT, 'Bound BC source identity cannot be replaced'); END""")


def downgrade():
    raise RuntimeError("Refusing to erase permanent BC source and webhook provenance")
