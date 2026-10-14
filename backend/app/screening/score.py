"""Section 7.3 scoring with original and normalized pairwise evidence."""

from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from typing import Any

from rapidfuzz import fuzz

from app.csl.parser import canonical_entity_type
from app.screening.normalize import (
    NormalizedName,
    normalize_country,
    normalize_name,
    normalize_text,
)
from app.screening.settings import ScreeningSettings


def entry_type(entry: Any) -> str | None:
    kind = canonical_entity_type(entry.entity_type)
    return kind if kind in {"company", "individual"} else None


def names(record: Any, settings: ScreeningSettings, *, party: bool) -> list[NormalizedName]:
    kind = record.party_type if party else entry_type(record) or "company"
    return [
        normalize_name(value, kind, settings.legal_suffixes)
        for value in [record.name, *(record.alt_names or [])]
    ]


def name_similarity(left: str, right: str, weights: dict[str, float]) -> tuple[float, dict]:
    components = {
        "token_set": fuzz.token_set_ratio(left, right),
        "token_sort": fuzz.token_sort_ratio(left, right),
        "partial": fuzz.partial_ratio(left, right),
    }
    return sum(components[key] * weights[key] for key in components), components


@dataclass(frozen=True)
class ScoredPair:
    score: float
    explanation: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PartyFeatures:
    """Normalized values a party contributes to every one of its comparisons."""

    names: tuple[NormalizedName, ...]
    party_type: str
    country: str | None
    city: str
    address: frozenset[str]


@dataclass(frozen=True, slots=True)
class EntryFeatures:
    """Normalized values a listing contributes to every one of its comparisons."""

    names: tuple[NormalizedName, ...]
    kind: str | None
    countries: frozenset[str]
    cities: frozenset[str]
    addresses: tuple[frozenset[str], ...]
    address_tokens: frozenset[str]


def party_features(party: Any, settings: ScreeningSettings) -> PartyFeatures:
    return PartyFeatures(
        tuple(names(party, settings, party=True)),
        party.party_type,
        normalize_country(party.country_iso2),
        normalize_text(party.city),
        frozenset(
            token for line in party.address_lines or [] for token in normalize_text(line).split()
        ),
    )


def entry_features(
    entry: Any, settings: ScreeningSettings, entry_names: list[NormalizedName] | None = None
) -> EntryFeatures:
    addresses = []
    for address in entry.addresses or []:
        value = address.get("address", "") or ""
        if isinstance(value, list):
            value = " ".join(value)
        addresses.append(frozenset(normalize_text(value).split()))
    return EntryFeatures(
        tuple(names(entry, settings, party=False) if entry_names is None else entry_names),
        entry_type(entry),
        frozenset(code for value in entry.countries or [] if (code := normalize_country(value))),
        frozenset(
            normalize_text(address.get("city"))
            for address in entry.addresses or []
            if address.get("city")
        ),
        tuple(addresses),
        frozenset().union(*addresses),
    )


def _context_deltas(
    party: PartyFeatures, entry: EntryFeatures, weights: dict[str, float]
) -> tuple[float, float, float, float, frozenset[str]]:
    country_delta = (
        (
            weights["country_match"]
            if party.country in entry.countries
            else weights["country_mismatch"]
        )
        if party.country and entry.countries
        else 0
    )
    city_delta = weights["city_match"] if party.city and party.city in entry.cities else 0
    best_address_tokens: frozenset[str] = frozenset()
    address_ratio = 0.0
    # Disjoint token sets give a zero ratio, which never replaces the running best.
    overlapping = not party.address.isdisjoint(entry.address_tokens)
    for tokens in entry.addresses if overlapping else ():
        if party.address.isdisjoint(tokens):
            continue
        overlap = party.address & tokens
        union = party.address | tokens
        ratio = len(overlap) / len(union) if union else 0
        if ratio > address_ratio:
            address_ratio, best_address_tokens = ratio, overlap
    address_delta = weights["address_max"] * address_ratio
    type_delta = weights["entity_mismatch"] if entry.kind and entry.kind != party.party_type else 0
    return country_delta, city_delta, address_delta, type_delta, best_address_tokens


def _short_name(party: PartyFeatures) -> bool:
    primary_tokens = party.names[0].tokens
    return len(primary_tokens) == 1 and len(primary_tokens[0]) < 5


def _final(name, country, city, address, entity_type, short) -> float:
    return round(max(0.0, min(100.0, name + country + city + address + entity_type + short)), 4)


