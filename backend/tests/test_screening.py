import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from app.cli import cli
from app.csl.service import refresh_csl
from app.models import (
    CSLEntry,
    Disposition,
    Hit,
    Party,
    ScreeningRun,
    ScreeningTask,
    Setting,
    Source,
    Suppression,
)
from app.screening.candidates import CandidateIndex
from app.screening.engine import (
    Adjudication,
    ScreeningBusyError,
    ScreeningTasksFailedError,
    process_pending_tasks,
    queue_hit_is_current,
    queue_hits,
    run_screening,
    snapshot,
)
from app.screening.normalize import normalize_country, normalize_name, party_record_hash
from app.screening.score import bucket, score_pair
from app.screening.settings import ScreeningSettings, ScreeningValidationError


def party(name="Acme Manufacturing", **changes):
    return SimpleNamespace(
        **(
            {
                "id": "party",
                "name": name,
                "alt_names": [],
                "party_type": "company",
                "country_iso2": "US",
                "city": None,
                "address_lines": [],
            }
            | changes
        )
    )


def entry(name="Acme Manufacturing", **changes):
    return SimpleNamespace(
        **(
            {
                "id": "entry",
                "name": name,
                "alt_names": [],
                "entity_type": "entity",
                "countries": ["US"],
                "addresses": [],
            }
            | changes
        )
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("  ÁCME, Manufacturing Ltd. ", "acme manufacturing"),
        ("ＡＣＭＥ LLC", "acme"),
        ("Acme Pty Ltd", "acme"),
    ],
)
def test_normalization(value, expected):
    result = normalize_name(value)
    assert result.original == value
    assert result.ascii == expected
    assert normalize_name("Ácme").unicode == "ácme"
    assert normalize_name("GmbH").ascii == "gmbh"  # never erase an entire name
    assert normalize_name("Acme Ltd", legal_suffixes=()).ascii == "acme ltd"
    assert "john smith" in normalize_name("Smith, John", "individual").variants


def test_country_and_hash_contract():
    assert normalize_country("United States") == "US"
    assert normalize_country("CAN") == "CA"
    assert normalize_country("unknown") is None
    with pytest.raises(ValueError):
        normalize_country("unknown", strict=True)
    fields = {"name": "ACME Ltd.", "alt_names": ["Second", "First"], "country": "United States"}
    assert party_record_hash(fields) == party_record_hash(
        fields
        | {
            "name": "acme ltd",
            "alt_names": ["First", "Second"],
            "phone": "changed",
            "role": "vendor",
            "raw": {"phone": "other"},
        }
    )
    assert party_record_hash(fields) != party_record_hash(fields | {"city": "Changed"})
    assert party_record_hash(fields) != party_record_hash(fields | {"name": "ACME LLC"})
    assert party_record_hash(fields) != party_record_hash(fields | {"party_type": "individual"})


@pytest.mark.parametrize(
    ("changes", "component", "expected"),
    [
        ({"country_iso2": "CA"}, "country", -15),
        ({"country_iso2": None}, "country", 0),
        ({"city": "Boston"}, "city", 5),
        ({"address_lines": ["1 Main Street"]}, "address", 5),
        ({"party_type": "individual"}, "entity_type", -10),
    ],
)
def test_scoring_components(changes, component, expected):
    result = score_pair(
        party(**changes),
        entry(addresses=[{"address": "1 Main Street", "city": "Boston"}]),
        ScreeningSettings(),
    )
    assert result.explanation["components"][component] == expected
    assert 0 <= result.score <= 100


def test_alias_max_order_short_name_and_buckets():
    settings = ScreeningSettings()
    result = score_pair(
        party("Unknown", alt_names=["Exact Listed Alias"]),
        entry("Other", alt_names=["Exact Listed Alias"]),
        settings,
    )
    assert result.explanation["best_aliases"] == {
        "party": "Exact Listed Alias",
        "entry": "Exact Listed Alias",
    }
    assert result.score == 100
    assert (
        score_pair(party("Li"), entry("Li"), settings).explanation["components"]["short_name"]
        == -10
    )
    assert (
        score_pair(
            party("Li", alt_names=["Long Exact Alias"]), entry("Long Exact Alias"), settings
        ).explanation["components"]["short_name"]
        == 0
    )
    assert (
        score_pair(
            party("Smith, John", party_type="individual"),
            entry("John Smith", entity_type="individual"),
            settings,
        ).score
        == 100
    )
    assert bucket(64.99, settings) == "low"
    assert bucket(65, settings) == "review"
    assert bucket(90, settings) == "likely"
    assert bucket(1, settings, 95) == "likely"


