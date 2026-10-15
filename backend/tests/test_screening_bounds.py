"""Exact ceilings and batched comparisons never change a screening result."""

import importlib.util
import json
import sys
from itertools import chain, islice
from pathlib import Path
from types import SimpleNamespace

import pytest
from rapidfuzz import fuzz, process
from rapidfuzz.distance import LCSseq

from app.screening.batch import score_rows
from app.screening.bounds import NameBounds
from app.screening.candidates import FALLBACK_WINDOW, CandidateIndex
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


def extract_iter_admissions(variants, entries, settings):
    """Entries the pre-batching fallback admitted: extract_iter over every name."""
    choices = {
        f"{entry.id}:{position}": variant
        for entry in entries
        for position, variant in enumerate(
            variant for name in names(entry, settings, party=False) for variant in name.variants
        )
    }
    return {
        key.rsplit(":", 1)[0]
        for variant in variants
        for _, _, key in process.extract_iter(
            variant, choices, scorer=fuzz.WRatio, score_cutoff=settings.candidate_min
        )
    }


def test_fallback_scan_admits_exactly_what_extract_iter_admits():
    settings = ScreeningSettings()
    groups = list(recall_groups())
    entries = list({entry.id: entry for _, rows in groups for entry in rows}.values())
    index = CandidateIndex(entries, settings)
    variants = sorted(
        {
            variant
            for party, _ in groups
            for name in names(party, settings, party=True)
            for variant in name.variants
        }
    )
    index._scan(variants)
    for variant in variants:
        assert index.fallback_hits[variant] == extract_iter_admissions([variant], entries, settings)


def company(identifier, name):
    return SimpleNamespace(id=identifier, name=name, alt_names=[], entity_type="Entity")


def party(identifier, name):
    return SimpleNamespace(id=identifier, name=name, alt_names=[], party_type="company")


def variants_of(record, settings):
    return [v for name in names(record, settings, party=True) for v in name.variants]


@pytest.mark.parametrize(
    ("settings", "left", "right"),
    [
        # WRatio("a", "ab") is exactly 90; rapidfuzz's batch cutoff tolerance
        # still admits it just above 90, as extract_iter always did.
        (
            ScreeningSettings(
                candidate_min=90.00000000000001,
                review_threshold=91,
                likely_threshold=91,
                auto_clear_threshold=91,
                score_weights={**DEFAULT_WEIGHTS, "token_set": 0, "token_sort": 0, "partial": 1},
            ),
            "A",
            "AB",
        ),
        # A zero cutoff admits zero scores too.
        (ScreeningSettings(candidate_min=0), "Q", "Zzzz"),
        # WRatio returns its scaled score 45.599999999999994 without rechecking
        # the cutoff; the pair then scores 51.625 and is stored.
        (ScreeningSettings(candidate_min=45.6), "shipping maria", "marine park"),
        # The same tolerance on the repeated-token path that is always rescored.
        (
            ScreeningSettings(candidate_min=41.8),
            "anna royal capital ali",
            "abu lee abu aisha 4 mohammed",
        ),
    ],
)
def test_fallback_admission_matches_extract_iter_at_the_cutoff(settings, left, right):
    entry, record = company("entry", right), party("party", left)
    candidates, fallback = CandidateIndex([entry], settings).candidates(record)
    assert fallback
    admitted = extract_iter_admissions(variants_of(record, settings), [entry], settings)
    assert {row.id for row in candidates} == admitted == {"entry"}


@pytest.mark.parametrize(
    ("settings", "left", "right", "country", "score"),
    [
        (ScreeningSettings(candidate_min=45.6), "shipping maria", "marine park", None, 51.625),
        # extract_iter admits even a zero WRatio at this near-zero cutoff, and
        # the country match alone then stores the pair.
        (ScreeningSettings(candidate_min=1e-46), "qqqqq", "zzzzz", "US", 5.0),
    ],
)
def test_boundary_fallback_admission_keeps_the_stored_pair(settings, left, right, country, score):
    entry = SimpleNamespace(
        id="e1",
        name=right,
        alt_names=[],
        entity_type="Entity",
        countries=[country] if country else [],
        addresses=[],
    )
    record = SimpleNamespace(
        id="p1",
        name=left,
        alt_names=[],
        party_type="company",
        country_iso2=country,
        city=None,
        address_lines=[],
    )
    candidates, fallback = CandidateIndex([entry], settings).candidates(record)
    assert fallback and candidates == [entry]
    assert score_pair(record, entry, settings).score == score >= settings.candidate_min