def below_minimum(
    party: PartyFeatures,
    entries: list[EntryFeatures],
    name_ceilings: list[float | None],
    settings: ScreeningSettings,
) -> list[bool]:
    """Whether each entry provably scores below candidate_min against the party.

    A name ceiling bounds every variant comparison's name score. Context
    deltas are exact and summed in score_pair's order; float addition, clamping
    and rounding are monotone, so each resulting ceiling is never below the
    actual score. Only the alias-dependent short-name delta is bounded.
    """
    weights, minimum = settings.score_weights, settings.candidate_min
    short = max(weights["short_name"], 0) if _short_name(party) else 0
    return [
        ceiling is not None
        and _final(ceiling, *_context_deltas(party, entry, weights)[:4], short) < minimum
        for entry, ceiling in zip(entries, name_ceilings, strict=True)
    ]


def score_pair(
    party: Any,
    entry: Any,
    settings: ScreeningSettings,
    *,
    explanation_minimum: float | None = None,
    features: tuple[PartyFeatures, EntryFeatures] | None = None,
    name_bound: Callable[[str, str], float] | None = None,
) -> ScoredPair:
    party_side, entry_side = features or (
        party_features(party, settings),
        entry_features(entry, settings),
    )
    party_names, entry_names = party_side.names, entry_side.names
    best: tuple[float, dict, NormalizedName, NormalizedName, str, str] = (
        -1.0,
        {},
        party_names[0],
        entry_names[0],
        "",
        "",
    )
    alt_matches = False
    comparisons = [
        (pi, pn, en, pv, ev)
        for pi, pn in enumerate(party_names)
        for en in entry_names
        for pv in pn.variants
        for ev in en.variants
    ]
    order: Iterable[int] = range(len(comparisons))
    ceilings: list[float] = []
    if name_bound is not None:
        ceilings = [name_bound(pv, ev) for _, _, _, pv, ev in comparisons]
        order = sorted(order, key=ceilings.__getitem__, reverse=True)
    scored: dict[int, tuple[float, dict]] = {}
    maximum = -1.0
    for index in order:
        pi, pn, en, pv, ev = comparisons[index]
        # Highest ceilings first: a ceiling below the running maximum (and below
        # the alias threshold while that is still open) can neither reach the
        # maximum, and so its tie-break, nor establish alt_matches.
        if ceilings and ceilings[index] < (
            maximum if pi == 0 or alt_matches else min(maximum, settings.review_threshold)
        ):
            continue
        scored[index] = score, _ = name_similarity(pv, ev, settings.score_weights)
        if pi > 0 and score >= settings.review_threshold:
            alt_matches = True
        maximum = max(maximum, score)
    # Every comparison reaching the maximum was scored; the first in nested
    # order wins, exactly as the strict comparison of a sequential scan.
    for index in sorted(scored):
        score, components = scored[index]
        if score > best[0]:
            best = score, components, *comparisons[index][1:]
    name_score, ratios, pn, en, pv, ev = best
    if name_score < 0:
        name_score = 0
    weights = settings.score_weights
    country_delta, city_delta, address_delta, type_delta, best_address_tokens = _context_deltas(
        party_side, entry_side, weights
    )
    short_delta = weights["short_name"] if _short_name(party_side) and not alt_matches else 0
    components = {
        "name": name_score,
        "name_ratios": ratios,
        "country": country_delta,
        "city": city_delta,
        "address": address_delta,
        "entity_type": type_delta,
        "short_name": short_delta,
    }
    final = _final(name_score, country_delta, city_delta, address_delta, type_delta, short_delta)
    # Every candidate is fully scored. Below-cutoff comparisons are never
    # persisted, so the engine can skip constructing their unused evidence.
    if explanation_minimum is not None and final < explanation_minimum:
        return ScoredPair(final, {})
    explanation = {
        "components": components,
        "matched_tokens": {
            "name": sorted(set(pv.split()) & set(ev.split())),
            "address": sorted(best_address_tokens),
        },
        "best_aliases": {"party": pn.original, "entry": en.original},
        "normalized": {
            "party": asdict(pn),
            "entry": asdict(en),
            "compared_party": pv,
            "compared_entry": ev,
        },
        "country": {"party": party_side.country, "entry": sorted(entry_side.countries)},
        "city": {"party": party_side.city or None, "entry": sorted(entry_side.cities)},
        "entity_type": {"party": party_side.party_type, "entry": entry_side.kind},
    }
    return ScoredPair(final, explanation)


def bucket(confidence: float, settings: ScreeningSettings, det_score: float | None = None) -> str:
    if confidence >= settings.likely_threshold or (
        det_score is not None and det_score >= settings.likely_threshold
    ):
        return "likely"
    return "review" if confidence >= settings.review_threshold else "low"
