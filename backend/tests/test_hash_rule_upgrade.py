"""Explicit type-hash transition retains genuine v1 comparison provenance."""

import json
from copy import deepcopy

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from app.cli import cli
from app.connectors.csv import CSVConnector
from app.connectors.sync import apply_sync
from app.csl.parser import canonical_hash, parse_bulk
from app.csl.service import refresh_csl
from app.models import (
    CSLEntry,
    CSLEntryRevision,
    CSLVersion,
    Disposition,
    Hit,
    Party,
    ScreeningRun,
    ScreeningTask,
    Setting,
    Source,
    Suppression,
)
from app.screening.engine import process_pending_tasks, queue_hits, run_screening, snapshot
from app.screening.hash_upgrade import HASH_RULE_SETTING, upgrade_hash_rules
from app.screening.normalize import normalize_party_fields
from app.screening.settings import ScreeningValidationError


def bulk(kind="Entity"):
    return json.dumps(
        [
            {
                "id": "legacy",
                "source": "EL",
                "name": "Example Manufacturing",
                "type": kind,
                "country": "US",
            }
        ]
    ).encode()


def legacy_cache(db, actor_id):
    parsed = parse_bulk(bulk())
    fields = parsed.entries[0].fields()
    old_hash = canonical_hash(
        {key: fields[key] for key in ("name", "alt_names", "addresses", "countries", "programs")}
    )
    fields["entry_hash"] = old_hash
    version = CSLVersion(
        file_sha256=parsed.file_sha256, semantic_sha256=canonical_hash([fields]), entry_count=1
    )
    source = Source(kind="csv", label="Legacy synthetic cache")
    db.add_all([source, version])
    db.flush()
    entry = CSLEntry(**fields, first_seen_version=version.id, last_seen_version=version.id)
    party_fields = {"party_type": "company", "name": fields["name"], "country_iso2": "US"}
    legacy_party_fields = normalize_party_fields(party_fields)
    legacy_party_fields.pop("party_type")
    party = Party(
        source_id=source.id,
        external_id="legacy",
        **party_fields,
        record_hash=canonical_hash(legacy_party_fields),
    )
    db.add_all([entry, party])
    db.flush()
    revision = CSLEntryRevision(
        version_id=version.id, entry_id=entry.id, snapshot={"id": entry.id, **fields}
    )
    run = ScreeningRun(
        trigger="manual",
        csl_version_id=version.id,
        settings_snapshot={},
        parties_screened=1,
        hits_new=1,
    )
    db.add_all([revision, run])
    db.flush()
    hit = Hit(
        run_id=run.id,
        party_id=party.id,
        party_hash=party.record_hash,
        csl_entry_id=entry.id,
        entry_hash=entry.entry_hash,
        det_score=100,
        final_confidence=100,
        bucket="likely",
        status="open",
        party_snapshot=snapshot(party),
        entry_snapshot=snapshot(entry),
    )
    db.add(hit)
    db.flush()
    decision = Disposition(
        hit_id=hit.id,
        kind="false_positive",
        actor_user_id=actor_id,
        note="Original legacy decision",
    )
    db.add(decision)
    db.flush()
    hit.status = "dispositioned"
    suppression = Suppression(
        party_id=party.id,
        csl_entry_id=entry.id,
        party_hash=party.record_hash,
        entry_hash=entry.entry_hash,
        disposition_id=decision.id,
    )
    task = ScreeningTask(trigger="list_change", csl_version_id=version.id, entry_ids=[entry.id])
    db.add_all([suppression, task, Setting(key=HASH_RULE_SETTING, value=1)])
    db.commit()
    return party, entry, version, revision, hit, decision, suppression


def test_explicit_legacy_upgrade_preserves_evidence_and_screens_once(client, admin):
    with client.app.state.session_factory() as db:
        party, entry, version, revision, old_hit, decision, suppression = legacy_cache(
            db, admin["user"]["id"]
        )
        original = {
            "version": snapshot(version),
            "revision": deepcopy(revision.snapshot),
            "hit": snapshot(old_hit),
            "decision": snapshot(decision),
            "suppression": snapshot(suppression),
        }
        old_party_hash, old_entry_hash = party.record_hash, entry.entry_hash
        result = upgrade_hash_rules(db)
        assert (result.parties_rehashed, result.entries_rehashed) == (1, 1)
        assert result.task_id is not None
        assert party.record_hash != old_party_hash and entry.entry_hash != old_entry_hash
        assert snapshot(version) == original["version"]
        assert revision.snapshot == original["revision"]
        assert snapshot(old_hit) == original["hit"]
        assert snapshot(decision) == original["decision"]
        assert snapshot(suppression) == original["suppression"]
        # The revision proves the old v1 hash; current v2 hashes do not rewrite it.
        assert revision.snapshot["entry_hash"] == old_entry_hash
        assert revision.snapshot["entry_hash"] != entry.entry_hash
        again = upgrade_hash_rules(db)
        assert (again.parties_rehashed, again.entries_rehashed, again.task_id) == (0, 0, None)
        assert db.scalar(select(func.count()).select_from(ScreeningTask)) == 2
        runs = process_pending_tasks(db)
        assert len(runs) == 2 and runs[0].hits_new == 0 and runs[1].hits_new == 1
        hit = queue_hits(db)[0]
        assert hit.explanation["prior_disposition_id"] == decision.id
        assert hit.explanation["hash_rule_change"]["previous"] == 1
        assert hit.explanation["hash_rule_change"]["current"] == 2
        assert runs[1].settings_snapshot["hash_rule_version"] == 2
        assert runs[1].settings_snapshot["scope"]["entry_hashes"] == {entry.id: entry.entry_hash}
        assert runs[1].settings_snapshot["scope"]["party_hashes"] == {party.id: party.record_hash}
        assert hit.party_snapshot["party_type"] == "company"
        assert hit.entry_snapshot["entity_type"] == "entity"
        # Reading the same unchanged government bytes records a new v2 semantic
        # version while keeping the raw file hash and every v1 revision intact.
        refreshed = refresh_csl(db, data=bulk())
        assert refreshed.version.id != version.id
        assert refreshed.version.file_sha256 == version.file_sha256
        assert refreshed.version.semantic_sha256 != version.semantic_sha256
        assert (refreshed.version.added, refreshed.version.changed, refreshed.version.removed) == (
            0,
            0,
            0,
        )
        assert revision.snapshot == original["revision"]


