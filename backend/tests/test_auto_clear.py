"""Acknowledged, future-only system decisions with immutable comparison evidence."""

import json

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.audit import ImmutableAuditError
from app.csl.service import refresh_csl
from app.models import (
    AuditLog,
    Disposition,
    Hit,
    Party,
    ScreeningRun,
    ScreeningTask,
    Setting,
    Source,
    Suppression,
    User,
)
from app.models.evidence import RunPartyEvidence
from app.reports.metrics import (
    auto_clear_observations,
    current_screening_coverage,
    dashboard_metrics,
)
from app.screening.engine import Adjudication, queue_hits, run_screening, snapshot
from app.screening.normalize import party_record_hash
from app.screening.settings import AUTO_CLEAR_ACK_KEY, ScreeningValidationError, load_settings


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


@pytest.fixture
def policy_db(client, admin):
    with client.app.state.session_factory() as db:
        refresh_csl(
            db,
            data=json.dumps(
                [
                    {
                        "id": "policy-entry",
                        "source": "EL",
                        "name": "Acme Manufacturing",
                        "type": "Entity",
                        "country": "US",
                    }
                ]
            ).encode(),
        )
        source = Source(kind="csv", label="Original synthetic book")
        db.add(source)
        db.flush()
        fields = {"name": "Acme Manufacturing", "party_type": "company", "country_iso2": "US"}
        record = Party(
            source_id=source.id,
            external_id="policy-party",
            **fields,
            record_hash=party_record_hash(fields),
            raw={"name": fields["name"]},
        )
        db.add(record)
        db.commit()
        yield db, record, source


def configure(client, admin, **changes):
    return client.patch(
        "/api/v1/settings", headers={"x-csrf-token": admin["csrf_token"]}, json=changes
    )


def make_review(db):
    db.add(Setting(key="score_weights", value={"country_match": -20}))
    db.commit()


def enable(client, admin, threshold=85):
    result = configure(
        client,
        admin,
        auto_clear_enabled=True,
        auto_clear_threshold=threshold,
        auto_clear_acknowledgment="I understand",
    )
    assert result.status_code == 200, result.text
    return result.json()


def test_typed_enable_acknowledgment_and_read_only_proof(policy_db, client, admin):
    db, _, _ = policy_db
    assert client.get("/api/v1/settings").json()["auto_clear_enabled"] is False
    before = count(db, AuditLog)
    for phrase in (None, "yes", "i understand", "I understand "):
        response = configure(
            client,
            admin,
            auto_clear_enabled=True,
            auto_clear_threshold=85,
            auto_clear_acknowledgment=phrase,
        )
        assert response.status_code == 422
    assert count(db, AuditLog) == before
    values = enable(client, admin)
    db.expire_all()
    reference = values[AUTO_CLEAR_ACK_KEY]
    proof = db.get(AuditLog, reference)
    assert proof.action == "settings.auto_clear_acknowledged"
    assert proof.actor_user_id == admin["user"]["id"]
    assert proof.after["acknowledgment"] == "I understand"
    assert proof.after["actor"]["role"] == "admin" and proof.after["actor"]["active"]
    assert values["auto_clear_acknowledgment_valid"]
    assert db.scalar(select(Setting).where(Setting.key == "auto_clear_acknowledgment")) is None
    assert configure(client, admin, **{AUTO_CLEAR_ACK_KEY: reference}).status_code == 422
    assert configure(client, admin, auto_clear_enabled=False).status_code == 200
    assert configure(client, admin, auto_clear_enabled=True).status_code == 422
    again = enable(client, admin)
    assert again[AUTO_CLEAR_ACK_KEY] != reference
    # The acknowledged actor is historical evidence, not the actor's mutable role.
    db.expire_all()
    db.get(User, admin["user"]["id"]).role = "reviewer"
    db.commit()
    assert load_settings(db).auto_clear_enabled
    assert configure(client, admin, auto_clear_enabled=False).status_code == 403


@pytest.mark.parametrize("threshold", [True, 65.5, 64, 91, "75", None])
def test_slider_type_and_bounds_rollback(policy_db, client, admin, threshold):
    db, _, _ = policy_db
    before = count(db, AuditLog), count(db, Setting), count(db, ScreeningTask)
    response = configure(
        client,
        admin,
        auto_clear_enabled=True,
        auto_clear_threshold=threshold,
        auto_clear_acknowledgment="I understand",
    )
    assert response.status_code == 422
    assert (count(db, AuditLog), count(db, Setting), count(db, ScreeningTask)) == before


