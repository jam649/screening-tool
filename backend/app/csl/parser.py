"""Validated parsing of ITA's public bulk CSL export.

Contract evidence: docs/research/vendor-contracts.md, verified 2026-10-13.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from app.canonical import HASH_RULE_VERSION, canonical_hash  # noqa: F401 -- public compatibility
from app.screening.normalize import normalize_country, normalize_text, normalized_text_pair

BULK_URL = "https://data.trade.gov/downloadable_consolidated_screening_list/v1/consolidated.json"
SOURCES_URL = "https://data.trade.gov/consolidated_screening_list/v1/sources"
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024

SOURCE_CODES = {
    "Capta List (CAP) - Treasury Department": "CAP",
    (
        "Non-SDN Chinese Military-Industrial Complex Companies List (CMIC) - Treasury Department"
    ): "CMIC",
    "Denied Persons List (DPL) - Bureau of Industry and Security": "DPL",
    "ITAR Debarred (DTC) - State Department": "DTC",
    "Entity List (EL) - Bureau of Industry and Security": "EL",
    "Foreign Sanctions Evaders (FSE) - Treasury Department": "FSE",
    "Nonproliferation Sanctions (ACN) - State Department": "ACN",
    "Non-SDN Menu-Based Sanctions List (NS-MBS List) - Treasury Department": "NS-MBS",
    "Military End User (MEU) List - Bureau of Industry and Security": "MEU",
    "Palestinian Legislative Council List (PLC) - Treasury Department": "PLC",
    "Specially Designated Nationals (SDN) - Treasury Department": "SDN",
    "Sectoral Sanctions Identifications List (SSI) - Treasury Department": "SSI",
    "Unverified List (UVL) - Bureau of Industry and Security": "UVL",
}


class CSLValidationError(ValueError):
    """No database changes may be made for a rejected download."""


def canonical_entity_type(value: str | None) -> str | None:
    """The hash and type penalty share semantic aliases; retain raw type separately."""
    kind = normalize_text(value) if value else None
    if kind in {"individual", "person"}:
        return "individual"
    if kind in {"entity", "company", "organization", "organisation"}:
        return "company"
    return kind


def csl_entry_hash(fields: dict[str, Any]) -> str:
    def country(value: str | None) -> str:
        return normalize_country(value) or normalize_text(value)

    def unique_sorted(values: list[Any]) -> list[Any]:
        return [
            value
            for _, value in sorted(
                {
                    json.dumps(value, ensure_ascii=False, sort_keys=True): value for value in values
                }.items()
            )
        ]

    addresses = [
        {
            **{
                key: normalized_text_pair(address.get(key))
                for key in ("address", "city", "state", "postal_code")
            },
            "country": country(address.get("country")),
        }
        for address in fields.get("addresses") or []
    ]
    return canonical_hash(
        {
            "name": normalized_text_pair(fields.get("name")),
            "alt_names": unique_sorted(
                [normalized_text_pair(name) for name in fields.get("alt_names") or []]
            ),
            "addresses": unique_sorted(addresses),
            "countries": sorted({country(value) for value in fields.get("countries") or []}),
            "programs": unique_sorted(
                [normalized_text_pair(program) for program in fields.get("programs") or []]
            ),
            "entity_type": canonical_entity_type(fields.get("entity_type")),
        }
    )


def safe_url(value: Any) -> str | None:
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        return None
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme.lower() in {"http", "https"} and parsed.hostname and not parsed.username:
            return value.strip()
    except ValueError:
        pass
    return None


def _string(value: Any, field: str, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise CSLValidationError(
            f"Listing {field} must be {'a nonempty' if required else 'a'} string"
        )
    return value.strip()


def _strings(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise CSLValidationError(f"Listing {field} must be a string list or null")
    return sorted({item.strip() for item in value if item.strip()})


@dataclass(frozen=True)
class ParsedEntry:
    csl_id: str
    source_list: str
    name: str
    alt_names: list[str]
    addresses: list[dict[str, Any]]
    countries: list[str]
    programs: list[str]
    remarks: str | None
    entity_type: str | None
    fr_notice_url: str | None
    source_list_url: str | None
    raw: dict[str, Any]
    entry_hash: str

    @property
    def key(self) -> tuple[str, str]:
        return self.source_list, self.csl_id

    def fields(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in self.__dataclass_fields__}


@dataclass(frozen=True)
class ParsedCSL:
    entries: list[ParsedEntry]
    file_sha256: str
    semantic_sha256: str
    source_list_stamps: dict[str, Any]


def _entry(row: Any) -> ParsedEntry:
    if not isinstance(row, dict):
        raise CSLValidationError("Every listing must be an object")
    csl_id = _string(row.get("id"), "id", required=True)
    source = _string(row.get("source"), "source", required=True)
    name = _string(row.get("name"), "name", required=True)
    assert csl_id is not None and source is not None and name is not None
    if len(csl_id) > 300:
        raise CSLValidationError("Listing id exceeds 300 characters")
    source_code = SOURCE_CODES.get(source, source)
    if source_code not in SOURCE_CODES.values() and not re.fullmatch(
        r"[A-Z][A-Z0-9-]{1,15}", source
    ):
        raise CSLValidationError("Unknown CSL source; update the verified source mapping")
    addresses = row.get("addresses")
    if addresses is None:
        addresses = []
    if not isinstance(addresses, list) or any(
        not isinstance(address, dict) for address in addresses
    ):
        raise CSLValidationError("Listing addresses must be an object list")
    normalized_addresses = []
    countries = _strings(row.get("countries"), "countries")
    country = _string(row.get("country"), "country")
    if country:
        countries.append(country)
    for address in addresses:
        normalized = {
            key: _string(address.get(key), f"address.{key}")
            for key in ("address", "city", "state", "postal_code", "country")
        }
        if normalized["country"]:
            countries.append(normalized["country"])
        normalized_addresses.append(normalized)
    # Residency and citizenship are different facts: only published residency countries
    # are matching countries. Citizenship/nationality remain available in the raw evidence.
    countries = sorted({country.upper() for country in countries})
    aliases = _strings(row.get("alt_names"), "alt_names")
    programs = _strings(row.get("programs"), "programs")
    normalized_addresses.sort(key=lambda address: json.dumps(address, sort_keys=True))
    fields: dict[str, Any] = {
        "name": name,
        "alt_names": aliases,
        "addresses": normalized_addresses,
        "countries": countries,
        "programs": programs,
    }
    entity_type = _string(row.get("type"), "type")
    return ParsedEntry(
        csl_id=csl_id,
        source_list=source_code,
        **fields,
        remarks=_string(row.get("remarks"), "remarks"),
        entity_type=entity_type.casefold() if entity_type else None,
        fr_notice_url=safe_url(row.get("federal_register_notice")),
        source_list_url=safe_url(row.get("source_list_url"))
        or safe_url(row.get("source_information_url")),
        raw=row,
        entry_hash=csl_entry_hash(fields | {"entity_type": entity_type}),
    )


def parse_bulk(data: bytes, *, require_envelope: bool = False) -> ParsedCSL:
    if not data or len(data) > MAX_DOWNLOAD_BYTES:
        raise CSLValidationError("CSL download is empty or exceeds the size limit")
    try:

        def reject_constant(value: str) -> None:
            raise CSLValidationError("Nonfinite JSON numbers are not accepted")

        def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise CSLValidationError("Duplicate JSON object keys are not accepted")
                result[key] = value
            return result

        payload = json.loads(data, parse_constant=reject_constant, object_pairs_hook=unique_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise CSLValidationError("CSL download is not valid UTF-8 JSON") from exc
    stamps: dict[str, Any] = {}
    if isinstance(payload, dict):
        rows = payload.get("results")
        total = payload.get("total")
        if not isinstance(rows, list) or type(total) is not int or total != len(rows):
            raise CSLValidationError("CSL envelope total must equal the complete result count")
        if type(payload.get("offset", 0)) is not int or payload.get("offset", 0) != 0:
            raise CSLValidationError("CSL envelope must start at offset zero")
        sources = payload.get("sources_used", [])
        if not isinstance(sources, list):
            raise CSLValidationError("CSL sources_used must be a list")
        for stamp in sources:
            if not isinstance(stamp, dict) or not isinstance(stamp.get("source"), str):
                raise CSLValidationError("Invalid CSL source metadata")
            stamp_source = str(stamp["source"])
            code = SOURCE_CODES.get(stamp_source, stamp_source)
            if code in stamps:
                raise CSLValidationError("Duplicate CSL source metadata")
            stamps[code] = stamp
    elif isinstance(payload, list) and not require_envelope:
        rows = payload
    else:
        raise CSLValidationError("CSL download must contain the official results envelope")
    if not rows:
        raise CSLValidationError("Refusing an empty CSL list")
    entries = [_entry(row) for row in rows]
    ids = [entry.key for entry in entries]
    if len(set(ids)) != len(ids):
        raise CSLValidationError("Duplicate CSL listing source/id pairs are not accepted")
    entries.sort(key=lambda entry: entry.key)
    # Include all published evidence in version identity, while entry_hash covers only
    # screening fields. Remarks/link changes retain a new version without re-screening.
    semantic = canonical_hash(
        [{**entry.fields(), "entry_hash": entry.entry_hash} for entry in entries]
    )
    return ParsedCSL(entries, hashlib.sha256(data).hexdigest(), semantic, stamps)
