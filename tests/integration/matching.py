#!/usr/bin/env python3
"""Explicit synthetic recall and real migrated-engine performance gates.

No network, model, SMTP, customer input, resets or existing-store writes. Setup
uses the actual CSL parser/service, CSV connector and audited apply_sync. Both
full runs use the production protected operation and immutable evidence. Run
1k/5k profiles first; 20k requires an additional explicit fixture opt-in and a
coordinator-provided constrained container. Failed workspaces remain intact.
"""

from __future__ import annotations

import argparse
import cProfile
import csv
import gzip
import hashlib
import io
import json
import os
import platform
import pstats
import random
import resource
import signal
import statistics
import sys
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet
from sqlalchemy import event, func, select

from app.config import Config
from app.connectors.csv import CSVConnector
from app.connectors.sync import apply_sync
from app.csl.parser import parse_bulk
from app.csl.service import refresh_csl
from app.database import create_database_engine, migrate, session_factory
from app.models import AuditLog, CSLEntry, Hit, Party, RunPartyEvidence, ScreeningRun, Source
from app.reports.metrics import current_screening_coverage
from app.scheduler.service import run_operation
from app.screening import engine as screening
from app.screening.candidates import CandidateIndex
from app.screening.score import bucket, score_pair
from app.screening.settings import ScreeningSettings
from app.security import validate_key_binding

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures" if (ROOT / "fixtures").is_dir() else Path("/app/fixtures")
RECALL = FIXTURES / "matching-recall"
SEED = 20261014
PUBLIC_BULK_SHA256 = "fd63c5fcfbe921972ef691785c4dd4573a783ec7425787270b25e399f2b889e3"
SOURCE = "Entity List (EL) - Bureau of Industry and Security"
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
    "CH",
    "AT",
    "BE",
    "TR",
    "AE",
]
CSV_FIELDS = [
    "external_id",
    "name",
    "party_type",
    "role",
    "address_lines",
    "city",
    "region",
    "postal_code",
    "country",
    "alt_names",
]
GIVEN = [
    "John",
    "Maria",
    "Ahmed",
    "Anna",
    "David",
    "Olga",
    "Peter",
    "Sara",
    "Nikolay",
    "Jean",
    "Mira",
    "Theo",
    "Celia",
    "Nolan",
    "Hazel",
    "Avery",
    "Sylvia",
    "Eleni",
    "Amit",
    "Ravi",
    "Yusuf",
    "Viktor",
    "Alex",
    "Iris",
]
FAMILY = [
    "Smith",
    "Martin",
    "Garcia",
    "Orlov",
    "Petrov",
    "Alvarez",
    "Sokolov",
    "Ivanov",
    "Volkova",
    "Kumar",
    "Sharma",
    "Hollis",
    "Winslow",
    "Merriman",
    "Fenwick",
    "Alderton",
    "Brookfield",
    "Larkspur",
    "Demetriou",
    "Hassan",
    "Mansour",
    "Richter",
    "Mueller",
    "Bennett",
    "Cooper",
    "Foster",
    "Hayes",
    "Nelson",
    "Parker",
    "Wright",
]
LIST_BRANDS = [
    "Volga Dynamo",
    "Baltic Meridian",
    "Danube Vector",
    "Boreal Magnetics",
    "Caspian Vertex",
    "Pacific Foundry",
    "Southern Circuit",
    "Eastern Strontium",
    "Northern Plasma",
    "Central Alloy",
    "Alpine Turbine",
    "Desert Lattice",
    "Continental Optics",
    "Atlantic Radar",
    "Delta Robotics",
    "Amber Reactor",
]
PARTY_BRANDS = [
    "Maple Finch",
    "Cedar Lantern",
    "Willow Orchard",
    "Juniper Meadow",
    "Birch Haven",
    "Hickory Sandstone",
    "Elm Valley",
    "Oak Harbor",
    "Pine Crest",
    "Aspen Ridge",
    "Rowan Brook",
    "Hazel Grove",
    "Clover Fields",
    "Silver Fern",
    "Copper Mill",
    "Meadow Lark",
]
INDUSTRIES = [
    "Electrical Components",
    "Precision Instruments",
    "Garden Supplies",
    "Ceramic Workshop",
    "Bicycle Repair",
    "Industrial Textiles",
    "Food Processing",
    "Timber Products",
    "Machinery Parts",
    "Marine Equipment",
    "Medical Supplies",
    "Laboratory Devices",
    "Packaging Materials",
    "Metal Fabrication",
    "Transport Logistics",
    "Energy Controls",
    "Optical Systems",
    "Pneumatic Valves",
    "Agricultural Tools",
    "Chemical Products",
    "Electronic Sensors",
    "Cooling Equipment",
    "Printing Services",
    "Furniture Workshop",
]
REGIONAL = [
    "Atlantic",
    "Pacific",
    "Northern",
    "Southern",
    "Eastern",
    "Western",
    "Coastal",
    "Alpine",
    "Highland",
    "Prairie",
    "Woodland",
    "Riverside",
    "Lakeside",
    "Valley",
    "Hilltop",
    "Desert",
    "Island",
    "Central",
    "Seaside",
    "Evergreen",
    "Clearwater",
    "Sunrise",
    "Sunset",
    "Mountain",
    "Redwood",
    "Meadow",
    "Brookside",
    "Sandstone",
    "Oakwood",
    "Harbor",
]