def test_candidate_alias_phonetic_and_full_index_fallback():
    settings = ScreeningSettings()
    entries = [
        entry("Acme Global", id="one"),
        entry("Unrelated", id="two", alt_names=["Acme Works"]),
        entry("Smith John", id="three", entity_type="individual"),
        entry("Smyth Peter", id="four"),
    ]
    index = CandidateIndex(entries, settings)
    candidates, fallback = index.candidates(party("Acme"))
    assert {"one", "two"} <= {e.id for e in candidates}
    assert fallback
    candidates, _ = index.candidates(party("Smith Jane", party_type="individual"))
    assert "four" in {e.id for e in candidates}
    candidates, fallback = CandidateIndex([entry("Qwerty Fittings")], settings).candidates(
        party("Qwrety Fittings")
    )
    assert fallback and len(candidates) == 1
    # A two-character first token has no token block and a different phonetic code;
    # only the complete fallback can find the pair.
    candidates, fallback = CandidateIndex([entry("Li")], settings).candidates(party("Lu"))
    assert fallback and len(candidates) == 1


def listing(name="Acme Manufacturing", number="one", **changes):
    return {
        "id": number,
        "source": "Entity List (EL) - Bureau of Industry and Security",
        "name": name,
        "type": "Entity",
        "alt_names": [],
        "addresses": [{"country": "US"}],
        "programs": [],
        **changes,
    }


@pytest.fixture
def seeded(client, admin):
    with client.app.state.session_factory() as db:
        refresh = refresh_csl(db, data=json.dumps([listing()]).encode())
        source = Source(kind="csv", label="Synthetic")
        db.add(source)
        db.flush()
        values = {"name": "Acme Manufacturing", "country_iso2": "US"}
        record = Party(
            source_id=source.id,
            external_id="one",
            **values,
            record_hash=party_record_hash(values),
            raw={"name": "original source"},
        )
        db.add(record)
        db.commit()
        yield db, record, refresh


def test_repeated_runs_have_immutable_observations_and_one_queue_anchor(seeded):
    db, record, _ = seeded
    first = run_screening(db)
    second = run_screening(db)
    assert first.hits_new == 1 and second.hits_new == 0
    hits = list(db.scalars(select(Hit).order_by(Hit.created_at)))
    assert len(hits) == 2 and all(h.status == "open" for h in hits)
    assert hits[1].explanation["queue_anchor_hit_id"] == hits[0].id
    assert queue_hits(db) == [hits[0]]
    old_name = hits[0].party_snapshot["name"]
    record.name = "Changed now"
    record.record_hash = party_record_hash({"name": record.name})
    db.commit()
    assert hits[0].party_snapshot["name"] == old_name
    assert queue_hits(db) == []


def test_exact_suppression_and_changed_hash_prior_history(seeded, admin):
    db, record, _ = seeded
    run_screening(db)
    hit = db.scalar(select(Hit))
    disposition = Disposition(
        hit_id=hit.id, kind="false_positive", actor_user_id=admin["user"]["id"]
    )
    db.add(disposition)
    db.flush()
    hit.status = "dispositioned"
    db.add(
        Suppression(
            party_id=record.id,
            csl_entry_id=hit.csl_entry_id,
            party_hash=hit.party_hash,
            entry_hash=hit.entry_hash,
            disposition_id=disposition.id,
        )
    )
    db.commit()
    repeat = run_screening(db)
    assert repeat.hits_new == 0 and repeat.hits_suppressed == 1
    suppressed = db.scalar(select(Hit).where(Hit.run_id == repeat.id))
    assert suppressed.status == "suppressed"
    record.city = "Boston"
    record.record_hash = party_record_hash(
        {"name": record.name, "country_iso2": "US", "city": "Boston"}
    )
    db.commit()
    reopened = run_screening(db)
    new_hit = db.scalar(select(Hit).where(Hit.run_id == reopened.id))
    assert reopened.hits_new == 1 and new_hit.status == "open"
    assert new_hit.explanation["prior_disposition_id"] == disposition.id
    assert new_hit.explanation["what_changed"]["party"]["city"] == {
        "before": None,
        "after": "Boston",
    }


