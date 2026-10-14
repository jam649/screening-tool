import json
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from app.audit import ImmutableAuditError
from app.config import Config
from app.models import (
    AuditLog,
    CSLEntry,
    CSLVersion,
    Disposition,
    Hit,
    Party,
    ScreeningRun,
    Setting,
    Source,
    Suppression,
)
from app.security import validate_key_binding


@pytest.fixture
def hit(client, app, admin):
    with app.state.session_factory() as db:
        source = Source(kind="csv", label="Synthetic source")
        version = CSLVersion(file_sha256="a" * 64, entry_count=1)
        db.add_all([source, version])
        db.flush()
        party = Party(
            source_id=source.id,
            external_id="synthetic-1",
            name="Original name",
            record_hash="b" * 64,
        )
        entry = CSLEntry(
            csl_id="synthetic-1",
            source_list="SDN",
            name="Listed name",
            entry_hash="c" * 64,
            first_seen_version=version.id,
            last_seen_version=version.id,
        )
        run = ScreeningRun(trigger="manual", csl_version_id=version.id)
        db.add_all([party, entry, run])
        db.flush()
        hit = Hit(
            run_id=run.id,
            party_id=party.id,
            csl_entry_id=entry.id,
            party_hash=party.record_hash,
            entry_hash=entry.entry_hash,
            det_score=80,
            model_prob=None,
            final_confidence=80,
            bucket="review",
            explanation={"name": 80},
            party_snapshot={"name": party.name},
            entry_snapshot={"name": entry.name},
        )
        db.add(hit)
        db.commit()
        return hit


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "rewritten"),
        ("run_id", "rewritten"),
        ("party_id", "rewritten"),
        ("csl_entry_id", "rewritten"),
        ("party_hash", "d" * 64),
        ("entry_hash", "d" * 64),
        ("det_score", 50),
        ("model_prob", 0.25),
        ("final_confidence", 40),
        ("bucket", "low"),
        ("explanation", {"name": 1}),
        ("party_snapshot", {"name": "rewritten"}),
        ("entry_snapshot", {"name": "rewritten"}),
    ],
)
def test_every_hit_evidence_field_is_immutable_in_orm_and_sql(app, hit, field, value):
    with app.state.session_factory() as db:
        row = db.get(Hit, hit.id)
        setattr(row, field, value)
        with pytest.raises(ImmutableAuditError, match="Historical hit evidence"):
            db.commit()
        db.rollback()
        sql_value = json.dumps(value) if isinstance(value, dict) else value
        with pytest.raises(IntegrityError, match="hit evidence is immutable"):
            db.execute(
                text(f"UPDATE hits SET {field}=:value WHERE id=:id"),
                {"value": sql_value, "id": hit.id},
            )
        db.rollback()


def test_hit_creation_and_status_changes_preserve_evidence_and_audit(app, hit, admin):
    with app.state.session_factory() as db:
        db.info["actor_user_id"] = admin["user"]["id"]
        row = db.get(Hit, hit.id)
        db.add(Disposition(hit_id=row.id, kind="false_positive", actor_user_id=admin["user"]["id"]))
        db.flush()
        row.status = "dispositioned"
        db.commit()
        changes = db.scalars(select(AuditLog).where(AuditLog.target_id == hit.id)).all()
        assert [change.action for change in changes] == ["hits.create", "hits.update"]
        assert changes[0].after["status"] == "open"
        assert changes[0].after["created_at"] == row.created_at.isoformat()
        assert changes[1].before["status"] == "open"
        assert changes[1].after["status"] == "dispositioned"
        assert changes[1].actor_user_id == admin["user"]["id"]
        for statement in (
            "DELETE FROM hits",
            "INSERT OR REPLACE INTO hits SELECT * FROM hits LIMIT 1",
        ):
            with pytest.raises(IntegrityError, match="hit evidence is immutable|new hit requires"):
                db.execute(text(statement))
            db.rollback()


