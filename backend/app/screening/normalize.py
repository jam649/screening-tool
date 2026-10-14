"""Shared, deterministic normalization for connectors and both sides of matching."""

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

import pycountry
from text_unidecode import unidecode

from app.csl.parser import canonical_hash

DEFAULT_LEGAL_SUFFIXES = (
    "inc",
    "incorporated",
    "llc",
    "ltd",
    "limited",
    "corp",
    "corporation",
    "co",
    "company",
    "gmbh",
    "ag",
    "sa",
    "sarl",
    "bv",
    "pty",
    "llp",
    "plc",
    "ooo",
)


def normalize_text(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "").casefold()
    return " ".join("".join(c if c.isalnum() else " " for c in text).split())


@dataclass(frozen=True)
class NormalizedName:
    original: str
    unicode: str
    ascii: str
    tokens: tuple[str, ...]
    variants: tuple[str, ...]


def normalize_name(
    value: str, party_type: str = "company", legal_suffixes=DEFAULT_LEGAL_SUFFIXES
) -> NormalizedName:
    suffixes = {normalize_text(suffix) for suffix in legal_suffixes}

    def clean(text: str) -> str:
        tokens = normalize_text(text).split()
        if party_type != "individual":
            while len(tokens) > 1 and tokens[-1] in suffixes:
                tokens.pop()
        return " ".join(tokens)

    native = clean(value)
    ascii_form = clean(unidecode(unicodedata.normalize("NFKC", value)))
    variants = [native, ascii_form]
    if party_type == "individual":
        for text in (native, ascii_form):
            tokens = text.split()
            if len(tokens) > 1:
                variants.extend(
                    (" ".join(tokens[1:] + tokens[:1]), " ".join(tokens[-1:] + tokens[:-1]))
                )
    return NormalizedName(
        value,
        native,
        ascii_form,
        tuple(ascii_form.split()),
        tuple(dict.fromkeys(text for text in variants if text)),
    )


def normalize_country(value: str | None, *, strict: bool = False) -> str | None:
    if not value or not value.strip():
        return None
    try:
        return pycountry.countries.lookup(value.strip()).alpha_2
    except LookupError:
        if strict:
            raise ValueError(f"Unrecognized country: {value}") from None
        return None


def normalize_party_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Hash inputs exclude source metadata, contact information, role and raw payload.

    Hash normalization retains legal suffixes and original Unicode alongside ASCII:
    a meaningful name edit invalidates a remembered decision even if scoring strips it.
    """

    def name(value: str) -> dict[str, str]:
        normalized = normalize_name(value, legal_suffixes=())
        return {"unicode": normalized.unicode, "ascii": normalized.ascii}

    return {
        "name": name(fields["name"]),
        "alt_names": sorted(
            (name(value) for value in fields.get("alt_names", [])),
            key=lambda value: (value["unicode"], value["ascii"]),
        ),
        "address_lines": [normalize_text(value) for value in fields.get("address_lines", [])],
        "city": normalize_text(fields.get("city")),
        "region": normalize_text(fields.get("region")),
        "postal_code": re.sub(r"\s+", "", normalize_text(fields.get("postal_code"))),
        "country_iso2": normalize_country(fields.get("country_iso2") or fields.get("country")),
    }


def party_record_hash(fields: dict[str, Any]) -> str:
    return canonical_hash(normalize_party_fields(fields))


party_hash = party_record_hash