def test_scoring_changes_enqueue_full_task_policy_changes_do_not(policy_db, client, admin):
    db, _, _ = policy_db
    before = count(db, ScreeningTask)
    result = configure(
        client,
        admin,
        score_weights={"country_match": -20},
        auto_clear_enabled=True,
        auto_clear_threshold=85,
        auto_clear_acknowledgment="I understand",
    )
    assert result.status_code == 200, result.text
    assert result.json()["pending_screening_task_id"] is not None
    assert count(db, ScreeningTask) == before + 1
    task = db.get(ScreeningTask, result.json()["pending_screening_task_id"])
    assert task.trigger == "manual" and task.party_ids == task.entry_ids == []
    unchanged = configure(client, admin, score_weights={"country_match": -20})
    assert unchanged.status_code == 200 and unchanged.json()["pending_screening_task_id"] is None
    moved = configure(client, admin, auto_clear_threshold=80)
    assert moved.status_code == 200 and moved.json()["pending_screening_task_id"] is None
    assert (
        configure(client, admin, display_name="Renamed tool").json()["pending_screening_task_id"]
        is None
    )
    assert count(db, ScreeningTask) == before + 1
    bad = configure(client, admin, review_threshold=81)
    assert bad.status_code == 422
    joint = configure(client, admin, review_threshold=81, auto_clear_threshold=82)
    assert joint.status_code == 200 and joint.json()["pending_screening_task_id"] is not None
    assert client.get("/api/v1/settings/auto-clear").json()["threshold"] == 82


def test_auto_clear_future_observations_counts_history_and_coverage(policy_db, client, admin):
    db, record, _ = policy_db
    make_review(db)
    original_run = run_screening(db)
    original = queue_hits(db)[0]
    assert original.det_score == 80 and original.bucket == "review"
    before_tasks = count(db, ScreeningTask)
    enabled = enable(client, admin)
    db.expire_all()
    assert count(db, ScreeningTask) == before_tasks
    assert queue_hits(db) == [original] and original.status == "open"
    assert count(db, Disposition) == 0
    assert current_screening_coverage(db)[record.id]  # policy-only changes retain scoring coverage
    db.info["actor_user_id"] = admin["user"]["id"]  # emulate an Admin-requested manual run
    cleared = run_screening(db)
    hit = db.scalar(select(Hit).where(Hit.run_id == cleared.id))
    decision = db.scalar(select(Disposition).where(Disposition.hit_id == hit.id))
    assert cleared.auto_cleared == 1 and cleared.hits_new == 0 and cleared.hits_suppressed == 0
    assert hit.status == "dispositioned" and hit.bucket == "review"
    assert "queue_anchor_hit_id" not in hit.explanation
    assert decision.kind == "auto_cleared" and decision.actor_user_id is None
    assert decision.slider_value_at_time == 85
    assert all(
        value in decision.note
        for value in (cleared.id, "det_score=80", "final_confidence=80", "model=none")
    )
    assert hit.explanation["auto_clear"]["acknowledgment_audit_id"] == enabled[AUTO_CLEAR_ACK_KEY]
    assert original.status == "open" and queue_hits(db) == []
    assert original.explanation["queue_context"] == hit.explanation["queue_context"]
    assert original.explanation["decision_context"] != hit.explanation["decision_context"]
    assert cleared.settings_snapshot["auto_clear_enabled"]
    assert cleared.settings_snapshot["auto_clear_threshold"] == 85
    assert count(db, Suppression) == 0
    events = list(db.scalars(select(AuditLog).where(AuditLog.action == "screening.auto_cleared")))
    assert len(events) == 1 and events[0].actor_user_id is None
    assert db.info["actor_user_id"] == admin["user"]["id"]
    repeat = run_screening(db)
    assert repeat.auto_cleared == 1 and len(auto_clear_observations(db)) == 2
    assert dashboard_metrics(db)["auto_cleared_since_report"] == 2
    disabled = configure(client, admin, auto_clear_enabled=False)
    assert disabled.status_code == 200
    db.expire_all()
    assert queue_hits(db) == []  # changing settings alone never rewrites prior outcomes
    reopened = run_screening(db)
    assert reopened.hits_new == 1 and reopened.auto_cleared == 0
    assert queue_hits(db)[0].explanation["prior_disposition_id"] is not None
    assert original_run.settings_snapshot["auto_clear_enabled"] is False


def test_strict_slider_equality_reuses_unresolved_anchor(policy_db, client, admin):
    db, _, _ = policy_db
    make_review(db)
    run_screening(db)
    original = queue_hits(db)[0]
    enable(client, admin, 80)
    db.expire_all()
    equal = run_screening(db)
    assert equal.auto_cleared == equal.hits_new == 0 and queue_hits(db) == [original]
    assert configure(client, admin, auto_clear_threshold=81).status_code == 200
    db.expire_all()
    below = run_screening(db)
    assert below.auto_cleared == 1 and queue_hits(db) == []


