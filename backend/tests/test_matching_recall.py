"""Independent frozen recall and actual migrated benchmark contracts."""

import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.screening.candidates import CandidateIndex
from app.screening.normalize import normalize_country, normalize_name
from app.screening.score import score_pair
from app.screening.settings import ScreeningSettings

GATE_PATH = Path(__file__).resolve().parents[2] / "tests/integration/matching.py"
spec = importlib.util.spec_from_file_location("matching_recall_gate", GATE_PATH)
assert spec and spec.loader
gate = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gate
spec.loader.exec_module(gate)


def test_independent_labels_and_original_public_sample_are_frozen():
    cases, controls = gate.frozen_cases()
    assert len(cases) == 180
    assert sum(case["same_party"] for case in cases) == 120
    assert sum(case["must_find"] for case in cases) == 80
    assert sum(len(control["targets"]) for control in controls) == 31
    original = json.loads((gate.FIXTURES / "csl_sample_v1.json").read_text())
    public = gate.public_entries(25_000)
    assert public[:300] == original
    assert len(public) == 25_000
    assert (
        len({entry.key for entry in gate.parse_bulk(json.dumps(public).encode()).entries}) == 25_000
    )


def test_benchmark_uses_real_nested_operation_result_and_immutable_repeat(tmp_path, monkeypatch):
    monkeypatch.delenv("SCREENING_TOOL_SECRET_KEY", raising=False)
    workspace = tmp_path / "new-book"
    result = gate.benchmark(workspace, parties=60, entries=300, seconds=30)
    assert result["passed"]
    assert result["list_kind"] == "frozen-public-CSL-subset"
    assert [run["name"] for run in result["runs"]] == ["cold", "repeat"]
    assert result["runs"][1]["hits_new"] == 0
    assert result["runs"][0]["queue_size"] == result["runs"][1]["queue_size"]
    assert result["runs"][0]["zero_hit_evidence_rows"] > 0
    assert (
        result["runs"][0]["original_evidence_sha256"]
        == result["runs"][1]["original_evidence_sha256"]
    )
    saved = json.loads((workspace / "report.json").read_text())
    assert saved == result
    with pytest.raises(gate.MatchingGateFailure, match="fresh_workspace_required_no_reset"):
        gate.benchmark(workspace, parties=60, entries=300, seconds=30)
    assert json.loads((workspace / "report.json").read_text()) == result


def test_frozen_first_run_recall_and_negative_rails():
    report = gate.recall_report()
    assert report["must_find_pairs"] == 80
    assert report["must_find_missed"] == []
    assert report["candidate_positive"] >= 114
    assert report["stored_positive"] >= 114
    assert report["negative_likely"] == []
    assert {row["case_id"]: row["candidates"] for row in report["mechanisms"]} == {
        "alias-slot-crowding": 1,
        "distinct-target-tail": 30,
    }


def test_recall_positive_aggregate_uses_case_labels_when_a_family_is_mixed(monkeypatch):
    baseline = gate.recall_report()
    cases, controls = gate.frozen_cases()
    mixed = deepcopy(cases)
    negative = next(case for case in mixed if case["family"] == "country-negative")
    negative["family"] = "unicode-equivalent"
    assert not negative["same_party"]
    monkeypatch.setattr(gate, "frozen_cases", lambda: (mixed, controls))
    report = gate.recall_report()
    assert report["families"]["unicode-equivalent"]["candidates"] == 11
    assert report["candidate_positive"] == baseline["candidate_positive"]


def test_fallback_returns_every_distinct_short_target_without_alias_slots():
    entries = [
        SimpleNamespace(
            id=f"source-{n}", name="12", alt_names=["12"] * 25, entity_type="individual"
        )
        for n in range(31)
    ]
    index = CandidateIndex(entries, ScreeningSettings())
    candidates, fallback = index.candidates(
        SimpleNamespace(name="13", alt_names=[], party_type="individual")
    )
    assert fallback
    assert {entry.id for entry in candidates} == {entry.id for entry in entries}


def test_normalization_cache_keys_include_type_suffixes_values_and_strict_country():
    assert normalize_name("Acme LLC").ascii == "acme"
    assert normalize_name("Acme LLC", legal_suffixes=[]).ascii == "acme llc"
    assert normalize_name("Acme LLC", "individual").ascii == "acme llc"
    assert normalize_name("Changed LLC").ascii == "changed"
    assert normalize_country("not-a-country") is None
    with pytest.raises(ValueError, match="Unrecognized country"):
        normalize_country("not-a-country", strict=True)
    assert ScreeningSettings().scoring_snapshot()["matching_policy_version"] == 2


def test_deferred_explanation_preserves_all_scores_and_every_stored_component():
    settings = ScreeningSettings()
    cases, _ = gate.frozen_cases()
    entries = gate.entry_objects([case["entry"] for case in cases])
    for case in cases:
        party = SimpleNamespace(**case["party"])
        entry = entries[case["entry"]["id"]]
        full = score_pair(party, entry, settings)
        deferred = score_pair(party, entry, settings, explanation_minimum=settings.candidate_min)
        assert full.score == deferred.score
        assert deferred.explanation == (
            full.explanation if full.score >= settings.candidate_min else {}
        )
