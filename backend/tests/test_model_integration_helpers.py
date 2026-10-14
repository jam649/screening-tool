"""Gate contract proof on isolated fixtures; no Docker or live model requests."""

import csv
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.adjudicators.budget import prepare_state
from app.adjudicators.context import build_state
from app.adjudicators.contracts import ModelConfig
from app.adjudicators.http import HTTPAdjudicator
from app.connectors.csv import CSVConnector
from app.csl.parser import parse_bulk
from app.csl.service import refresh_csl
from app.models import Party, Source
from app.reports.training import export_training_pairs
from app.screening.engine import queue_hits, run_screening
from app.screening.score import score_pair
from app.screening.settings import ScreeningSettings

GATES = Path(__file__).resolve().parents[2] / "tests" / "integration"
spec = importlib.util.spec_from_file_location("milestone8_gate_helpers", GATES / "models.py")
assert spec and spec.loader
gate = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gate
original_path = list(sys.path)
try:
    sys.path.insert(0, str(GATES))
    spec.loader.exec_module(gate)
finally:
    sys.path[:] = original_path


def test_explicit_mode_required_before_gate_or_network(monkeypatch):
    monkeypatch.setattr(gate, "ModelGate", lambda _: pytest.fail("No gate without explicit flag"))
    with pytest.raises(SystemExit) as error:
        gate.main([])
    assert error.value.code == 2


def test_actual_mode_requires_endpoint_and_cannot_be_smuggled_into_synthetic_run(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        gate, "ModelGate", lambda _: pytest.fail("No network before mode validation")
    )
    for flags in (
        ["--run-actual-laya-gate"],
        ["--run-fixture-gate", "--actual-laya-base-url", "http://localhost:8009"],
    ):
        with pytest.raises(SystemExit) as error:
            gate.main(flags)
        assert error.value.code == 1
    assert "failed_check" in capsys.readouterr().out


def fixture_pair(changed=False):
    batch = CSVConnector(
        gate.csv_book(changed), "model.csv", {name: name for name in gate.HEADERS}
    ).full_sync()
    assert not batch.rejected and len(batch.parties) == 2
    entry = next(
        row
        for row in parse_bulk(json.dumps(gate.fixture_rows()).encode()).entries
        if row.csl_id == gate.CONTROL_ID
    )
    party = batch.parties[0].fields()
    scored = score_pair(
        SimpleNamespace(**party), SimpleNamespace(**entry.fields()), ScreeningSettings()
    )
    return batch, party, entry.fields(), scored


def test_real_fixture_parser_csv_types_budget_trimming_and_synthetic_wire_usage(client):
    batch, party, entry, score = fixture_pair(changed=True)
    original, _, _, _ = fixture_pair()
    assert batch.parties[0].external_id == original.parties[0].external_id == "fp"
    assert batch.parties[0].record_hash != original.parties[0].record_hash
    assert batch.parties[1].record_hash == original.parties[1].record_hash
    assert score.score >= 90
    state, budget = prepare_state(
        build_state(party, entry, score.explanation), score.explanation, "laya"
    )
    assert budget["removed_fields"] == ["remarks", "address_lines", "extra_aliases"]
    assert budget["exact"] and budget["sequence_tokens"] <= 1024
    remote = gate.ModelFixture()
    remote.app = client.app
    model = ModelConfig(provider="laya", laya_base_url=gate.FIXTURE_URL)
    result = HTTPAdjudicator(model, transport=httpx.MockTransport(remote.request)).evaluate(
        state, budget=budget
    )
    assert result.model_prob == 0 and result.returned_model == "multilingual"
    assert result.input_tokens == budget["sequence_tokens"] and result.attempts == 1
    assert remote.writer_checks == remote.calls == 1
    remote.phase = "invalid_response"
    rejected = HTTPAdjudicator(model, transport=httpx.MockTransport(remote.request)).evaluate(
        state, budget=budget
    )
    assert (
        rejected.model_prob is None
        and rejected.error_code == "invalid_response"
        and rejected.attempts == 1
    )


def test_synthetic_hosted_wire_uses_verified_pinned_model_and_never_real_network(client):
    remote = gate.ModelFixture()
    remote.app = client.app
    result = HTTPAdjudicator(
        ModelConfig(provider="jev", jev_api_key=gate.PRIVATE_KEY),
        transport=httpx.MockTransport(remote.request),
    ).evaluate({"synthetic": True})
    assert result.model_prob == 0 and result.returned_model == gate.JEV_MODEL
    assert remote.calls == 1