class MatchingGateFailure(ValueError):
    """Only a fixed check identifier is safe for public failure output."""


def require(condition, check):
    if not condition:
        raise MatchingGateFailure(check)


def digest(body):
    return hashlib.sha256(body).hexdigest()


def frozen_cases():
    body = (RECALL / "cases.json").read_bytes()
    manifest = json.loads((RECALL / "freeze.json").read_text())
    require(digest(body) == manifest["cases_sha256"], "recall_labels_frozen")
    controls = (RECALL / "mechanism-controls.json").read_bytes()
    require(
        digest(controls) == json.loads((RECALL / "mechanism-freeze.json").read_text())["sha256"],
        "mechanism_labels_frozen",
    )
    return json.loads(body)["cases"], json.loads(controls)["controls"]


def entry_objects(raw):
    return {
        row.csl_id: SimpleNamespace(id=row.csl_id, **row.fields())
        for row in parse_bulk(json.dumps(raw).encode()).entries
    }


def recall_report():
    cases, controls = frozen_cases()
    settings = ScreeningSettings()
    entries = entry_objects(
        [row for case in cases for row in [case["entry"], *case["distractors"]]]
    )
    index = CandidateIndex(list(entries.values()), settings)
    families = defaultdict(Counter)
    missed, negative_likely, must_find_missed = [], [], []
    candidate_positive = 0
    for case in cases:
        party = SimpleNamespace(id=case["case_id"], **case["party"])
        target = entries[case["entry"]["id"]]
        candidates, _ = index.candidates(party)
        candidate = target.id in {row.id for row in candidates}
        score = score_pair(party, target, settings).score
        stored = candidate and score >= settings.candidate_min
        candidate_positive += int(case["same_party"] and candidate)
        counters = families[case["family"]]
        counters["pairs"] += 1
        counters["candidates"] += int(candidate)
        counters["stored"] += int(stored)
        counters["review_or_likely"] += int(stored and score >= settings.review_threshold)
        if case["same_party"] and not stored:
            missed.append(case["case_id"])
        if case["must_find"] and not stored:
            must_find_missed.append(case["case_id"])
        if not case["same_party"] and bucket(score, settings) == "likely":
            negative_likely.append(case["case_id"])
        if case["expected_below_candidate_min"]:
            require(score < settings.candidate_min, "unrelated_address_below_cutoff")
    mechanisms = []
    for control in controls:
        rows = entry_objects(control["targets"] + control["distractors"])
        candidates, fallback = CandidateIndex(list(rows.values()), settings).candidates(
            SimpleNamespace(id=control["case_id"], **control["party"])
        )
        ids = {row.id for row in candidates}
        mechanisms.append(
            {
                "case_id": control["case_id"],
                "pairs": len(control["targets"]),
                "candidates": sum(row["id"] in ids for row in control["targets"]),
                "fallback": fallback,
            }
        )
    positives = [case for case in cases if case["same_party"]]
    return {
        "schema_version": 1,
        "passed": not must_find_missed
        and not negative_likely
        and all(row["candidates"] == row["pairs"] for row in mechanisms),
        "matching_policy_version": settings.scoring_snapshot()["matching_policy_version"],
        "seed": SEED,
        "labels_sha256": digest((RECALL / "cases.json").read_bytes()),
        "positive_pairs": len(positives),
        "candidate_positive": candidate_positive,
        "stored_positive": len(positives) - len(missed),
        "positive_missed": missed,
        "must_find_pairs": sum(case["must_find"] for case in cases),
        "must_find_missed": must_find_missed,
        "negative_likely": negative_likely,
        "families": dict(families),
        "mechanisms": mechanisms,
    }