def test_near_cutoff_fallback_rejection_matches_extract_iter():
    # Batched WRatio keeps 57.49999999999999 at cutoff 57.5 where extract_iter
    # rejects it; the pair would otherwise be stored at 59.9351.
    settings = ScreeningSettings(candidate_min=57.5)
    entry = SimpleNamespace(
        id="e1",
        name="umar logistics shipping services karamay",
        alt_names=[],
        entity_type="Entity",
        countries=["US"],
        addresses=[],
    )
    record = SimpleNamespace(
        id="p1",
        name="ramu scitsigol gnippihs services yamarak",
        alt_names=[],
        party_type="company",
        country_iso2="US",
        city=None,
        address_lines=[],
    )
    candidates, fallback = CandidateIndex([entry], settings).candidates(record)
    assert fallback and candidates == []
    assert extract_iter_admissions(variants_of(record, settings), [entry], settings) == set()
    assert score_pair(record, entry, settings).score == 59.9351


@pytest.mark.parametrize("cutoff", [57.499999, 57.49999999999999, 57.5])
def test_near_cutoff_batch_scores_defer_to_extract_iter(cutoff):
    settings = ScreeningSettings(candidate_min=cutoff)
    query = "holdings aero 8gbp3vmw emond import olga"
    entry = company("entry", "sgnidloh orea wmv3pbg8 emond import olga")
    (value,) = score_rows([query], ["sgnidloh orea wmv3pbg8 emond import olga"], fuzz.WRatio)[0]
    assert value == 57.49999999999999
    index = CandidateIndex([entry], settings)
    index._scan([query])
    assert index.fallback_hits[query] == extract_iter_admissions([query], [entry], settings)


@pytest.mark.parametrize("cutoff", [1e-46, 1e-12, 1e-9, 1e-6, 0.001, 0.5, 0.999999, 1.0])
def test_fallback_admission_below_one_matches_extract_iter(cutoff):
    settings = ScreeningSettings(candidate_min=cutoff)
    entries = [
        company(f"e{n}", name)
        for n, name in enumerate(["zzzzz", "qzzzz", "zzzzq", "qqzzz", "q z", "qqqqqqqqqqqqqqqqz"])
    ]
    for left in ("qqqqq", "q", "qq qq"):
        record = party("party", left)
        candidates, fallback = CandidateIndex(entries, settings).candidates(record)
        assert fallback
        assert {row.id for row in candidates} == extract_iter_admissions(
            variants_of(record, settings), entries, settings
        )


def test_fallback_hits_stay_within_the_planned_window():
    settings = ScreeningSettings()
    # Digit-only names never block (no token of three characters, no phonetic
    # code), so every party falls back and admits many short listings.
    entries = [company(f"e{n}", f"{n % 7} {n % 5} {n % 3}") for n in range(400)]
    parties = [party(f"p{n}", f"{n % 9} {n % 4} {n % 6} {n}") for n in range(3 * FALLBACK_WINDOW)]
    index = CandidateIndex(entries, settings)
    index.plan_fallbacks(parties)
    assert len(index.planned) == len(parties)
    unplanned = CandidateIndex(entries, settings)
    for record in parties:
        candidates, fallback = index.candidates(record)
        assert fallback
        assert candidates == unplanned.candidates(record)[0]
        assert {row.id for row in candidates} == extract_iter_admissions(
            variants_of(record, settings), entries, settings
        )
        window = set(chain.from_iterable(islice(index.planned, FALLBACK_WINDOW - 1)))
        assert set(index.fallback_hits) <= window
        assert not unplanned.fallback_hits
    assert not index.planned and not index.fallback_hits