def test_actual_server_does_not_install_mock_transport_or_dummy_inference_route(
    client, monkeypatch
):
    from app.csl import service

    monkeypatch.setattr(
        gate,
        "Config",
        lambda: SimpleNamespace(
            database_url="sqlite:////data/milestone8-fixture.db", allowed_hosts=["localhost"]
        ),
    )
    monkeypatch.setattr(gate, "create_app", lambda _: client.app)
    # Restore global download helpers after this isolated app-construction proof.
    monkeypatch.setattr(service, "download_bulk", service.download_bulk)
    monkeypatch.setattr(service, "download_stamps", service.download_stamps)
    application = gate.fixture_app("http://host.docker.internal:8009")
    assert application.state.model_transport is None
    assert "/v1/systemone" not in {
        getattr(route, "path", None) for route in application.router.routes
    }
    phase = next(
        route
        for route in application.router.routes
        if getattr(route, "path", None) == "/_fixture/model/phase"
    )
    with pytest.raises(gate.GateFailure, match="actual_server_never_installs_synthetic_transport"):
        phase.endpoint(gate.PhaseBody(phase="valid"), None)
    assert application.state.model_transport is None


@pytest.fixture
def human_export(client):
    setup = client.post(
        "/api/v1/setup",
        json={
            "email": gate.ADMIN_EMAIL,
            "password": gate.PASSWORD,
            "display_name": "Fixture Model Admin",
            "disclaimer_acknowledged": True,
        },
    )
    assert setup.status_code == 201
    admin = setup.json()
    with client.app.state.session_factory() as db:
        refresh_csl(db, data=json.dumps(gate.fixture_rows()).encode())
        source = Source(kind="csv", label="Synthetic model book")
        db.add(source)
        db.flush()
        batch, _, _, _ = fixture_pair()
        for row in batch.parties:
            db.add(Party(source_id=source.id, **row.fields()))
        db.commit()
        run_screening(db)
        run_screening(db)
        originals = {}
        targets = [hit for hit in queue_hits(db) if hit.entry_snapshot["csl_id"] == gate.CONTROL_ID]
        assert len(targets) == 2
        for hit, kind in zip(targets, ("false_positive", "confirmed_match"), strict=True):
            result = client.post(
                f"/api/v1/hits/{hit.id}/dispositions",
                headers={"x-csrf-token": admin["csrf_token"]},
                json={"kind": kind, "note": "Actual synthetic actor note"},
            )
            assert result.status_code == 201 and result.json()["observations_dispositioned"] >= 2
            originals[hit.party_id] = {
                "id": hit.id,
                "party_hash": hit.party_hash,
                "entry_hash": hit.entry_hash,
            }
        db.rollback()
        artifact = export_training_pairs(db)
        rows = list(csv.DictReader(io.StringIO(artifact.body.decode("utf-8-sig"))))
        yield rows, admin["user"]["id"], originals


def training_bytes(rows):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def test_gate_training_contract_matches_actual_original_human_fanout(human_export):
    rows, actor_id, originals = human_export
    assert gate.verify_training(training_bytes(rows), actor_id=actor_id, originals=originals) == 4


@pytest.mark.parametrize(
    "field", ["actor_snapshot", "party_hash", "queue_hit_id", "party_snapshot"]
)
def test_training_helper_rejects_changed_actor_hash_group_and_snapshot(human_export, field):
    rows, actor_id, originals = human_export
    if field == "actor_snapshot":
        rows[0][field] = json.dumps({"id": "wrong-actor", "email": gate.ADMIN_EMAIL})
    elif field == "party_snapshot":
        rows[0][field] = json.dumps({"name": gate.CONTROL, "party_type": "individual"})
    else:
        rows[0][field] = "wrong-provenance"
    with pytest.raises(gate.GateFailure):
        gate.verify_training(training_bytes(rows), actor_id=actor_id, originals=originals)


@pytest.mark.parametrize("secret", ["plaintext", "", "[REDACTED]", False])
def test_export_privacy_only_accepts_exact_markers_and_public_measurements(secret):
    with pytest.raises(gate.GateFailure, match="export_exact_private_redaction"):
        gate.verify_private_fields({"nested": {"jev_api_key": secret}})
    gate.verify_private_fields(
        {
            "jev_api_key": "[redacted]",
            "laya_api_key": None,
            "input_tokens": 512,
            "budget": {"tokenizer_sha256": gate.LAYA_TOKENIZER_SHA256},
        }
    )