def freeze_public_subset(bulk_path):
    """Reproducible public-government capture; never rewrite frozen recall labels."""
    data = bulk_path.read_bytes()
    require(digest(data) == PUBLIC_BULK_SHA256, "verified_public_capture_sha256")
    entries = parse_bulk(data).entries
    original = json.loads((FIXTURES / "csl_sample_v1.json").read_text())
    original_parsed = parse_bulk(json.dumps(original).encode()).entries
    original_keys = {entry.key for entry in original_parsed}
    by_key = {entry.key: entry.raw for entry in entries}
    require(
        all(by_key.get(entry.key) == entry.raw for entry in original_parsed),
        "original_public_sample_bytes_unchanged",
    )
    remainder = sorted(
        (entry for entry in entries if entry.key not in original_keys),
        key=lambda entry: digest(json.dumps([SEED, *entry.key], ensure_ascii=False).encode()),
    )
    selected = original + [entry.raw for entry in remainder[:24_700]]
    body = json.dumps(selected, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    compressed = gzip.compress(body, mtime=0)
    path = RECALL / "public-csl-25000.json.gz"
    manifest = RECALL / "public-csl-provenance.json"
    require(not path.exists() and not manifest.exists(), "public_subset_freeze_no_overwrite")
    path.write_bytes(compressed)
    manifest.write_text(
        json.dumps(
            {
                "source_url": "https://data.trade.gov/downloadable_consolidated_screening_list/v1/consolidated.json",
                "capture_sha256": digest(data),
                "capture_bytes": len(data),
                "capture_entries": len(entries),
                "subset_entries": len(selected),
                "original_sample_entries": len(original),
                "seed": SEED,
                "selection": (
                    "Original 300 in frozen order, then SHA-256 rank of JSON "
                    "[seed, source_list, csl_id]"
                ),
                "subset_sha256": digest(body),
                "compressed_sha256": digest(compressed),
                "subset_bytes": len(body),
                "compressed_bytes": len(compressed),
                "data_kind": (
                    "Unmodified public United States government CSL rows; no customer records"
                ),
            },
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )
    return {"passed": True, "public_entries": len(selected), "compressed_bytes": len(compressed)}


def public_entries(count):
    manifest = json.loads((RECALL / "public-csl-provenance.json").read_text())
    compressed = (RECALL / "public-csl-25000.json.gz").read_bytes()
    require(digest(compressed) == manifest["compressed_sha256"], "public_subset_compressed_frozen")
    body = gzip.decompress(compressed)
    require(digest(body) == manifest["subset_sha256"], "public_subset_rows_frozen")
    return json.loads(body)[:count]


def benchmark_inputs(party_count, entry_count, *, dense=False, synthetic_list=False):
    require(60 <= party_count <= 20_000 and 300 <= entry_count <= 25_000, "fixture_dimensions")
    rng = random.Random(SEED)
    entries = json.loads((FIXTURES / "csl_sample_v1.json").read_text())
    frozen = list(csv.DictReader(io.StringIO((FIXTURES / "parties.csv").read_text())))
    planted = [row for row in frozen if row["external_id"].startswith("case-")]
    background = [row for row in frozen if not row["external_id"].startswith("case-")]
    parties = (planted + background)[:party_count]
    if not (synthetic_list or dense):
        entries = public_entries(entry_count)
    for n in range(entry_count - len(entries)):
        individual = n % 5 == 0
        country = COUNTRIES[n % len(COUNTRIES)]
        stem = (
            f"{rng.choice(GIVEN)} {rng.choice(FAMILY)}"
            if individual
            else f"{rng.choice(LIST_BRANDS)} {rng.choice(INDUSTRIES)}"
        )
        name = stem if individual else stem + (" Group" if n % 7 == 0 else " Ltd")
        if dense and not individual:
            name = "Central Industrial " + name
        aliases = [stem + " Corporation"] if not individual and n % 4 == 0 else []
        if individual and n % 3 == 0:
            parts = name.split()
            aliases.append(" ".join(parts[-1:] + parts[:-1]))
        if n % 20 == 0:
            aliases.append(name.replace(" ", "-"))
        entries.append(
            {
                "id": f"synthetic-m9-{n:05d}",
                "source": SOURCE,
                "name": name,
                "type": "Individual" if individual else "Entity",
                "alt_names": aliases,
                "addresses": [
                    {
                        "country": country,
                        "city": f"Synthetic District {n % 60}",
                        "address": f"{n % 500 + 1} Synthetic Avenue",
                    }
                ]
                if n % 3
                else [],
                "programs": ["Explicit synthetic performance control"],
                "remarks": "Synthetic performance fixture, not a government listing.",
            }
        )
    for n in range(party_count - len(parties)):
        individual = n % 5 == 0
        base = f"{rng.choice(GIVEN)} {rng.choice(FAMILY)}"
        brand = f"{rng.choice(REGIONAL)} {rng.choice(PARTY_BRANDS)}"
        industry = rng.choice(INDUSTRIES)
        name = base if individual else f"{brand} {industry} LLC"
        alias = base if individual and n % 3 == 0 else ""
        if individual and alias:
            first, last = base.split()
            name = f"{first} {chr(65 + n % 26)} {last}"
        elif not individual and n % 7 == 0:
            alias = f"{brand} {industry} Corporation"
        if dense and not individual:
            name = "Central Industrial " + name
        # A modest planted overlap is explicit, not inferred from match output.
        if n % 100 == 0:
            target = (
                entries[300 + n % max(1, entry_count - 300)]
                if entry_count > 300
                else entries[n % 300]
            )
            name, individual = target["name"], target.get("type") == "Individual"
            alias = ""
        parties.append(
            {
                "external_id": f"synthetic-m9-party-{n:05d}",
                "name": name,
                "alt_names": alias,
                "party_type": "individual" if individual else "company",
                "role": "vendor" if n % 2 else "customer",
                "country": COUNTRIES[n % len(COUNTRIES)],
                "city": f"Synthetic District {n % 60}",
                "address_lines": f"{n % 500 + 1} Synthetic Avenue",
                "region": f"Region {n % 12}",
                "postal_code": str(10000 + n % 50000),
            }
        )
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=CSV_FIELDS)
    writer.writeheader()
    writer.writerows(parties)
    return json.dumps(entries, ensure_ascii=False, sort_keys=True).encode(), out.getvalue().encode()


@contextmanager
def instrumentation(database):
    timings = Counter()
    counts = Counter()
    candidates = []
    original = {name: getattr(screening, name) for name in ("_prepare", "_infer", "_execute")}
    original_candidates = CandidateIndex.candidates

    def wrapper(name):
        def timed(*args, **kwargs):
            start = time.perf_counter()
            try:
                result = original[name](*args, **kwargs)
                if name == "_prepare":
                    prepared, _ = result
                    counts["eligible_pairs_if_model_enabled"] = sum(
                        pair.eligible for pairs in prepared.pairs.values() for pair in pairs
                    )
                return result
            finally:
                timings[name] += time.perf_counter() - start

        return timed

    def record_candidates(self, party):
        rows, fallback = original_candidates(self, party)
        candidates.append(len(rows))
        counts["fallback_parties"] += int(fallback)
        return rows, fallback

    def sql_count(*args):
        counts["sql_statements"] += 1

    for name in original:
        setattr(screening, name, wrapper(name))
    CandidateIndex.candidates = record_candidates
    event.listen(database, "before_cursor_execute", sql_count)
    try:
        yield timings, counts, candidates
    finally:
        for name, function in original.items():
            setattr(screening, name, function)
        CandidateIndex.candidates = original_candidates
        event.remove(database, "before_cursor_execute", sql_count)


def rss_mib():
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024 if sys.platform == "darwin" else 1024)


