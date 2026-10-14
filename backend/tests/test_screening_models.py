"""Real SQLite writer boundaries and immutable optional-model comparisons."""

import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select, text

from app.adjudicators.contracts import ModelConfig
from app.adjudicators.settings import save_model_config
from app.csl.service import refresh_csl
from app.models import (
    AuditLog,
    Hit,
    OperationLease,
    Party,
    ScreeningRun,
    ScreeningTask,
    Setting,
    Source,
    User,
    UserSession,
    utcnow,
)
from app.models.evidence import RunPartyEvidence
from app.reports.metrics import current_screening_coverage
from app.scheduler.leases import (
    OperationAuthorityError,
    OperationLeaseLostError,
    protected_operation,
)
from app.screening.engine import (
    Adjudication,
    ScreeningTasksFailedError,
    process_pending_tasks,
    queue_hits,
    run_screening,
)
from app.screening.normalize import party_record_hash
from app.screening.settings import ScreeningValidationError


@pytest.fixture
def book(client, admin):
    factory = client.app.state.session_factory
    with factory() as db:
        refreshed = refresh_csl(db, data=json.dumps([listing()]).encode())
        source = Source(kind="csv", label="Original synthetic source")
        db.add(source)
        db.flush()
        values = {"name": "Acme Manufacturing", "party_type": "company", "country_iso2": "US"}
        party = Party(
            source_id=source.id,
            external_id="model-case",
            **values,
            record_hash=party_record_hash(values),
            raw={"public_fixture": True},
        )
        db.add(party)
        db.commit()
        yield db, party, refreshed, factory


def listing(**changes):
    return {
        "id": "model-case",
        "source": "Entity List (EL) - Bureau of Industry and Security",
        "name": "Acme Manufacturing",
        "type": "Entity",
        "alt_names": [],
        "addresses": [{"country": "US"}],
        "programs": [],
        **changes,
    }


def hits_for(db, run):
    return list(db.scalars(select(Hit).where(Hit.run_id == run.id)))