def test_type_only_party_sync_invalidates_false_positive(client, admin):
    with client.app.state.session_factory() as db:
        refresh_csl(db, data=bulk())
        source = Source(kind="csv", label="Type change regression")
        db.add(source)
        db.commit()
        mapping = {"external_id": "id", "name": "name", "party_type": "type", "country": "country"}
        first = CSVConnector(
            b"id,name,type,country\nparty,Example Manufacturing,company,US\n",
            "parties.csv",
            mapping,
        )
        apply_sync(db, source, first.full_sync(), mapping=mapping)
        run_screening(db)
        hit = queue_hits(db)[0]
        decision = Disposition(
            hit_id=hit.id, kind="false_positive", actor_user_id=admin["user"]["id"]
        )
        db.add(decision)
        db.flush()
        hit.status = "dispositioned"
        db.add(
            Suppression(
                party_id=hit.party_id,
                csl_entry_id=hit.csl_entry_id,
                party_hash=hit.party_hash,
                entry_hash=hit.entry_hash,
                disposition_id=decision.id,
            )
        )
        db.commit()
        updated = CSVConnector(
            b"id,name,type,country\nparty,Example Manufacturing,individual,US\n",
            "parties.csv",
            mapping,
        )
        result = apply_sync(db, source, updated.full_sync(), mapping=mapping)
        assert result.changed == 1 and len(result.task_ids) == 1
        runs = process_pending_tasks(db)
        current = queue_hits(db)[0]
        assert current.party_hash != hit.party_hash and current.status == "open"
        assert current.explanation["prior_disposition_id"] == decision.id
        assert current.explanation["what_changed"]["party"]["party_type"] == {
            "before": "company",
            "after": "individual",
        }
        assert any(run.hits_new == 1 for run in runs)


def test_type_only_entry_delta_reopens_false_positive(client, admin):
    with client.app.state.session_factory() as db:
        party, entry, _, _, _, _, _ = legacy_cache(db, admin["user"]["id"])
        upgrade_hash_rules(db)
        process_pending_tasks(db)
        old_hit = queue_hits(db)[0]
        decision = Disposition(
            hit_id=old_hit.id, kind="false_positive", actor_user_id=admin["user"]["id"]
        )
        db.add(decision)
        db.flush()
        old_hit.status = "dispositioned"
        db.add(
            Suppression(
                party_id=party.id,
                csl_entry_id=entry.id,
                party_hash=old_hit.party_hash,
                entry_hash=old_hit.entry_hash,
                disposition_id=decision.id,
            )
        )
        db.commit()
        refresh = refresh_csl(db, data=bulk("Individual"))
        assert refresh.version.changed == 1
        runs = process_pending_tasks(db)
        hit = queue_hits(db)[0]
        assert hit.entry_hash != old_hit.entry_hash
        assert hit.explanation["prior_disposition_id"] == decision.id
        assert hit.explanation["what_changed"]["entry"]["entity_type"] == {
            "before": "entity",
            "after": "individual",
        }
        assert runs[0].hits_new == 1


def test_legacy_screening_refuses_stale_hashes_until_explicit_cli_upgrade(
    client, admin, config, monkeypatch
):
    with client.app.state.session_factory() as db:
        legacy_cache(db, admin["user"]["id"])
        with pytest.raises(ScreeningValidationError, match="hashes upgrade"):
            run_screening(db)
        assert db.scalar(select(func.count()).select_from(ScreeningRun)) == 1
        monkeypatch.setenv("SCREENING_TOOL_DATABASE_URL", config.database_url)
        monkeypatch.setenv("SCREENING_TOOL_SECRET_KEY", config.secret_key.get_secret_value())
        runner = CliRunner()
        result = runner.invoke(cli, ["hashes", "upgrade"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["hash_rule_version"] == 2
        assert payload["parties_rehashed"] == payload["entries_rehashed"] == 1
        assert payload["task_id"] is not None
        db.expire_all()
        assert (
            db.scalar(select(func.count()).select_from(ScreeningRun)) == 1
        )  # upgrade does not screen
        result = runner.invoke(cli, ["hashes", "upgrade"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["task_id"] is None
        result = runner.invoke(cli, ["screen", "--changed"])
        assert result.exit_code == 0, result.output
        assert sum(run["hits_new"] for run in json.loads(result.output)) == 1