def evidence_fingerprint(db, run_id):
    """Stream complete fixture evidence; publish its checksum, never raw rows."""
    fingerprint = hashlib.sha256()
    for model in (ScreeningRun, Hit, RunPartyEvidence):
        table = model.__table__
        query = (
            select(table)
            .where(table.c.id == run_id if model is ScreeningRun else table.c.run_id == run_id)
            .order_by(table.c.id)
        )
        for row in db.execute(query).mappings().yield_per(500):
            fingerprint.update(json.dumps(dict(row), sort_keys=True, default=str).encode())
            fingerprint.update(b"\n")
    return fingerprint.hexdigest()


def benchmark(
    workspace,
    *,
    parties,
    entries,
    dense=False,
    synthetic_list=False,
    seconds=600,
    repeat=True,
    profile=False,
):
    require(not workspace.exists(), "fresh_workspace_required_no_reset")
    workspace.mkdir(parents=True, mode=0o700)
    database_path = workspace / "screening-benchmark.db"
    key = os.environ.get("SCREENING_TOOL_SECRET_KEY") or Fernet.generate_key().decode()
    config = Config(
        secret_key=key,
        database_url="sqlite:///" + str(database_path.resolve()),
        scheduler_enabled=False,
    )
    started = time.perf_counter()
    listing_data, party_data = benchmark_inputs(
        parties, entries, dense=dense, synthetic_list=synthetic_list
    )
    database = create_database_engine(config)
    migrate(database)
    factory = session_factory(database, config)
    with factory() as db:
        validate_key_binding(db, config)
        refreshed = refresh_csl(db, data=listing_data)
        require(refreshed.version.entry_count == entries, "actual_listing_count")
        source = Source(kind="csv", label="Explicit synthetic performance book")
        db.add(source)
        db.commit()
        batch = CSVConnector(
            party_data, "synthetic-performance.csv", {key: key for key in CSV_FIELDS}
        ).full_sync()
        require(not batch.rejected and len(batch.parties) == parties, "actual_csv_validation")
        imported = apply_sync(db, source, batch)
        require(imported.added == parties, "actual_party_import")
    setup = time.perf_counter() - started
    result = {
        "validation": "synthetic-real-migrated-protected-engine",
        "seed": SEED,
        "parties": parties,
        "entries": entries,
        "dense": dense,
        "list_kind": "repetitive-synthetic-diagnostic"
        if synthetic_list or dense
        else "frozen-public-CSL-subset",
        "inputs_sha256": {"list": digest(listing_data), "parties": digest(party_data)},
        "setup_seconds": setup,
        "hardware": {"platform": platform.system(), "cpu_count": os.cpu_count()},
        "cold_definition": (
            "First screen after actual import; pure normalization cache may be warmed by setup"
        ),
        "runs": [],
    }

    timed_out = False

    def timeout(*args):
        nonlocal timed_out
        timed_out = True
        raise TimeoutError("fixture_benchmark_time_bound")

    previous_signal = signal.signal(signal.SIGALRM, timeout)
    try:
        queue_ids = None
        original_run_id = None
        original_fingerprint = None
        for name in ["cold", "repeat"] if repeat else ["cold"]:
            signal.alarm(seconds)
            before = resource.getrusage(resource.RUSAGE_SELF)
            started = time.perf_counter()
            profiler = cProfile.Profile() if profile else None
            if profiler:
                profiler.enable()
            with instrumentation(database) as (timings, counts, considered):
                outcome = run_operation(factory, config, "screen_all")
            if profiler:
                profiler.disable()
            elapsed = time.perf_counter() - started
            engine_peak = rss_mib()
            signal.alarm(0)
            if profile:
                stats = pstats.Stats(profiler)
                result["profile_enabled"] = True
                result["profile_top"] = [
                    {
                        "module": Path(file).name,
                        "line": line,
                        "function": function,
                        "calls": values[1],
                        "seconds": values[2],
                        "cumulative_seconds": values[3],
                    }
                    for (file, line, function), values in sorted(
                        stats.stats.items(), key=lambda item: -item[1][3]
                    )[:25]
                ]
            if outcome["status"] != "success":
                result.update(
                    passed=False,
                    failed_check="engine_time_bound"
                    if timed_out
                    else "protected_screen_all_success",
                )
                result["runs"].append(
                    {
                        "name": name,
                        "seconds": elapsed,
                        "engine_peak_rss_mib": engine_peak,
                        "phase_seconds": dict(timings),
                        "measurements": dict(counts),
                        "parties_candidates_completed": len(considered),
                        "candidates_considered": sum(considered),
                        "operation_status": outcome["status"],
                    }
                )
                (workspace / "report.json").write_text(
                    json.dumps(result, sort_keys=True, indent=2) + "\n"
                )
                raise MatchingGateFailure(result["failed_check"])
            run_ids = outcome.get("outcome", {}).get("run_ids", [])
            require(
                outcome["status"] == "success" and len(run_ids) == 1,
                "protected_screen_all_success",
            )
            after = resource.getrusage(resource.RUSAGE_SELF)
            with factory() as db:
                run = db.get(ScreeningRun, run_ids[0])
                scope = run.settings_snapshot["scope"]
                require(
                    run.parties_screened == parties
                    and len(scope["party_hashes"]) == parties
                    and len(scope["entry_hashes"]) == entries,
                    "actual_original_scope",
                )
                require(
                    scope["party_hashes"]
                    == dict(db.execute(select(Party.id, Party.record_hash)).all())
                    and scope["entry_hashes"]
                    == dict(db.execute(select(CSLEntry.id, CSLEntry.entry_hash)).all()),
                    "scope_hashes_not_reconstructed",
                )
                evidence = db.scalar(
                    select(func.count())
                    .select_from(RunPartyEvidence)
                    .where(RunPartyEvidence.run_id == run.id)
                )
                require(evidence == parties, "one_actual_evidence_row_per_party")
                zero_hits = db.scalar(
                    select(func.count())
                    .select_from(RunPartyEvidence)
                    .where(RunPartyEvidence.run_id == run.id, RunPartyEvidence.hits_recorded == 0)
                )
                require(
                    run.settings_snapshot["model"]["provider"] == "none"
                    and run.settings_snapshot["adjudication_summary"]["calls"] == 0,
                    "actual_disabled_model_no_adjudications",
                )
                require(
                    db.scalar(
                        select(func.count())
                        .select_from(Hit)
                        .where(Hit.run_id == run.id, Hit.model_prob.is_not(None))
                    )
                    == 0,
                    "no_model_calls",
                )
                read_started = time.perf_counter()
                anchors = {hit.id for hit in screening.queue_hits(db)}
                queue_seconds = time.perf_counter() - read_started
                read_started = time.perf_counter()
                coverage = current_screening_coverage(db)
                coverage_seconds = time.perf_counter() - read_started
                require(
                    len(coverage) == parties and all(coverage.values()),
                    "all_current_participants_proven",
                )
                if queue_ids is not None:
                    require(
                        run.hits_new == 0 and anchors == queue_ids, "repeat_no_new_queue_anchor"
                    )
                    require(
                        evidence_fingerprint(db, original_run_id) == original_fingerprint,
                        "original_hit_run_party_evidence_immutable",
                    )
                else:
                    original_run_id = run.id
                    original_fingerprint = evidence_fingerprint(db, run.id)
                queue_ids = anchors
                hits = db.scalar(select(func.count()).select_from(Hit).where(Hit.run_id == run.id))
                audits = db.scalar(select(func.count()).select_from(AuditLog))
                result["runs"].append(
                    {
                        "name": name,
                        "seconds": elapsed,
                        "cpu_seconds": after.ru_utime
                        + after.ru_stime
                        - before.ru_utime
                        - before.ru_stime,
                        "engine_peak_rss_mib": engine_peak,
                        "full_gate_peak_rss_mib": rss_mib(),
                        "peak_includes_setup": True,
                        "phase_seconds": dict(timings),
                        "measurements": dict(counts),
                        "candidates": {
                            "total": sum(considered),
                            "p50": statistics.median(considered),
                            "p95": sorted(considered)[int(0.95 * (len(considered) - 1))],
                            "max": max(considered),
                        },
                        "hits_recorded": hits,
                        "hits_new": run.hits_new,
                        "queue_size": len(anchors),
                        "audit_rows_total": audits,
                        "party_evidence_rows": evidence,
                        "zero_hit_evidence_rows": zero_hits,
                        "original_evidence_sha256": original_fingerprint,
                        "queue_read_seconds": queue_seconds,
                        "coverage_read_seconds": coverage_seconds,
                        "db_bytes": database_path.stat().st_size,
                        "wal_bytes": database_path.with_name(database_path.name + "-wal")
                        .stat()
                        .st_size
                        if database_path.with_name(database_path.name + "-wal").exists()
                        else 0,
                    }
                )
                (workspace / "report.json").write_text(
                    json.dumps(result, sort_keys=True, indent=2) + "\n"
                )
        result["passed"] = True
        result["resource_targets"] = {
            "five_minute_full_run_met": all(run["seconds"] <= 300 for run in result["runs"]),
            "ten_minute_bound_met": all(run["seconds"] <= 600 for run in result["runs"]),
            "process_peak_below_3gib": rss_mib() < 3072,
        }
        (workspace / "report.json").write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
        return result
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_signal)
        database.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--recall", action="store_true")
    mode.add_argument("--fixture-benchmark", action="store_true")
    mode.add_argument("--freeze-public-list", type=Path)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--parties", type=int, default=1000)
    parser.add_argument("--entries", type=int, default=25_000)
    parser.add_argument("--allow-20k-fixture", action="store_true")
    parser.add_argument("--dense", action="store_true")
    parser.add_argument("--synthetic-list", action="store_true")
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--cold-only", action="store_true")
    parser.add_argument("--profile", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.freeze_public_list:
            result = freeze_public_subset(args.freeze_public_list)
        elif args.recall:
            result = recall_report()
        else:
            require(args.workspace is not None, "explicit_new_workspace_required")
            require(args.parties <= 5000 or args.allow_20k_fixture, "large_fixture_requires_opt_in")
            result = benchmark(
                args.workspace,
                parties=args.parties,
                entries=args.entries,
                dense=args.dense,
                synthetic_list=args.synthetic_list,
                seconds=args.seconds,
                repeat=not args.cold_only,
                profile=args.profile,
            )
        print(json.dumps(result, sort_keys=True))
        if result.get("passed") is False:
            raise SystemExit(1)
    except Exception as error:  # noqa: BLE001 -- no input values/tracebacks in gate failures
        print(
            json.dumps(
                {
                    "passed": False,
                    "failed_check": str(error)
                    if isinstance(error, MatchingGateFailure)
                    else "matching_gate_execution_failed",
                    "error_code": type(error).__name__,
                }
            )
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