def total(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_hook_has_no_writer_and_blend_ignores_adapter_confidence(book):
    db, party, _, factory = book
    metadata = {
        "provider": "laya",
        "input_tokens": 123,
        "attempts": 1,
        "budget": {"removed": ["remarks"]},
    }

    def hook(p, e, score, explanation, settings):
        assert not db.in_transaction()
        with factory() as other:
            other.execute(text("BEGIN IMMEDIATE"))
            other.rollback()
        assert p["record_hash"] == party.record_hash and score == 100
        return Adjudication(
            model_prob=0.01, final_confidence=99, model_name="fixture-model", metadata=metadata
        )

    policy = ModelConfig(provider="laya", w_det=0.4, w_model=0.6)
    run = run_screening(db, adjudicate=hook, model_config=policy)
    hit = hits_for(db, run)[0]
    assert (hit.det_score, hit.model_prob, hit.final_confidence, hit.bucket) == (
        100,
        0.01,
        41,
        "likely",
    )
    assert hit.explanation["adjudication"] == metadata
    metadata["budget"]["removed"].append("addresses")
    db.expire_all()
    assert hits_for(db, run)[0].explanation["adjudication"]["budget"]["removed"] == ["remarks"]
    assert run.settings_snapshot["model"] == policy.scoring_snapshot()
    assert run.settings_snapshot["adjudication_summary"] == {
        "calls": 1,
        "superseded": 0,
        "fallbacks": 0,
    }


def test_null_probability_keeps_deterministic_score_and_records_fallback(book):
    db, _, _, _ = book
    run = run_screening(
        db,
        adjudicate=lambda *args: Adjudication(
            final_confidence=0,
            model_name="fixture-model",
            fallback={"error_code": "unreachable"},
            metadata={"attempts": 2, "input_tokens": None, "error_code": "unreachable"},
        ),
        model_config=ModelConfig(provider="laya", w_det=0, w_model=1),
    )
    hit = hits_for(db, run)[0]
    assert hit.model_prob is None and hit.final_confidence == hit.det_score == 100
    assert run.model_fallbacks[0]["error_code"] == "unreachable"
    assert run.settings_snapshot["adjudication_summary"]["fallbacks"] == 1
    event = db.scalar(select(AuditLog).where(AuditLog.action == "screening.model_fallback"))
    assert event.target_id == run.id and event.after["party_id"] == hit.party_id


@pytest.mark.parametrize("probability", [float("nan"), float("inf"), True, -0.01, 1.01])
def test_invalid_probability_never_inserts_partial_evidence(book, probability):
    db, _, _, _ = book
    with pytest.raises(ScreeningValidationError, match="invalid confidence"):
        run_screening(db, adjudicate=lambda *args: Adjudication(model_prob=probability))
    assert total(db, ScreeningRun) == total(db, Hit) == total(db, RunPartyEvidence) == 0


def test_source_changes_during_inference_keep_original_evidence_and_unproven_current_coverage(book):
    db, party, refreshed, factory = book
    original_hash, party_id = party.record_hash, party.id

    def hook(*args):
        with factory() as other:
            changed = other.get(Party, party_id)
            changed.name, changed.party_type = "Changed individual record", "individual"
            changed.record_hash = party_record_hash(
                {
                    "name": changed.name,
                    "party_type": changed.party_type,
                    "country_iso2": changed.country_iso2,
                }
            )
            other.get(Source, changed.source_id).label = "Changed source label"
            other.commit()
        return Adjudication(model_prob=0.5, metadata={"attempts": 1})

    run = run_screening(db, adjudicate=hook)
    hit = hits_for(db, run)[0]
    evidence = db.scalar(select(RunPartyEvidence).where(RunPartyEvidence.run_id == run.id))
    assert hit.party_hash == evidence.party_hash == original_hash
    assert hit.party_snapshot["name"] == evidence.party_snapshot["name"] == "Acme Manufacturing"
    assert hit.party_snapshot["party_type"] == "company"
    assert evidence.source_snapshot["label"] == "Original synthetic source"
    assert run.csl_version_id == refreshed.version.id
    db.expire_all()
    assert db.get(Party, party_id).record_hash != original_hash
    assert not current_screening_coverage(db)[party_id] and queue_hits(db) == []


def test_list_change_during_inference_keeps_original_entry_and_version(book):
    db, _, original, factory = book

    def hook(*args):
        with factory() as other:
            refresh_csl(other, data=json.dumps([listing(name="Changed listing")]).encode())
        return Adjudication(model_prob=0.8)

    run = run_screening(db, adjudicate=hook)
    hit = hits_for(db, run)[0]
    assert run.csl_version_id == original.version.id
    assert hit.entry_snapshot["name"] == "Acme Manufacturing"
    assert run.settings_snapshot["scope"]["entry_hashes"][hit.csl_entry_id] == hit.entry_hash
    assert queue_hits(db) == []


@pytest.mark.parametrize("policy", ["model", "screening"])
def test_policy_change_during_inference_aborts_complete_batch(book, policy):
    db, _, _, factory = book

    def hook(*args):
        with factory() as other:
            if policy == "model":
                save_model_config(other, other.info["config"], ModelConfig(generation=1))
            else:
                other.add(Setting(key="review_threshold", value=66))
            other.commit()
        return Adjudication(model_prob=0.5)

    with pytest.raises(ScreeningValidationError, match="policy changed"):
        run_screening(db, adjudicate=hook)
    assert total(db, ScreeningRun) == total(db, Hit) == total(db, RunPartyEvidence) == 0


def test_admin_offboarding_during_inference_prevents_business_writes(book, admin):
    db, _, _, factory = book
    actor = db.scalar(select(User).where(User.email == "admin@example.com"))
    actor_id = actor.id
    db.info["actor_user_id"] = actor_id

    def hook(*args):
        with factory() as other:
            other.get(User, actor_id).active = False
            other.commit()
        return Adjudication(model_prob=0.5)

    with pytest.raises(OperationAuthorityError):
        run_screening(db, adjudicate=hook)
    assert total(db, ScreeningRun) == total(db, Hit) == 0


@pytest.mark.parametrize("kind", ["false_positive", "confirmed_match", "escalated"])
def test_new_human_decision_during_inference_supersedes_model_and_later_bypasses(
    book, client, admin, kind
):
    db, _, _, _ = book
    run_screening(db, adjudicate=lambda *args: Adjudication())
    original = queue_hits(db)[0]
    calls = 0

    def hook(*args):
        nonlocal calls
        calls += 1
        response = client.post(
            f"/api/v1/hits/{original.id}/dispositions",
            headers={"x-csrf-token": admin["csrf_token"]},
            json={"kind": kind, "note": "Actual human during model call"},
        )
        assert response.status_code == 201
        return Adjudication(model_prob=0.01, metadata={"attempts": 1, "input_tokens": 99})

    run = run_screening(db, adjudicate=hook, model_config=ModelConfig(w_det=0, w_model=1))
    hit = hits_for(db, run)[0]
    assert hit.model_prob is None and hit.final_confidence == hit.det_score
    assert hit.explanation["adjudication_superseded_by_human"]
    assert hit.explanation["adjudication"]["input_tokens"] == 99
    assert queue_hits(db) == [] and run.hits_new == 0
    assert run.settings_snapshot["adjudication_summary"]["superseded"] == 1
    repeat = run_screening(db, adjudicate=hook)
    assert calls == 1 and repeat.hits_new == 0


def test_task_inference_failure_is_atomic_and_retry_uses_same_scoped_task(book):
    db, party, _, factory = book
    task_id = db.scalar(select(ScreeningTask.id))

    def failed(*args):
        with factory() as other:
            other.execute(text("BEGIN IMMEDIATE"))
            assert other.get(ScreeningTask, task_id).status == "pending"
            other.rollback()
        raise RuntimeError("provider exception must not become public evidence")

    with pytest.raises(ScreeningTasksFailedError) as failure:
        process_pending_tasks(db, adjudicate=failed)
    assert "provider exception" not in str(failure.value.failures)
    assert total(db, ScreeningRun) == total(db, Hit) == total(db, RunPartyEvidence) == 0
    assert db.get(ScreeningTask, task_id).status == "failed"
    runs = process_pending_tasks(db, adjudicate=lambda *args: Adjudication(model_prob=0.9))
    assert len(runs) == 1 and runs[0].parties_screened == 1
    assert db.get(ScreeningTask, task_id).status == "complete"
    assert db.get(ScreeningTask, task_id).attempts == 2
    assert hits_for(db, runs[0])[0].party_id == party.id


@pytest.mark.parametrize("entrypoint", ["run", "task"])
def test_actual_operation_fence_replacement_during_inference_prevents_evidence(book, entrypoint):
    db, _, _, factory = book
    with pytest.raises(OperationLeaseLostError):
        with protected_operation(factory, "screen_all") as execution:
            db.info["operation_execution"] = execution

            def hook(*args):
                with factory() as other:
                    other.execute(text("BEGIN IMMEDIATE"))
                    lease = other.scalar(
                        select(OperationLease).where(OperationLease.key == execution.key)
                    )
                    lease.fence += 1
                    other.commit()
                return Adjudication(model_prob=0.9)

            if entrypoint == "run":
                run_screening(db, adjudicate=hook)
            else:
                process_pending_tasks(db, adjudicate=hook)
    db.info.pop("operation_execution")
    assert total(db, ScreeningRun) == total(db, Hit) == total(db, RunPartyEvidence) == 0
    task = db.scalar(select(ScreeningTask))
    assert task.status == "pending" and task.attempts == 0


def test_model_configuration_change_invalidates_coverage_and_creates_new_queue_context(book):
    db, party, _, _ = book
    initial = run_screening(db, adjudicate=lambda *args: Adjudication(model_prob=0.5))
    initial_anchor = hits_for(db, initial)[0]
    assert current_screening_coverage(db)[party.id]
    policy = ModelConfig(provider="laya", generation=1, w_det=0.4, w_model=0.6)
    save_model_config(db, db.info["config"], policy)
    db.commit()
    assert not current_screening_coverage(db)[party.id]
    changed = run_screening(db, adjudicate=lambda *args: Adjudication(model_prob=0.1))
    new_anchor = hits_for(db, changed)[0]
    assert new_anchor.final_confidence == 46 and new_anchor.bucket == "likely"
    assert new_anchor.explanation["queue_context"] != initial_anchor.explanation["queue_context"]
    assert "queue_anchor_hit_id" not in new_anchor.explanation and changed.hits_new == 1
    assert queue_hits(db) == [new_anchor] and current_screening_coverage(db)[party.id]


def test_below_eligibility_cutoff_and_zero_hit_participation_do_not_invoke_model(book):
    db, party, _, _ = book
    party.country_iso2 = "CA"
    party.record_hash = party_record_hash(
        {"name": party.name, "party_type": party.party_type, "country_iso2": party.country_iso2}
    )
    db.add_all(
        [
            Setting(key="review_threshold", value=96),
            Setting(key="likely_threshold", value=99),
            Setting(key="auto_clear_threshold", value=96),
        ]
    )
    db.commit()

    def forbidden(*args):
        raise AssertionError("Model cannot adjudicate a comparison below its cutoff")

    low = run_screening(db, adjudicate=forbidden)
    hit = hits_for(db, low)[0]
    assert hit.det_score == 85 and hit.model_prob is None and hit.bucket == "low"
    party.name = "Zzz unrelated synthetic record"
    party.record_hash = party_record_hash(
        {"name": party.name, "party_type": party.party_type, "country_iso2": party.country_iso2}
    )
    db.commit()
    empty = run_screening(db, adjudicate=forbidden)
    assert hits_for(db, empty) == [] and empty.parties_screened == 1
    evidence = db.scalar(select(RunPartyEvidence).where(RunPartyEvidence.run_id == empty.id))
    assert evidence.hits_recorded == 0 and evidence.party_hash == party.record_hash


@pytest.mark.parametrize("entrypoint", ["run", "task"])
def test_production_hook_loader_adjudicates_both_entrypoints_without_writer(book, entrypoint):
    from app.adjudicators.budget import local_state_room, local_tokens

    db, _, _, factory = book
    policy = ModelConfig(provider="laya", generation=1, w_det=0.4, w_model=0.6)
    save_model_config(db, db.info["config"], policy)
    db.commit()
    requests = []

    def vendor(request):
        with factory() as other:
            other.execute(text("BEGIN IMMEDIATE"))
            other.rollback()
        payload = json.loads(request.content)
        assert request.url == "http://127.0.0.1:8009/v1/systemone"
        assert payload["questions"]["same_party"]["type"] == "noul"
        count = local_tokens(payload["state"]) + (1024 - local_state_room())
        requests.append(count)
        return httpx.Response(
            200,
            json={
                "model": "multilingual",
                "answers": {"same_party": {"type": "noul", "noul": 0.01}},
                "usage": {"input_tokens": count, "output_tokens": 0},
            },
        )

    db.info["model_transport_loader"] = lambda: httpx.MockTransport(vendor)
    run = run_screening(db) if entrypoint == "run" else process_pending_tasks(db)[0]
    hit = hits_for(db, run)[0]
    assert requests and hit.model_prob == 0.01 and hit.final_confidence == 41
    assert hit.bucket == "likely" and hit.explanation["adjudication"]["input_tokens"] == requests[0]
    assert run.settings_snapshot["model"] == policy.scoring_snapshot()
    assert run.settings_snapshot["adjudication_summary"]["calls"] == 1


@pytest.mark.parametrize("entrypoint", ["run", "task"])
def test_screening_rejects_uncommitted_caller_changes_without_flushing_or_discarding(
    book, entrypoint
):
    db, party, _, factory = book
    party_id = party.id
    party.name = "Pending caller edit"
    with pytest.raises(ScreeningValidationError, match="Commit pending changes"):
        if entrypoint == "run":
            run_screening(db)
        else:
            process_pending_tasks(db)
    assert party.name == "Pending caller edit" and party in db.dirty
    with factory() as other:
        assert other.get(Party, party_id).name == "Acme Manufacturing"
        assert total(other, ScreeningRun) == 0
    db.rollback()


@pytest.mark.parametrize("revocation", ["logout", "expiry"])
def test_manual_session_revocation_during_inference_prevents_apply(book, revocation):
    db, _, _, factory = book
    actor_id = db.scalar(select(User.id).where(User.email == "admin@example.com"))
    session_id = db.scalar(select(UserSession.id).where(UserSession.user_id == actor_id))
    assert session_id is not None
    db.info.update(actor_user_id=actor_id, auth_session_id=session_id)

    def hook(*args):
        with factory() as other:
            session = other.get(UserSession, session_id)
            if revocation == "logout":
                other.delete(session)
            else:
                session.expires_at = utcnow() - timedelta(seconds=1)
            other.commit()
        return Adjudication(model_prob=0.9)

    with pytest.raises(OperationAuthorityError, match="session required"):
        run_screening(db, adjudicate=hook)
    assert total(db, ScreeningRun) == total(db, Hit) == total(db, RunPartyEvidence) == 0
    assert db.get(User, actor_id).active


def test_policy_revocation_after_first_pair_stops_later_identity_transmission(book):
    db, party, _, factory = book
    db.add(
        Party(
            source_id=party.source_id,
            external_id="second-model-case",
            name=party.name,
            party_type=party.party_type,
            country_iso2=party.country_iso2,
            record_hash=party.record_hash,
            raw={"public_fixture": True},
        )
    )
    db.commit()
    calls = []

    def hook(party_snapshot, *args):
        calls.append(party_snapshot["id"])
        assert not db.in_transaction()
        with factory() as other:
            save_model_config(
                other, other.info["config"], ModelConfig(provider="none", generation=1)
            )
            other.commit()
        return Adjudication(model_prob=0.9)

    with pytest.raises(ScreeningValidationError, match="policy changed"):
        run_screening(db, adjudicate=hook)
    assert len(calls) == 1
    assert total(db, ScreeningRun) == total(db, Hit) == total(db, RunPartyEvidence) == 0