def test_disposition_corrections_are_new_rows_and_suppressions_audited(app, hit, admin):
    with app.state.session_factory() as db:
        decision = Disposition(
            hit_id=hit.id, kind="false_positive", actor_user_id=admin["user"]["id"]
        )
        db.add(decision)
        db.flush()
        suppression = Suppression(
            party_id=hit.party_id,
            csl_entry_id=hit.csl_entry_id,
            party_hash=hit.party_hash,
            entry_hash=hit.entry_hash,
            disposition_id=decision.id,
        )
        db.add(suppression)
        db.commit()
        decision.note = "rewritten"
        with pytest.raises(ImmutableAuditError, match="Disposition"):
            db.commit()
        db.rollback()
        with pytest.raises(ImmutableAuditError):
            db.execute(update(Disposition).values(note="rewritten"))
        db.rollback()
        for statement in (
            "UPDATE dispositions SET note='rewritten'",
            "DELETE FROM dispositions",
            "INSERT OR REPLACE INTO dispositions SELECT * FROM dispositions LIMIT 1",
        ):
            with pytest.raises(IntegrityError, match="dispositions are insert-only"):
                db.execute(text(statement))
            db.rollback()
        db.delete(suppression)
        db.commit()
        actions = db.scalars(select(AuditLog).where(AuditLog.target_id == suppression.id)).all()
        assert [row.action for row in actions] == ["suppressions.create", "suppressions.delete"]


@pytest.mark.parametrize(
    "kind,actor,note",
    [
        ("false_positive", None, ""),
        ("confirmed_match", "admin", " "),
        ("escalated", "admin", ""),
        ("escalated", "admin", "\t\n\u2003"),
        ("auto_cleared", "admin", "system"),
    ],
)
def test_dispositions_require_correct_actor_and_note_orm_and_sql(
    app, hit, admin, kind, actor, note
):
    actor_id = admin["user"]["id"] if actor else None
    with app.state.session_factory() as db:
        db.add(Disposition(hit_id=hit.id, kind=kind, actor_user_id=actor_id, note=note))
        with pytest.raises(ValueError):
            db.commit()
        db.rollback()
        with pytest.raises(IntegrityError, match="requires an actor|system-only|require a note"):
            db.execute(
                text("""INSERT INTO dispositions
                (id, created_at, hit_id, kind, actor_user_id, note)
                VALUES ('invalid', CURRENT_TIMESTAMP, :hit, :kind, :actor, :note)"""),
                {"hit": hit.id, "kind": kind, "actor": actor_id, "note": note},
            )
        db.rollback()


def test_audit_defaults_secret_redaction_and_real_party_changes(app, hit):
    with app.state.session_factory() as db:
        setting = Setting(key="smtp_password", value="synthetic-private-value")
        source = Source(
            kind="csv",
            label="Secrets test",
            credentials_enc="encrypted-private",
            config={
                "nested": [
                    {
                        "client_secret": "synthetic-client-secret",
                        "api-key": "synthetic-api-key",
                        "label": "kept",
                    }
                ]
            },
        )
        public = Setting(key="review_threshold", value=65)
        db.add_all([setting, source, public])
        db.commit()
        rows = db.scalars(
            select(AuditLog).where(AuditLog.target_id.in_([setting.id, source.id, public.id]))
        ).all()
        serialized = json.dumps([row.after for row in rows])
        assert "synthetic-private" not in serialized
        assert "synthetic-client-secret" not in serialized
        assert "synthetic-api-key" not in serialized
        setting_snapshot = next(row.after for row in rows if row.target_id == setting.id)
        assert setting_snapshot["encrypted"] is False
        assert setting_snapshot["updated_at"] == setting.updated_at.isoformat()
        source_snapshot = next(row.after for row in rows if row.target_id == source.id)
        assert source_snapshot["record_count"] == 0
        assert source_snapshot["config"]["nested"][0]["label"] == "kept"
        assert next(row.after for row in rows if row.target_id == public.id)["value"] == 65
        old_updated_at = public.updated_at.isoformat()
        public.value = 70
        db.commit()
        update_snapshot = db.scalar(
            select(AuditLog).where(
                AuditLog.target_id == public.id, AuditLog.action == "settings.update"
            )
        )
        assert update_snapshot.before["updated_at"] == old_updated_at
        assert update_snapshot.after["updated_at"] == public.updated_at.isoformat()
        party = db.get(Party, hit.party_id)
        party.name = "New name"
        db.commit()
        change = db.scalar(select(AuditLog).where(AuditLog.action == "parties.update"))
        assert change.before["name"] == "Original name"
        assert change.after["name"] == "New name"
        assert db.get(Hit, hit.id).party_snapshot == {"name": "Original name"}


