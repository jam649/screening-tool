"""Minimal identity evidence for one comparison; no raw source or private metadata."""

from copy import deepcopy
from typing import Any


def build_state(party: dict, entry: dict, explanation: dict) -> dict[str, Any]:
    normalized = explanation.get("normalized", {})
    return {
        "party": {
            "name": party.get("name", ""),
            "alt_names": deepcopy(party.get("alt_names") or []),
            "type": party.get("party_type"),
            "country": party.get("country_iso2"),
            "city": party.get("city"),
            "region": party.get("region"),
            "postal_code": party.get("postal_code"),
            "address_lines": deepcopy(party.get("address_lines") or []),
            "normalized_name": normalized.get("compared_party"),
        },
        "listing": {
            "name": entry.get("name", ""),
            "alt_names": deepcopy(entry.get("alt_names") or []),
            "type": entry.get("entity_type"),
            "countries": deepcopy(entry.get("countries") or []),
            "addresses": deepcopy(entry.get("addresses") or []),
            "remarks": entry.get("remarks"),
            "normalized_name": normalized.get("compared_entry"),
        },
        "components": deepcopy(explanation.get("components") or {}),
    }
