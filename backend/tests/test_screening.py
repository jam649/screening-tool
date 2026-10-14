import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from app.cli import cli
from app.csl.service import refresh_csl
from app.models import (
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
    process_pending_tasks,
    queue_hits,
    run_screening,
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
    hit.status = "dispositioned"
    disposition = Disposition(
        hit_id=hit.id, kind="false_positive", actor_user_id=admin["user"]["id"]
    )
    db.add(disposition)
    db.flush()
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
    with pytest.raises(RuntimeError):
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
