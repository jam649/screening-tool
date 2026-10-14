"""The frozen dataset is a public input contract, not a score-tuning output."""

import hashlib
import json
from collections import Counter
from pathlib import Path

from sqlalchemy import select

from app.connectors.csv import CSVConnector
from app.connectors.sync import sync_csv
from app.csl.parser import parse_bulk
from app.csl.service import refresh_csl
from app.models import CSLEntry, Disposition, Hit, Party, ScreeningTask, Source
from app.screening.engine import process_pending_tasks, queue_hits, run_screening

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
MAPPING = {
    key: key
    for key in [
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
}


def test_frozen_counts_sources_types_and_expected_diff():
    v1 = parse_bulk((FIXTURES / "csl_sample_v1.json").read_bytes())
    v2 = parse_bulk((FIXTURES / "csl_sample_v2.json").read_bytes())
    assert len(v1.entries) == len(v2.entries) == 300
    assert {row.source_list for row in v1.entries} == {
        "ACN",
        "CAP",
        "CMIC",
        "DPL",
        "DTC",
        "EL",
        "MEU",
        "NS-MBS",
        "PLC",
        "SDN",
        "SSI",
        "UVL",
    }
    assert any(entry.entity_type == "individual" for entry in v1.entries)
    assert any(entry.addresses for entry in v1.entries) and any(
        not entry.addresses for entry in v1.entries
    )
    assert any(len(entry.alt_names) > 1 for entry in v1.entries)
    old = {row.key: row.entry_hash for row in v1.entries}
    new = {row.key: row.entry_hash for row in v2.entries}
    expected = json.loads((FIXTURES / "expected/csl_diff.json").read_text())
    assert len(new.keys() - old.keys()) == expected["added"] == 5
    assert len(old.keys() - new.keys()) == expected["removed"] == 5
    assert sum(old[key] != new[key] for key in old.keys() & new.keys()) == expected["changed"] == 5
    batch = CSVConnector(
        (FIXTURES / "parties.csv").read_bytes(), "parties.csv", MAPPING
    ).full_sync()
    assert len(batch.parties) == 2060 and not batch.rejected
    assert (
        len({row.country_iso2 for row in batch.parties if row.external_id.startswith("background")})
        == 30
    )
    cases = json.loads((FIXTURES / "expected/hits.json").read_text())
    assert cases["case_count"] == len(cases["cases"]) == 60
    assert Counter(row["category"] for row in cases["cases"]) == {
        "exact": 10,
        "diacritics": 10,
        "token_order": 10,
        "legal_suffix": 10,
        "common_name_country": 10,
        "address_only": 5,
        "removed": 5,
    }
    provenance = json.loads((FIXTURES / "provenance.json").read_text())
    assert (
        "FSE" in provenance["source_list_stamps"] and "FSE" not in provenance["populated_sources"]
    )
    assert len(provenance["bulk_sha256"]) == 64


def test_generator_reproduces_frozen_files_when_bulk_is_available(tmp_path):
    """Compare byte identity without requiring a live download in CI."""
    import importlib.util

    bulk = FIXTURES.parent / ".workspace/csl-bulk-verified.json"
    if not bulk.is_file():
        # The lasting fixture hashes still detect modifications in CI. This test is
        # an additional local reproducibility check, not a dependency on private data.
        import pytest

        pytest.skip("Original public bulk file is not retained in CI")
    specification = importlib.util.spec_from_file_location(
        "fixture_generator", FIXTURES / "generate.py"
    )
    generator = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(generator)
    generator.generate(bulk, tmp_path)
    for relative in [
        "csl_sample_v1.json",
        "csl_sample_v2.json",
        "parties.csv",
        "provenance.json",
        "expected/hits.json",
        "expected/csl_diff.json",
    ]:
        assert (tmp_path / relative).read_bytes() == (FIXTURES / relative).read_bytes()


def test_fixture_hash_manifest():
    manifest = json.loads((FIXTURES / "expected/file_hashes.json").read_text())
    for filename, expected_hash in manifest.items():
        assert hashlib.sha256((FIXTURES / filename).read_bytes()).hexdigest() == expected_hash


def test_all_sixty_planted_cases_and_repeat_no_new_open_hits(client, app):
    expected = json.loads((FIXTURES / "expected/hits.json").read_text())["cases"]
    with app.state.session_factory() as db:
        refresh_csl(db, file=FIXTURES / "csl_sample_v1.json")
        source = Source(kind="csv", label="Synthetic contract fixture")
        db.add(source)
        db.commit()
        sync_csv(db, source, (FIXTURES / "parties.csv").read_bytes(), "parties.csv", MAPPING)
        # Exercise durable initial-list and party-change work, rather than bypassing it.
        process_pending_tasks(db)
        assert not db.scalars(select(ScreeningTask).where(ScreeningTask.status != "complete")).all()
        failures = []
        for case in expected:
            party = db.scalar(select(Party).where(Party.external_id == case["external_id"]))
            entry = db.scalar(
                select(CSLEntry).where(
                    CSLEntry.source_list == case["source_list"], CSLEntry.csl_id == case["csl_id"]
                )
            )
            hits = db.scalars(
                select(Hit).where(Hit.party_id == party.id, Hit.csl_entry_id == entry.id)
            ).all()
            if case["expected_below_candidate_min"]:
                if hits:
                    failures.append(
                        (
                            case["external_id"],
                            "unexpected address-only candidate",
                            [(hit.det_score, hit.bucket) for hit in hits],
                        )
                    )
            elif not hits or any(hit.bucket not in case["expected_buckets"] for hit in hits):
                failures.append(
                    (
                        case["external_id"],
                        case["expected_buckets"],
                        [(hit.det_score, hit.bucket) for hit in hits],
                    )
                )
        assert failures == [], f"Frozen fixture outcomes violated: {failures}"
        open_before = [hit.id for hit in queue_hits(db)]
        run = run_screening(db)
        assert run.hits_new == 0
        assert [hit.id for hit in queue_hits(db)] == open_before
        refresh = refresh_csl(db, file=FIXTURES / "csl_sample_v2.json")
        assert (refresh.version.added, refresh.version.changed, refresh.version.removed) == (
            5,
            5,
            5,
        )
        for case in [row for row in expected if row["removed_in_v2"]]:
            party = db.scalar(select(Party).where(Party.external_id == case["external_id"]))
            entry = db.scalar(
                select(CSLEntry).where(
                    CSLEntry.source_list == case["source_list"], CSLEntry.csl_id == case["csl_id"]
                )
            )
            hits = db.scalars(
                select(Hit).where(Hit.party_id == party.id, Hit.csl_entry_id == entry.id)
            ).all()
            assert hits and all(hit.status == "dispositioned" for hit in hits)
            assert all(
                db.scalar(
                    select(Disposition).where(
                        Disposition.hit_id == hit.id, Disposition.kind == "auto_cleared"
                    )
                )
                for hit in hits
            )
        process_pending_tasks(db)
        assert not db.scalars(
            select(Hit)
            .join(CSLEntry)
            .where(Hit.status == "open", CSLEntry.removed_version.is_not(None))
        ).all()
