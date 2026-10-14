#!/usr/bin/env python3
"""Reproduce the frozen v1 contract from an explicitly supplied public ITA bulk file.

Run: backend/.venv/bin/python fixtures/generate.py --bulk .workspace/csl-bulk-verified.json
No network calls are made. Generated party records are synthetic test data.
"""

import argparse
import copy
import csv
import hashlib
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.csl.parser import BULK_URL, ParsedCSL, canonical_hash, parse_bulk  # noqa: E402

def original_semantic_fingerprint(parsed: ParsedCSL) -> str:
    """Reproduce the frozen capture's v1 provenance, independent of current hash policy.

    This historical fingerprint uses the original raw-field matching hash. Runtime
    screening uses normalized, type-aware v2 hashes; the frozen government bytes,
    expected outcomes and capture metadata must not be rewritten to follow it.
    """
    records = []
    for entry in parsed.entries:
        fields = entry.fields()
        fields["entry_hash"] = canonical_hash(
            {key: fields[key] for key in ("name", "alt_names", "addresses", "countries", "programs")}
        )
        records.append(fields)
    return canonical_hash(records)


SEED = 20261013
COUNTRIES = [
    "US",
    "GB",
    "DE",
    "FR",
    "CA",
    "JP",
    "IN",
    "AU",
    "BR",
    "MX",
    "NL",
    "IT",
    "ES",
    "SE",
    "NO",
    "DK",
    "FI",
    "PL",
    "CZ",
    "PT",
    "IE",
    "NZ",
    "SG",
    "KR",
    "ZA",
    "AR",
    "CL",
    "CO",
    "TH",
    "MY",
]
COLUMNS = [
    "external_id",
    "name",
    "party_type",
    "role",
    "address_lines",
    "city",
    "region",
    "postal_code",
    "country",
]
COMMON = {
    "smith",
    "ali",
    "kim",
    "wang",
    "hassan",
    "ahmad",
    "zhang",
    "li",
    "chen",
    "johnson",
    "davis",
    "brown",
    "lee",
    "nguyen",
    "jones",
    "khan",
    "singh",
    "guo",
    "yang",
    "zheng",
    "choi",
}
SUFFIX = re.compile(r"(?i)\b(ltd\.?|limited|inc\.?|incorporated|corp\.?|corporation)\s*$")


def key(row):
    return row["source"], row["id"]


def kind(row):
    return "individual" if str(row.get("type", "")).casefold() == "individual" else "company"


def country(row):
    for address in row.get("addresses", []) or []:
        if address.get("country"):
            return address["country"]
    return row.get("country", "")


def mapped_party(row, external_id):
    addresses = row.get("addresses", []) or []
    address = next((value for value in addresses if value.get("country") == country(row)), {})
    return {
        "external_id": external_id,
        "name": row["name"],
        "party_type": kind(row),
        "role": "customer",
        "address_lines": address.get("address") or "",
        "city": address.get("city") or "",
        "region": address.get("state") or "",
        "postal_code": address.get("postal_code") or "",
        "country": country(row),
    }


