"""Section 7.3 scoring with original and normalized pairwise evidence."""

from dataclasses import asdict, dataclass
from typing import Any

from rapidfuzz import fuzz

from app.screening.normalize import (
    NormalizedName,
    normalize_country,
    normalize_name,
    normalize_text,
)
from app.screening.settings import ScreeningSettings


def entry_type(entry: Any) -> str | None:
    value = (entry.entity_type or "").casefold()
    if value in {"individual", "person"}:
        return "individual"
    if value in {"entity", "company", "organization", "organisation"}:
        return "company"
    return None


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


def score_pair(party: Any, entry: Any, settings: ScreeningSettings) -> ScoredPair:
    party_names, entry_names = (
        names(party, settings, party=True),
        names(entry, settings, party=False),
    )
    best: tuple[float, dict, NormalizedName, NormalizedName, str, str] = (
        -1.0,
        {},
        party_names[0],
        entry_names[0],
        "",
        "",
    )
    alt_matches = False
    for pi, pn in enumerate(party_names):
        for en in entry_names:
            for pv in pn.variants:
                for ev in en.variants:
                    score, components = name_similarity(pv, ev, settings.score_weights)
                    if pi > 0 and score >= settings.review_threshold:
                        alt_matches = True
                    if score > best[0]:
                        best = score, components, pn, en, pv, ev
    name_score, ratios, pn, en, pv, ev = best
    if name_score < 0:
        name_score = 0
    weights = settings.score_weights
    country = normalize_country(party.country_iso2)
    countries = {code for value in entry.countries or [] if (code := normalize_country(value))}
    country_delta = (
        (weights["country_match"] if country in countries else weights["country_mismatch"])
        if country and countries
        else 0
    )
    cities = {
        normalize_text(address.get("city"))
        for address in entry.addresses or []
        if address.get("city")
    }
    city = normalize_text(party.city)
    city_delta = weights["city_match"] if city and city in cities else 0
    party_address = {
        token for line in party.address_lines or [] for token in normalize_text(line).split()
    }
    best_address_tokens: set[str] = set()
    address_ratio = 0.0
    for address in entry.addresses or []:
        value = address.get("address", "") or ""
        if isinstance(value, list):
            value = " ".join(value)
        tokens = set(normalize_text(value).split())
        overlap = party_address & tokens
        ratio = len(overlap) / len(party_address | tokens) if party_address | tokens else 0
        if ratio > address_ratio:
            address_ratio, best_address_tokens = ratio, overlap
    address_delta = weights["address_max"] * address_ratio
    kind = entry_type(entry)
    type_delta = weights["entity_mismatch"] if kind and kind != party.party_type else 0
    primary_tokens = party_names[0].tokens
    short_delta = (
        weights["short_name"]
        if len(primary_tokens) == 1 and len(primary_tokens[0]) < 5 and not alt_matches
        else 0
    )
    components = {
        "name": name_score,
        "name_ratios": ratios,
        "country": country_delta,
        "city": city_delta,
        "address": address_delta,
        "entity_type": type_delta,
        "short_name": short_delta,
    }
    final = round(
        max(
            0.0,
            min(
                100.0,
                name_score + country_delta + city_delta + address_delta + type_delta + short_delta,
            ),
        ),
        4,
    )
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
        "country": {"party": country, "entry": sorted(countries)},
        "city": {"party": city or None, "entry": sorted(cities)},
        "entity_type": {"party": party.party_type, "entry": kind},
    }
    return ScoredPair(final, explanation)


def bucket(confidence: float, settings: ScreeningSettings, det_score: float | None = None) -> str:
    if confidence >= settings.likely_threshold or (
        det_score is not None and det_score >= settings.likely_threshold
    ):
        return "likely"
    return "review" if confidence >= settings.review_threshold else "low"
