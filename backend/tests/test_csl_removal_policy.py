"""Valid complete list removals are authoritative regardless of cardinality."""

import json

import pytest
from sqlalchemy import select

from app.csl.parser import CSLValidationError
from app.csl.service import refresh_csl
from app.models import AuditLog, CSLEntry, CSLEntryRevision, Disposition, Hit, Party, Source
from app.screening.engine import run_screening
from app.screening.normalize import party_record_hash


def listing(number):
    return {
        "id": f"listing-{number}",
        "source": "EL",
        "name": f"Listed Enterprise {number}",
        "type": "Entity",
        "alt_names": [],
        "addresses": [{"country": "US"}],
        "programs": [],
    }


def envelope(rows, total=None):
    return json.dumps(
        {
            "results": rows,
            "total": len(rows) if total is None else total,
            "offset": 0,
            "sources_used": [],
        }
    ).encode()


def test_complete_large_removal_applies_system_closure_and_retains_evidence(client, admin):
    with client.app.state.session_factory() as db:
        db.info["actor_user_id"] = admin["user"]["id"]
        first = refresh_csl(db, data=envelope([listing(i) for i in range(25)]))
        source = Source(kind="csv", label="Large removal fixture")
        db.add(source)
        db.flush()
        party = Party(
            source_id=source.id,
            external_id="one",
            name="Listed Enterprise 0",
            country_iso2="US",
            record_hash=party_record_hash({"name": "Listed Enterprise 0", "country_iso2": "US"}),
        )
        db.add(party)
        db.commit()
        run_screening(db)
        removed_id = db.scalar(select(CSLEntry.id).where(CSLEntry.csl_id == "listing-0"))
        old_hits = list(db.scalars(select(Hit).where(Hit.csl_entry_id == removed_id)))
        assert old_hits
        evidence = [hit.entry_snapshot for hit in old_hits]
        result = refresh_csl(db, data=envelope([listing(i) for i in range(22, 25)]))
        assert result.version.removed == 22 and result.fetch.status == "success"
        assert result.fetch.warnings and result.closed_hits >= 1
        assert all(hit.status == "dispositioned" for hit in old_hits)
        assert [hit.entry_snapshot for hit in old_hits] == evidence
        decisions = list(
            db.scalars(
                select(Disposition).where(Disposition.hit_id.in_([hit.id for hit in old_hits]))
            )
        )
        assert all(row.kind == "auto_cleared" and row.actor_user_id is None for row in decisions)
        assert all(result.version.id in row.note for row in decisions)
        assert (
            len(
                list(
                    db.scalars(
                        select(CSLEntryRevision).where(
                            CSLEntryRevision.version_id == first.version.id
                        )
                    )
                )
            )
            == 25
        )
        event = db.scalar(select(AuditLog).where(AuditLog.action == "csl.large_removal"))
        assert event.after["removed"] == 22 and event.after["active_before"] == 25


def test_incomplete_large_removal_is_still_rejected_without_changing_active_list(client):
    with client.app.state.session_factory() as db:
        initial = refresh_csl(db, data=envelope([listing(i) for i in range(25)]))
        with pytest.raises(CSLValidationError):
            refresh_csl(db, data=envelope([listing(i) for i in range(3)], total=25))
        active = list(db.scalars(select(CSLEntry).where(CSLEntry.removed_version.is_(None))))
        assert len(active) == 25 and all(
            row.last_seen_version == initial.version.id for row in active
        )
        assert not list(db.scalars(select(AuditLog).where(AuditLog.action == "csl.large_removal")))