def test_settings_context_refreshes_queue_without_inventing_disposition(seeded):
    db, _, _ = seeded
    run_screening(db)
    first = queue_hits(db)[0]
    db.add(Setting(key="score_weights", value={"country_match": -20}))
    db.commit()
    run = run_screening(db)
    current = queue_hits(db)[0]
    assert current.id != first.id
    assert current.det_score == 80 and current.bucket == "review"
    assert first.status == "open"
    assert run.hits_new == 1
    assert db.scalar(select(func.count()).select_from(Disposition)) == 0
    assert run_screening(db).hits_new == 0
    assert queue_hits(db)[0].id == current.id


def test_scoped_tasks_skip_stale_entry_and_preserve_unchanged_delta(seeded):
    db, record, original = seeded
    changed = refresh_csl(
        db,
        data=json.dumps(
            [listing("Changed Manufacturing"), listing("Acme Manufacturing", "two")]
        ).encode(),
    )
    # Old task's original entry hash is stale; the newer task retains its own scope.
    runs = process_pending_tasks(db)
    assert len(runs) == 2
    assert runs[0].csl_version_id == original.version.id
    assert db.scalar(select(func.count()).select_from(Hit).where(Hit.run_id == runs[0].id)) == 0
    assert runs[1].csl_version_id == changed.version.id
    assert all(task.status == "complete" for task in db.scalars(select(ScreeningTask)))
    assert process_pending_tasks(db) == []
    # A party change uses the current list, not an outdated enqueue version.
    task = ScreeningTask(
        trigger="party_change", csl_version_id=original.version.id, party_ids=[record.id]
    )
    db.add(task)
    db.commit()
    assert process_pending_tasks(db)[0].csl_version_id == changed.version.id


def test_task_survives_newer_version_when_its_entry_hash_unchanged(seeded):
    db, _, original = seeded
    refresh_csl(db, data=json.dumps([listing(), listing("Other Name", "two")]).encode())
    runs = process_pending_tasks(db)
    assert runs[0].csl_version_id == original.version.id
    assert db.scalar(select(func.count()).select_from(Hit).where(Hit.run_id == runs[0].id)) == 1


def test_model_hook_immutable_preinsert_rail_and_atomic_failure(seeded):
    db, _, _ = seeded
    run = run_screening(
        db,
        adjudicate=lambda *args: Adjudication(
            model_prob=0.01, final_confidence=10, model_name="synthetic test hook"
        ),
    )
    hit = db.scalar(select(Hit).where(Hit.run_id == run.id))
    assert hit.model_prob == 0.01 and hit.final_confidence == 10 and hit.bucket == "likely"
    before = db.scalar(select(func.count()).select_from(ScreeningRun))
    with pytest.raises(ScreeningValidationError):
        run_screening(db, adjudicate=lambda *args: Adjudication(final_confidence=101))
    assert db.scalar(select(func.count()).select_from(ScreeningRun)) == before