def test_changed_encryption_key_fails_binding_without_secret_in_error(client, app, config):
    with app.state.session_factory() as db:
        validate_key_binding(db, config)
        validate_key_binding(db, config)
        other_key = Fernet.generate_key().decode()
        wrong_config = Config(secret_key=other_key)
        with pytest.raises(RuntimeError, match="does not match") as caught:
            validate_key_binding(db, wrong_config)
        assert other_key not in str(caught.value)
        canaries = db.scalars(select(Setting).where(Setting.key == "__encryption_key_canary")).all()
        assert len(canaries) == 1
        snapshots = db.scalars(select(AuditLog).where(AuditLog.target_id == canaries[0].id)).all()
        assert snapshots[0].after["value"] == "[redacted]"


def test_baseline_downgrade_refuses_to_destroy_audit():
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/caa3a3b7e05a_baseline_schema_and_immutable_audit_.py"
    )
    spec = spec_from_file_location("baseline", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(RuntimeError, match="destroy audit evidence"):
        module.downgrade()


@pytest.mark.parametrize("status", ["suppressed", "dispositioned"])
def test_hit_status_cannot_invent_a_decision_orm_or_sql(app, hit, status):
    with app.state.session_factory() as db:
        row = db.get(Hit, hit.id)
        row.status = status
        with pytest.raises(ImmutableAuditError):
            db.commit()
        db.rollback()
        with pytest.raises(IntegrityError, match="persisted disposition"):
            db.execute(
                text("UPDATE hits SET status=:status WHERE id=:id"),
                {"status": status, "id": hit.id},
            )
        db.rollback()


def test_closed_hit_cannot_reopen_after_persisted_decision(app, hit, admin):
    with app.state.session_factory() as db:
        row = db.get(Hit, hit.id)
        db.add(
            Disposition(
                hit_id=row.id,
                kind="confirmed_match",
                actor_user_id=admin["user"]["id"],
                note="Documented human finding",
            )
        )
        db.flush()
        row.status = "dispositioned"
        db.commit()
        row.status = "open"
        with pytest.raises(ImmutableAuditError, match="only closes"):
            db.commit()
        db.rollback()
        with pytest.raises(IntegrityError, match="persisted disposition"):
            db.execute(text("UPDATE hits SET status='open' WHERE id=:id"), {"id": hit.id})
        db.rollback()
        assert db.get(Hit, hit.id).status == "dispositioned"


@pytest.mark.parametrize(
    "field,value",
    [
        ("csl_version_id", "rewritten"),
        ("settings_snapshot", {"hash_rule_version": 99}),
        ("trigger", "nightly"),
        ("parties_screened", 99),
        ("model_fallbacks", [{"error": "rewritten"}]),
        ("finished_at", None),
    ],
)
def test_completed_run_provenance_cannot_be_rewritten(app, hit, field, value):
    from app.models import utcnow

    with app.state.session_factory() as db:
        run = db.get(ScreeningRun, hit.run_id)
        run.finished_at = utcnow()
        db.commit()
        setattr(run, field, value)
        with pytest.raises(ImmutableAuditError, match="Completed screening run"):
            db.commit()
        db.rollback()
        sql_value = json.dumps(value) if isinstance(value, (dict, list)) else value
        with pytest.raises(IntegrityError, match="completed screening run"):
            db.execute(
                text(f"UPDATE screening_runs SET {field}=:value WHERE id=:id"),
                {"value": sql_value, "id": hit.run_id},
            )
        db.rollback()


def test_zero_hit_run_cannot_be_deleted_or_replaced(app, hit):
    with app.state.session_factory() as db:
        run = ScreeningRun(
            trigger="manual",
            csl_version_id=hit.entry_snapshot.get("version_id")
            or db.get(ScreeningRun, hit.run_id).csl_version_id,
        )
        db.add(run)
        db.commit()
        db.delete(run)
        with pytest.raises(ImmutableAuditError, match="cannot be deleted"):
            db.commit()
        db.rollback()
        for statement in (
            "DELETE FROM screening_runs WHERE id=:id",
            "INSERT OR REPLACE INTO screening_runs SELECT * FROM screening_runs WHERE id=:id",
        ):
            with pytest.raises(IntegrityError, match="screening run provenance"):
                db.execute(text(statement), {"id": run.id})
            db.rollback()


def test_new_run_can_complete_once_when_nullable_finish_attribute_was_unset(app, hit):
    from app.models import utcnow

    with app.state.session_factory() as db:
        original = db.get(ScreeningRun, hit.run_id)
        run = ScreeningRun(trigger="manual", csl_version_id=original.csl_version_id)
        db.add(run)
        db.flush()
        run.parties_screened = 2
        run.settings_snapshot = {"scope": {"party_ids": [], "entry_ids": []}}
        run.finished_at = utcnow()
        db.commit()
        assert run.finished_at is not None
        run.parties_screened = 3
        with pytest.raises(ImmutableAuditError, match="Completed screening run"):
            db.commit()
        db.rollback()


def test_batched_source_audit_preserves_every_original_row_and_mixed_changes(app, admin):
    with app.state.session_factory() as db:
        db.info["actor_user_id"] = admin["user"]["id"]
        sources = [
            Source(
                kind="csv",
                label=f"Synthetic batch {number}",
                config={
                    "nested": {"API-Key": "synthetic-private", "values": [number, False, None]}
                },
            )
            for number in range(30)
        ]
        db.add_all(sources)
        db.flush()
        originals = {
            row.target_id: row
            for row in db.scalars(
                select(AuditLog).where(
                    AuditLog.target_id.in_([source.id for source in sources]),
                    AuditLog.action == "sources.create",
                )
            )
        }
        assert len(originals) == 30
        for number, source in enumerate(sources):
            original = originals[source.id]
            assert original.actor_user_id == admin["user"]["id"]
            assert original.after["label"] == f"Synthetic batch {number}"
            assert original.after["config"] == {
                "nested": {"API-Key": "[redacted]", "values": [number, False, None]}
            }
            assert original.after["created_at"] == source.created_at.isoformat()
        changed, removed = sources[:2]
        removed_id = removed.id
        changed.label = "Synthetic renamed batch"
        db.delete(removed)
        added = Source(kind="csv", label="Synthetic added batch")
        db.add(added)
        db.commit()
        assert originals[changed.id].after["label"] == "Synthetic batch 0"
        assert originals[removed_id].after["label"] == "Synthetic batch 1"
        changes = list(
            db.scalars(
                select(AuditLog).where(AuditLog.target_id.in_([changed.id, removed_id, added.id]))
            )
        )
        assert len(changes) == 5
        updated = next(row for row in changes if row.action == "sources.update")
        deleted = next(row for row in changes if row.action == "sources.delete")
        assert updated.before["label"] == "Synthetic batch 0"
        assert updated.after["label"] == "Synthetic renamed batch"
        assert deleted.before["label"] == "Synthetic batch 1" and deleted.after is None
        assert all(row.actor_user_id == admin["user"]["id"] for row in changes)


def test_recursive_redaction_preserves_typed_measurements_and_independent_values():
    from datetime import UTC, datetime

    from app.audit import redact_credentials

    timestamp = datetime(2026, 10, 14, tzinfo=UTC)
    original = {
        "nested": [
            {"CLIENT-State": "synthetic-private", "input_tokens": 12, "output_tokens": None},
            {"input_tokens": "12", "token_set": 0.5, "token_sort": False},
            {"ordinary": [0, False, None, "", timestamp]},
        ]
    }
    retained = redact_credentials(original)
    assert retained == {
        "nested": [
            {"CLIENT-State": "[redacted]", "input_tokens": 12, "output_tokens": None},
            {"input_tokens": "[redacted]", "token_set": 0.5, "token_sort": "[redacted]"},
            {"ordinary": [0, False, None, "", timestamp.isoformat()]},
        ]
    }
    original["nested"][2]["ordinary"].append("later mutation")
    original["nested"][0]["input_tokens"] = 99
    assert retained["nested"][2]["ordinary"] == [0, False, None, "", timestamp.isoformat()]
    assert retained["nested"][0]["input_tokens"] == 12