@pytest.mark.parametrize("kind", ["false_positive", "confirmed_match", "escalated"])
def test_human_memory_or_completed_decision_never_auto_clears(policy_db, client, admin, kind):
    db, _, _ = policy_db
    make_review(db)
    run_screening(db)
    hit = queue_hits(db)[0]
    response = client.post(
        f"/api/v1/hits/{hit.id}/dispositions",
        headers={"x-csrf-token": admin["csrf_token"]},
        json={"kind": kind, "note": "Recorded human decision"},
    )
    assert response.status_code == 201
    before = count(db, Disposition)
    enable(client, admin)
    db.expire_all()
    repeat = run_screening(db)
    assert repeat.auto_cleared == repeat.hits_new == 0
    assert count(db, Disposition) == before and queue_hits(db) == []
    assert repeat.hits_suppressed == (1 if kind == "false_positive" else 0)


def test_low_and_deterministic_likely_rail_never_auto_clear(policy_db, client, admin):
    db, _, _ = policy_db
    enable(client, admin)
    db.expire_all()
    likely = run_screening(
        db,
        adjudicate=lambda *args: Adjudication(
            model_prob=0.01, final_confidence=10, model_name="synthetic hook"
        ),
    )
    assert likely.auto_cleared == 0 and queue_hits(db)[0].bucket == "likely"
    db.add(Setting(key="score_weights", value={"country_match": -45}))
    db.commit()
    low = run_screening(db)
    hit = db.scalar(select(Hit).where(Hit.run_id == low.id))
    assert hit.bucket == "low" and hit.status == "open" and low.auto_cleared == 0
    assert count(db, Disposition) == 0 and queue_hits(db) == []


def test_zero_hit_party_evidence_is_original_immutable_and_has_shared_scope(policy_db):
    db, record, source = policy_db
    record.name = "Zyqwv Xhztp"
    record.record_hash = party_record_hash(snapshot(record))
    db.commit()
    run = run_screening(db)
    evidence = db.scalar(select(RunPartyEvidence).where(RunPartyEvidence.run_id == run.id))
    assert evidence.hits_recorded == 0 and all(
        value == 0 for value in evidence.result_counts.values()
    )
    assert evidence.party_snapshot["name"] == "Zyqwv Xhztp"
    assert evidence.source_snapshot["label"] == "Original synthetic book"
    assert "entry_hashes" not in evidence.party_snapshot
    assert run.settings_snapshot["scope"]["entry_hashes"]
    record.name, source.label = "Edited current name", "Edited current source"
    record.record_hash = party_record_hash(snapshot(record))
    db.commit()
    assert evidence.party_snapshot["name"] == "Zyqwv Xhztp"
    assert evidence.source_snapshot["label"] == "Original synthetic book"
    evidence.result_counts = {"low": 99}
    with pytest.raises(ImmutableAuditError):
        db.flush()
    db.rollback()
    for operation in (
        "UPDATE run_party_evidence SET hits_recorded=1 WHERE id=:id",
        "DELETE FROM run_party_evidence WHERE id=:id",
    ):
        with db.get_bind().begin() as connection:
            with pytest.raises(IntegrityError):
                connection.execute(text(operation), {"id": evidence.id})


def test_auto_clear_atomic_failure_and_model_hook_cannot_edit_original_inputs(
    policy_db, client, admin, monkeypatch
):
    db, record, _ = policy_db
    make_review(db)
    enable(client, admin)
    db.expire_all()
    from app.screening import engine

    original_audit = engine.audit

    def audit(db, action, *args, **kwargs):
        if action == "screening.auto_cleared":
            raise RuntimeError("synthetic transaction fault")
        return original_audit(db, action, *args, **kwargs)

    monkeypatch.setattr(engine, "audit", audit)
    with pytest.raises(RuntimeError):
        run_screening(db)
    assert (
        count(db, ScreeningRun)
        == count(db, Hit)
        == count(db, Disposition)
        == count(db, RunPartyEvidence)
        == 0
    )
    monkeypatch.setattr(engine, "audit", original_audit)

    def hook(party, entry, *args):
        party["name"], entry["name"] = "tampered", "tampered"
        return Adjudication(final_confidence=80)

    run = run_screening(db, adjudicate=hook)
    hit = db.scalar(select(Hit).where(Hit.run_id == run.id))
    evidence = db.scalar(select(RunPartyEvidence).where(RunPartyEvidence.run_id == run.id))
    assert run.auto_cleared == 1
    assert hit.party_snapshot["name"] == evidence.party_snapshot["name"] == record.name
    assert hit.entry_snapshot["name"] == "Acme Manufacturing"


def test_enabling_in_storage_without_real_acknowledgment_fails_closed(policy_db):
    db, _, _ = policy_db
    from uuid import uuid4

    db.add_all(
        [
            Setting(key="auto_clear_enabled", value=True),
            Setting(key="auto_clear_threshold", value=85),
            Setting(key=AUTO_CLEAR_ACK_KEY, value=str(uuid4())),
        ]
    )
    db.commit()
    with pytest.raises(ScreeningValidationError, match="recorded Admin acknowledgment"):
        run_screening(db)
    assert (
        count(db, ScreeningRun)
        == count(db, Hit)
        == count(db, Disposition)
        == count(db, RunPartyEvidence)
        == 0
    )