def test_screen_api_scope_auth_and_run_status(seeded, client, admin):
    _, record, _ = seeded
    headers = {"x-csrf-token": admin["csrf_token"]}
    assert client.post("/api/v1/screen", json={"all": True}).status_code == 403
    for body in ({}, {"all": True, "party_id": record.id}):
        assert client.post("/api/v1/screen", json=body, headers=headers).status_code == 422
    response = client.post("/api/v1/screen", json={"party_id": record.id}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["hits_new"] == 1
    assert client.get(f"/api/v1/screen/runs/{response.json()['id']}").status_code == 200
    assert len(client.get("/api/v1/screen/runs").json()) == 1


def test_settings_validation():
    with pytest.raises(ScreeningValidationError):
        ScreeningSettings(candidate_min=80, review_threshold=65)
    with pytest.raises(ScreeningValidationError):
        replace(ScreeningSettings(), score_weights={"bogus": 1})
    for value in ("50", None, True, float("nan")):
        with pytest.raises(ScreeningValidationError):
            ScreeningSettings(candidate_min=value)
    for value in ("inc,ltd", {"inc": 1}, None):
        with pytest.raises(ScreeningValidationError):
            ScreeningSettings(legal_suffixes=value)


def test_low_recorded_and_raised_candidate_min_removes_stale_queue(seeded):
    db, _, _ = seeded
    run_screening(db)
    original = queue_hits(db)[0]
    weights = Setting(key="score_weights", value={"country_match": -45})
    db.add(weights)
    db.commit()
    low_run = run_screening(db)
    low_hit = db.scalar(select(Hit).where(Hit.run_id == low_run.id))
    assert low_hit.det_score == 55 and low_hit.bucket == "low" and low_hit.status == "open"
    assert low_run.hits_new == 0 and queue_hits(db) == []
    assert original.status == "open"
    weights.value = {"country_match": -20}
    db.commit()
    run_screening(db)
    assert queue_hits(db)[0].det_score == 80
    db.add(Setting(key="candidate_min", value=90))
    db.add(Setting(key="review_threshold", value=90))
    db.commit()
    no_hit_run = run_screening(db)
    assert db.scalar(select(func.count()).select_from(Hit).where(Hit.run_id == no_hit_run.id)) == 0
    assert queue_hits(db) == []
    assert db.scalar(select(func.count()).select_from(Disposition)) == 0


def test_failed_task_is_atomic_sanitized_and_retries(seeded, monkeypatch):
    db, _, _ = seeded
    from app.screening import engine

    original_score = engine.score_pair

    def fail(*args):
        raise RuntimeError("credential-must-not-be-recorded")

    monkeypatch.setattr(engine, "score_pair", fail)
    with pytest.raises(ScreeningTasksFailedError):
        process_pending_tasks(db)
    task = db.scalar(select(ScreeningTask))
    assert task.status == "failed" and task.attempts == 1
    assert "credential" not in task.last_error
    assert db.scalar(select(func.count()).select_from(ScreeningRun)) == 0
    assert db.scalar(select(func.count()).select_from(Hit)) == 0
    monkeypatch.setattr(engine, "score_pair", original_score)
    runs = process_pending_tasks(db)
    assert len(runs) == 1 and task.status == "complete" and task.attempts == 2


def test_cross_source_published_id_collision_preserves_two_hits(seeded):
    db, _, _ = seeded
    refresh_csl(
        db,
        data=json.dumps(
            [
                listing(),
                listing(source="Specially Designated Nationals (SDN) - Treasury Department"),
            ]
        ).encode(),
    )
    run = run_screening(db)
    hits = list(db.scalars(select(Hit).where(Hit.run_id == run.id)))
    assert len(hits) == 2 and len({hit.csl_entry_id for hit in hits}) == 2
    assert {hit.entry_snapshot["source_list"] for hit in hits} == {"EL", "SDN"}
    assert {hit.entry_snapshot["csl_id"] for hit in hits} == {"one"}


def test_screen_cli_all_party_changed_and_scope_validation(seeded, config, monkeypatch):
    _, record, _ = seeded
    monkeypatch.setenv("SCREENING_TOOL_DATABASE_URL", config.database_url)
    monkeypatch.setenv("SCREENING_TOOL_SECRET_KEY", config.secret_key.get_secret_value())
    runner = CliRunner()
    response = runner.invoke(cli, ["screen", "--all"])
    assert response.exit_code == 0, response.output
    assert json.loads(response.output)[0]["hits_new"] == 1
    response = runner.invoke(cli, ["screen", "--party", record.id])
    assert response.exit_code == 0, response.output
    assert json.loads(response.output)[0]["hits_new"] == 0
    response = runner.invoke(cli, ["screen", "--changed"])
    assert response.exit_code == 0, response.output
    assert len(json.loads(response.output)) == 1
    response = runner.invoke(cli, ["screen"])
    assert response.exit_code != 0
    response = runner.invoke(cli, ["screen", "--all", "--party", record.id])
    assert response.exit_code != 0


def test_queue_excludes_disabled_source_lists_without_rewriting_evidence(seeded):
    db, _, _ = seeded
    run_screening(db)
    original = queue_hits(db)[0]
    setting = Setting(key="enabled_source_lists", value=[])
    db.add(setting)
    db.commit()
    assert queue_hits(db) == []
    assert original.status == "open"
    setting.value = ["EL"]
    db.commit()
    assert queue_hits(db) == [original]


def decide(client, admin, hit, kind):
    response = client.post(
        f"/api/v1/hits/{hit.id}/dispositions",
        headers={"x-csrf-token": admin["csrf_token"]},
        json={"kind": kind, "note": "Recorded reviewer judgment"},
    )
    assert response.status_code == 201, response.text
    return response.json()["disposition"]


@pytest.mark.parametrize("kind", ["confirmed_match", "escalated"])
def test_same_hash_human_decision_survives_repeat_then_changed_hash_reopens(
    seeded, client, admin, kind
):
    db, record, _ = seeded
    run_screening(db)
    run_screening(db)  # two unresolved observations sharing the same anchor
    original = queue_hits(db)[0]
    decide(client, admin, original, kind)
    db.expire_all()
    before = db.scalar(select(func.count()).select_from(Disposition))
    latest = db.scalar(
        select(Disposition).order_by(Disposition.created_at.desc(), Disposition.id.desc())
    )
    assert queue_hits(db) == []
    repeat = run_screening(db)
    observation = db.scalar(select(Hit).where(Hit.run_id == repeat.id))
    assert repeat.hits_new == 0 and observation.status == "open"
    assert observation.explanation["queue_anchor_hit_id"] == latest.hit_id
    assert observation.explanation["completed_disposition_id"] == latest.id
    assert not queue_hit_is_current(db, observation) and queue_hits(db) == []
    assert db.scalar(select(func.count()).select_from(Disposition)) == before
    record.city = "Changed City"
    record.record_hash = party_record_hash(snapshot(record))
    db.commit()
    reopened = run_screening(db)
    assert reopened.hits_new == 1
    current = queue_hits(db)[0]
    assert current.explanation["prior_disposition_id"] == latest.id
    assert queue_hit_is_current(db, current)


@pytest.mark.parametrize("kind", ["confirmed_match", "escalated"])
def test_newer_positive_overrides_old_exact_false_positive_and_new_clear_restores_memory(
    seeded, client, admin, kind
):
    db, record, _ = seeded
    run_screening(db)
    x = queue_hits(db)[0]
    first_clear = decide(client, admin, x, "false_positive")
    db.expire_all()
    record.city = "New City"
    record.record_hash = party_record_hash(snapshot(record))
    db.commit()
    run_screening(db)
    y = queue_hits(db)[0]
    positive = decide(client, admin, y, kind)
    db.expire_all()
    record.city = None  # exact old X hashes; latest human judgment concerned Y
    record.record_hash = party_record_hash(snapshot(record))
    db.commit()
    reopened = run_screening(db)
    assert reopened.hits_new == 1 and reopened.hits_suppressed == 0
    current = queue_hits(db)[0]
    assert current.party_hash == x.party_hash
    assert current.explanation["prior_disposition_id"] == positive["id"]
    assert current.explanation["prior_disposition_id"] != first_clear["id"]
    assert current.explanation["what_changed"]["party"]["city"] == {
        "before": "New City",
        "after": None,
    }
    # A fresh human false-positive on X supersedes the intervening positive, even
    # though the existing unique suppression record still points at its first clear.
    newest_clear = decide(client, admin, current, "false_positive")
    db.expire_all()
    repeated = run_screening(db)
    observation = db.scalar(select(Hit).where(Hit.run_id == repeated.id))
    assert repeated.hits_suppressed == 1 and repeated.hits_new == 0
    assert observation.explanation["suppression_disposition_id"] == newest_clear["id"]
    assert queue_hits(db) == []


def test_full_run_explicitly_rescores_prior_delta_candidate_when_blocking_changes(seeded):
    db, record, _ = seeded
    record.name = "Nikolay Chugunov"
    record.party_type = "individual"
    record.record_hash = party_record_hash(snapshot(record))
    db.commit()
    original_listing = listing("Mykola Chuhunov", type="Individual")
    refresh_csl(db, data=json.dumps([original_listing]).encode())
    target = db.scalar(select(CSLEntry))
    first = run_screening(db, entry_ids=[target.id])
    target_hit = db.scalar(select(Hit).where(Hit.run_id == first.id))
    assert target_hit is not None and target_hit.bucket != "low"
    assert target_hit.explanation["blocking_fallback"]
    refresh_csl(
        db,
        data=json.dumps(
            [
                original_listing,
                listing("Nikolay Aster", "two", type="Individual"),
                listing("Nikolay Brown", "three", type="Individual"),
                listing("Chugunov Distant", "four", type="Individual"),
            ]
        ).encode(),
    )
    settings = ScreeningSettings()
    index = CandidateIndex(list(db.scalars(select(CSLEntry))), settings)
    candidates, fallback = index.candidates(record)
    assert not fallback and target.id not in {entry.id for entry in candidates}
    second = run_screening(db)
    observation = db.scalar(
        select(Hit).where(Hit.run_id == second.id, Hit.csl_entry_id == target.id)
    )
    assert observation is not None and observation.explanation["prior_pair_forced_candidate"]
    assert observation.explanation["queue_anchor_hit_id"] == target_hit.id
    assert target_hit.id in {hit.id for hit in queue_hits(db)}
    # Changing the score policy genuinely below candidate_min removes the current
    # item only after the previously observed pair has been explicitly re-scored.
    db.add(Setting(key="score_weights", value={"country_match": -100}))
    db.commit()
    third = run_screening(db)
    assert (
        db.scalar(select(Hit).where(Hit.run_id == third.id, Hit.csl_entry_id == target.id)) is None
    )
    assert target_hit.id not in {hit.id for hit in queue_hits(db)}


def test_failed_old_task_does_not_starve_independent_new_task(seeded, monkeypatch):
    db, record, refresh = seeded
    from app.screening import engine

    original_execute = engine._execute
    old_task = db.scalar(select(ScreeningTask))
    new_task = ScreeningTask(
        trigger="party_change", csl_version_id=refresh.version.id, party_ids=[record.id]
    )
    db.add(new_task)
    db.commit()

    def execute(db, **kwargs):
        if kwargs["trigger"] == "list_change":
            raise RuntimeError("SECRET: exception must never enter diagnostics")
        return original_execute(db, **kwargs)

    monkeypatch.setattr(engine, "_execute", execute)
    with pytest.raises(ScreeningTasksFailedError) as raised:
        process_pending_tasks(db)
    assert len(raised.value.completed_runs) == 1
    assert raised.value.completed_runs[0].hits_new == 1
    assert raised.value.failures[0]["task_id"] == old_task.id
    assert "SECRET" not in str(raised.value) and "SECRET" not in str(raised.value.failures)
    assert old_task.status == "failed" and new_task.status == "complete"
    assert db.scalar(select(func.count()).select_from(ScreeningRun)) == 1
    assert len(queue_hits(db)) == 1
    with pytest.raises(ScreeningTasksFailedError) as retried:
        process_pending_tasks(db)
    assert retried.value.completed_runs == []
    assert old_task.attempts == 2 and new_task.attempts == 1
    assert db.scalar(select(func.count()).select_from(ScreeningRun)) == 1


def test_busy_task_drain_retains_earlier_success_and_pending_task(seeded, monkeypatch):
    db, record, refresh = seeded
    from app.screening import engine

    pending = ScreeningTask(
        trigger="party_change", csl_version_id=refresh.version.id, party_ids=[record.id]
    )
    db.add(pending)
    db.commit()
    original_begin = engine._begin
    calls = 0

    def begin(db):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ScreeningBusyError("private details must not enter response")
        original_begin(db)

    monkeypatch.setattr(engine, "_begin", begin)
    with pytest.raises(ScreeningTasksFailedError) as raised:
        process_pending_tasks(db)
    assert len(raised.value.completed_runs) == 1
    assert raised.value.failures[0]["task_id"] == pending.id
    assert "private" not in str(raised.value.failures)
    assert pending.status == "pending" and pending.attempts == 0
    assert db.scalar(select(func.count()).select_from(ScreeningRun)) == 1
    assert len(queue_hits(db)) == 1
    retry = process_pending_tasks(db)
    assert len(retry) == 1 and retry[0].hits_new == 0
    assert pending.status == "complete" and pending.attempts == 1
