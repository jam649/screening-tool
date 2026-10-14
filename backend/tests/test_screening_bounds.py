"""Exact ceilings and batched comparisons never change a screening result."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from rapidfuzz import fuzz
from rapidfuzz.distance import LCSseq

from app.screening.batch import score_rows
from app.screening.bounds import NameBounds
from app.screening.score import below_minimum, names, party_features, score_pair
from app.screening.settings import DEFAULT_WEIGHTS, ScreeningSettings

GATE_PATH = Path(__file__).resolve().parents[2] / "tests/integration/matching.py"
spec = importlib.util.spec_from_file_location("matching_bounds_gate", GATE_PATH)
assert spec and spec.loader
gate = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gate
spec.loader.exec_module(gate)

SETTINGS = [
    ScreeningSettings(),
    ScreeningSettings(
        score_weights={
            **DEFAULT_WEIGHTS,
            "token_set": 0.2,
            "token_sort": 0.2,
            "partial": 0.6,
            "country_mismatch": 0,
            "address_max": -5,
            "short_name": 20,
        }
    ),
    ScreeningSettings(candidate_min=30, review_threshold=40, auto_clear_threshold=65),
    ScreeningSettings(candidate_min=0),
]


def recall_groups():
    cases, _ = gate.frozen_cases()
    entries = gate.entry_objects(
        [row for case in cases for row in [case["entry"], *case["distractors"]]]
    )
    for case in cases:
        rows = [case["entry"], *case["distractors"]]
        yield (
            SimpleNamespace(id=case["case_id"], **case["party"]),
            [entries[row["id"]] for row in rows],
        )


def typed(value):
    if isinstance(value, dict):
        return [type(value).__name__, [[key, typed(item)] for key, item in value.items()]]
    if isinstance(value, (list, tuple)):
        return [type(value).__name__, [typed(item) for item in value]]
    return [type(value).__name__, value.hex() if isinstance(value, float) else value]


@pytest.mark.parametrize("settings", SETTINGS)
def test_ceilings_only_exclude_discarded_pairs_and_bounded_scores_are_identical(settings):
    excluded = 0
    for party, entries in recall_groups():
        bounds = NameBounds(settings)
        listings = [bounds.listing(entry) for entry in entries]
        side = party_features(party, settings)
        party_bounds = bounds.party(side, listings)
        features = [listing.features for listing in listings]
        coarse = below_minimum(side, features, party_bounds.ceilings(), settings)
        refined = below_minimum(
            side,
            features,
            [party_bounds.refined_ceiling(index) for index in range(len(entries))],
            settings,
        )
        for index, entry in enumerate(entries):
            full = score_pair(party, entry, settings)
            if coarse[index] or refined[index]:
                excluded += 1
                assert full.score < settings.candidate_min
            bounded = score_pair(
                party,
                entry,
                settings,
                features=(side, features[index]),
                name_bound=party_bounds.comparisons(index),
            )
            assert typed(bounded.score) == typed(full.score)
            assert json.dumps(bounded.explanation) == json.dumps(full.explanation)
            assert typed(bounded.explanation) == typed(full.explanation)
    assert excluded or settings.candidate_min == 0


def test_batched_rows_are_bitwise_scalar_scores_for_any_worker_count():
    values = sorted(
        {
            variant
            for party, entries in recall_groups()
            for record, is_party in [(party, True), *((entry, False) for entry in entries)]
            for name in names(record, ScreeningSettings(), party=is_party)
            for variant in name.variants
        }
    )
    queries, choices = values[:40], values[40:400]
    for scorer, integer in [
        (fuzz.ratio, False),
        (fuzz.token_set_ratio, False),
        (fuzz.token_sort_ratio, False),
        (fuzz.partial_ratio, False),
        (fuzz.WRatio, False),
        (LCSseq.similarity, True),
    ]:
        expected = [[scorer(query, choice) for choice in choices] for query in queries]
        for workers in (1, 3):
            rows = score_rows(queries, choices, scorer, integer=integer, workers=workers)
            assert [[typed(value) for value in row] for row in rows] == [
                [typed(value) for value in row] for row in expected
            ]
