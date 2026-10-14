"""Frozen milestone 3 acceptance contract; expectations never follow engine output."""

import json
from pathlib import Path

from sqlalchemy import func, select

from app.connectors.csv import CSVConnector
from app.connectors.sync import apply_sync
from app.csl.service import refresh_csl
from app.models import CSLEntry, Disposition, Hit, Party, Source
from app.screening.engine import queue_hits, run_screening

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def test_frozen_300_entries_2060_parties_60_outcomes_and_repeat(client, admin):
    with client.app.state.session_factory() as db:
        refresh = refresh_csl(db, file=FIXTURES / "csl_sample_v1.json")
        assert refresh.version.entry_count == 300
        source = Source(kind="csv", label="Frozen synthetic acceptance book")
        db.add(source)
        db.commit()
        mapping = {
            key: key
            for key in (
                "external_id",
                "name",
                "party_type",
                "role",
                "address_lines",
                "city",
                "region",
                "postal_code",
                "country",
            )
        }
        batch = CSVConnector(
            (FIXTURES / "parties.csv").read_bytes(), "parties.csv", mapping
        ).full_sync()
        assert batch.rejected == []
        result = apply_sync(db, source, batch, mapping=mapping)
        assert result.added == 2060
        first = run_screening(db)
        expected = json.loads((FIXTURES / "expected" / "hits.json").read_text())
        assert expected["case_count"] == 60 and len(expected["cases"]) == 60
        parties = {party.external_id: party for party in db.scalars(select(Party))}
        entries = {
            (entry.source_list, entry.csl_id): entry for entry in db.scalars(select(CSLEntry))
        }
        hits = {
            (hit.party_id, hit.csl_entry_id): hit
            for hit in db.scalars(select(Hit).where(Hit.run_id == first.id))
        }
        for case in expected["cases"]:
            party = parties[case["external_id"]]
            entry = entries[(case["source_list"], case["csl_id"])]
            hit = hits.get((party.id, entry.id))
            if case["expected_below_candidate_min"]:
                assert hit is None, case
            else:
                assert hit is not None, case
                assert hit.bucket in case["expected_buckets"], (
                    case,
                    hit.det_score,
                    hit.explanation,
                )
        queue_before = {hit.id for hit in queue_hits(db)}
        assert first.hits_new == len(queue_before)
        second = run_screening(db)
        assert second.parties_screened == 2060 and second.hits_new == 0
        assert {hit.id for hit in queue_hits(db)} == queue_before
        assert db.scalar(
            select(func.count()).select_from(Hit).where(Hit.run_id == second.id)
        ) == len(hits)
        # Removal closes every unresolved observation, retaining its original snapshots.
        changed = refresh_csl(db, file=FIXTURES / "csl_sample_v2.json")
        assert (changed.version.added, changed.version.changed, changed.version.removed) == (
            5,
            5,
            5,
        )
        for case in expected["cases"]:
            if not case["removed_in_v2"]:
                continue
            party = parties[case["external_id"]]
            entry = entries[(case["source_list"], case["csl_id"])]
            hit = hits[(party.id, entry.id)]
            db.refresh(hit)
            assert hit.status == "dispositioned"
            decision = db.scalar(select(Disposition).where(Disposition.hit_id == hit.id))
            assert decision.kind == "auto_cleared" and decision.actor_user_id is None
            assert "entry removed" in decision.note
            assert hit.entry_snapshot["removed_version"] is None