def generate(bulk, destination):
    data = bulk.read_bytes()
    parsed = parse_bulk(data, require_envelope=True)
    entries = sorted((entry.raw for entry in parsed.entries), key=key)
    code_by_key = {key(entry.raw): entry.source_list for entry in parsed.entries}
    selected = {}
    targets = set()
    cases = []
    planted = []

    def take(predicate, amount):
        result = []
        for row in entries:
            if key(row) not in targets and predicate(row):
                targets.add(key(row))
                selected[key(row)] = row
                result.append(row)
                if len(result) == amount:
                    return result
        raise RuntimeError(f"Insufficient public entries for required category: {amount}")

    def add_case(row, category, buckets, mutate=None, below=False, removed=False):
        external_id = f"case-{category}-{1 + sum(c['category'] == category for c in cases):02d}"
        party = mapped_party(row, external_id)
        if mutate:
            mutate(party)
        planted.append(party)
        cases.append(
            {
                "external_id": external_id,
                "category": category,
                "source_list": code_by_key[key(row)],
                "csl_id": row["id"],
                "expected_buckets": buckets,
                "expected_below_candidate_min": below,
                "removed_in_v2": removed,
            }
        )

    def companies(row):
        return kind(row) == "company" and bool(country(row)) and len(row["name"].split()) >= 2

    exact = take(companies, 10)
    for row in exact:
        add_case(row, "exact", ["likely"])
    for row in take(
        lambda row: companies(row) and bool(re.search("[aeiou]", row["name"], re.IGNORECASE)), 10
    ):

        def accents(party):
            conversions = dict(zip("aeiouAEIOU", "áéíóúÁÉÍÓÚ", strict=True))
            party["name"] = "".join(conversions.get(char, char) for char in party["name"])

        add_case(row, "diacritics", ["review", "likely"], accents)
    for row in take(
        lambda row: kind(row) == "individual" and "," in row["name"] and bool(country(row)), 10
    ):

        def reorder(party):
            last, first = party["name"].split(",", 1)
            party["name"] = f"{first.strip()} {last.strip()}"

        add_case(row, "token_order", ["review", "likely"], reorder)
    for row in take(lambda row: companies(row) and bool(SUFFIX.search(row["name"])), 10):

        def suffix(party):
            word = SUFFIX.search(party["name"]).group(1).casefold().rstrip(".")
            replacement = {
                "ltd": "Limited",
                "limited": "Ltd",
                "inc": "Incorporated",
                "incorporated": "Inc",
                "corp": "Corporation",
                "corporation": "Corp",
            }[word]
            party["name"] = SUFFIX.sub(replacement, party["name"])

        add_case(row, "legal_suffix", ["likely"], suffix)
    for row in take(
        lambda row: (
            kind(row) == "individual"
            and bool(country(row))
            and bool(COMMON & set(re.findall(r"\w+", row["name"].casefold())))
        ),
        10,
    ):

        def different_country(party):
            party["country"] = next(value for value in COUNTRIES if value != party["country"])
            party["address_lines"] = "998 Invented Orchard Road"
            party["city"] = "Fictional Harbor"
            party["region"] = ""
            party["postal_code"] = "99999"

        add_case(row, "common_name_country", ["low", "review"], different_country)
    for row in take(companies, 5):
        add_case(
            row, "address_only", [], lambda party: party.update(name="ZXQJVB KTZRPF"), below=True
        )
    removed_rows = take(companies, 5)
    for row in removed_rows:
        add_case(row, "removed", ["likely"], removed=True)

    # Cover every populated source and deliberately include address-less and alias-rich records.
    for source in sorted({row["source"] for row in entries}):
        selected.setdefault(
            key(next(row for row in entries if row["source"] == source)),
            next(row for row in entries if row["source"] == source),
        )
    for predicate in (
        lambda row: not row.get("addresses"),
        lambda row: len(row.get("alt_names") or []) >= 3,
    ):
        for row in [value for value in entries if predicate(value)][:15]:
            selected.setdefault(key(row), row)
    rng = random.Random(SEED)
    remaining = [row for row in entries if key(row) not in selected]
    rng.shuffle(remaining)
    for row in remaining:
        if len(selected) >= 300:
            break
        selected[key(row)] = row
    sample_v1 = sorted(selected.values(), key=key)
    added_rows = sorted([row for row in entries if key(row) not in selected][:5], key=key)
    removed_keys = {key(row) for row in removed_rows}
    changed_keys = {key(row) for row in exact[:5]}
    sample_v2 = []
    for row in sample_v1:
        if key(row) in removed_keys:
            continue
        changed_row = copy.deepcopy(row)
        if key(row) in changed_keys:
            changed_row["alt_names"] = (changed_row.get("alt_names") or []) + [
                f"Synthetic fixture alternate alias {row['id']}"
            ]
        sample_v2.append(changed_row)
    sample_v2.extend(copy.deepcopy(added_rows))
    sample_v2.sort(key=key)

    background = []
    given = ["Rowan", "Iris", "Ellis", "Mira", "Theo", "Celia", "Nolan", "Hazel", "Avery", "Sylvia"]
    family = ["Fenwick", "Larkspur", "Brookfield", "Hollis", "Alderton", "Winslow", "Merriman"]
    business = ["Maple Finch", "Cedar Lantern", "Willow Orchard", "Juniper Meadow", "Birch Haven"]
    industries = [
        "Bicycle Repair",
        "Garden Supplies",
        "Bookbinding",
        "Ceramic Workshop",
        "Pet Grooming",
    ]
    for number in range(2000):
        individual = number % 5 == 0
        name = (
            f"{rng.choice(given)} {rng.choice(family)}"
            if individual
            else f"{rng.choice(business)} {rng.choice(industries)} {number:04d} LLC"
        )
        background.append(
            {
                "external_id": f"background-{number:04d}",
                "name": name,
                "party_type": "individual" if individual else "company",
                "role": "vendor" if number % 2 else "customer",
                "address_lines": f"{100 + number} Synthetic Orchard Avenue",
                "city": f"Example District {number % 40 + 1}",
                "region": f"Region {number % 10 + 1}",
                "postal_code": f"{10000 + number}",
                "country": COUNTRIES[number % len(COUNTRIES)],
            }
        )
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "expected").mkdir(exist_ok=True)
    for filename, value in (
        ("csl_sample_v1.json", sample_v1),
        ("csl_sample_v2.json", sample_v2),
        ("expected/hits.json", {"seed": SEED, "case_count": 60, "cases": cases}),
        (
            "expected/csl_diff.json",
            {
                "added": 5,
                "changed": 5,
                "removed": 5,
                "added_ids": [
                    {"source_list": code_by_key[key(row)], "csl_id": row["id"]}
                    for row in added_rows
                ],
                "changed_ids": [
                    {"source_list": code_by_key[key(row)], "csl_id": row["id"]} for row in exact[:5]
                ],
                "removed_ids": [
                    {"source_list": code_by_key[key(row)], "csl_id": row["id"]}
                    for row in removed_rows
                ],
            },
        ),
        (
            "provenance.json",
            {
                "source_url": BULK_URL,
                "bulk_sha256": hashlib.sha256(data).hexdigest(),
                "semantic_sha256": original_semantic_fingerprint(parsed),
                "seed": SEED,
                "sample_entries": 300,
                "background_parties": 2000,
                "planted_parties": 60,
                "populated_sources": sorted(Counter(code_by_key[key(row)] for row in sample_v1)),
                "source_list_stamps": parsed.source_list_stamps,
                "notes": [
                    "All v1 listings are unchanged public government rows.",
                    "V2 adds five other government rows, changes five synthetic aliases, "
                    "removes five rows.",
                    "All party records are synthetic test data; resemblance to real businesses "
                    "is incidental.",
                    "FSE metadata is present, but the verified bulk file contains no "
                    "current FSE listings.",
                ],
            },
        ),
    ):
        (destination / filename).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    with (destination / "parties.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(background + planted)
    files = [
        "csl_sample_v1.json",
        "csl_sample_v2.json",
        "parties.csv",
        "provenance.json",
        "expected/hits.json",
        "expected/csl_diff.json",
    ]
    hashes = {file: hashlib.sha256((destination / file).read_bytes()).hexdigest() for file in files}
    (destination / "expected/file_hashes.json").write_text(json.dumps(hashes, indent=2) + "\n")
    print(
        f"Frozen {len(sample_v1)} real listings and "
        f"{len(background) + len(planted)} synthetic parties."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bulk", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "fixtures")
    arguments = parser.parse_args()
    generate(arguments.bulk, arguments.output)
