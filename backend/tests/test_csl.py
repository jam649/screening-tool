import json

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from typer.testing import CliRunner

from app.cli import cli
from app.csl.fetch import CSLDownloadError, download_bulk, download_stamps
from app.csl.parser import BULK_URL, CSLValidationError, canonical_hash, parse_bulk
from app.csl.service import CSLBusyError, refresh_csl
from app.models import (
    AuditLog,
    CSLEntry,
    CSLEntryRevision,
    CSLFetch,
    CSLVersion,
    Disposition,
    Hit,
    Party,
    ScreeningRun,
    ScreeningTask,
    Source,
)


def listing(number=0):
    return {
        "id": f"listing-{number}",
        "source": "Entity List (EL) - Bureau of Industry and Security",
        "name": f"Example Manufacturing {number}",
        "alt_names": [f"Example {number}"],
        "addresses": [{"address": "1 Main Street", "city": "Boston", "country": "US"}],
        "programs": ["Export restriction"],
        "remarks": "Published remarks",
        "source_list_url": "https://www.bis.gov/entity-list",
        "federal_register_notice": "Vol. 59, No. 44, 03/07/1994",
        "type": "Entity",
    }


def envelope(rows, timestamp="2026-10-13T05:00:00Z"):
    return json.dumps(
        {
            "results": rows,
            "total": len(rows),
            "offset": 0,
            "search_performed_at": timestamp,
            "sources_used": [
                {
                    "source": "Entity List (EL) - Bureau of Industry and Security",
                    "last_imported": timestamp,
                    "source_last_updated": "2026-10-12T00:00:00Z",
                }
            ],
        }
    ).encode()


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_parser_published_shape_raw_hash_and_urls():
    raw = listing()
    raw["citizenships"] = ["CA"]
    raw["nationalities"] = ["GB"]
    parsed = parse_bulk(envelope([raw]), require_envelope=True)
    entry = parsed.entries[0]
    assert entry.source_list == "EL"
    assert entry.countries == ["US"]  # citizenship is not residency
    assert entry.entity_type == "entity"
    assert entry.fr_notice_url is None
    assert entry.raw == raw
    assert entry.entry_hash == canonical_hash(
        {
            "name": entry.name,
            "alt_names": entry.alt_names,
            "addresses": entry.addresses,
            "countries": entry.countries,
            "programs": entry.programs,
        }
    )
    changed_stamp = parse_bulk(envelope([raw], timestamp="2026-10-13T06:00:00Z"))
    assert changed_stamp.file_sha256 != parsed.file_sha256
    assert changed_stamp.semantic_sha256 == parsed.semantic_sha256
    for url in ["javascript:alert(1)", "https://user:password@example.com", "https://good.com\n"]:
        raw["source_list_url"] = url
        assert parse_bulk(envelope([raw])).entries[0].source_list_url is None
    raw["federal_register_notice"] = "https://www.federalregister.gov/notices/test"
    assert parse_bulk(envelope([raw])).entries[0].fr_notice_url == raw["federal_register_notice"]
    raw["alt_names"] = None
    raw["addresses"] = None
    assert parse_bulk(envelope([raw])).entries[0].addresses == []
    raw["addresses"] = {}
    with pytest.raises(CSLValidationError):
        parse_bulk(envelope([raw]))


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not JSON",
        b"[]",
        b'{"results": [], "total": 0}',
        b'{"results": [], "total": 1}',
        b'{"results": [], "total": 0, "offset": 1}',
        b'{"results": [], "total": NaN}',
        b'{"total":0,"total":1,"results":[]}',
        envelope([listing(), listing()]),
        envelope([{**listing(), "name": ""}]),
        envelope([{**listing(), "programs": "single string"}]),
        envelope([{**listing(), "source": "An unknown source department"}]),
    ],
)
def test_invalid_or_empty_bulk_rejected(payload):
    with pytest.raises(CSLValidationError):
        parse_bulk(payload)


def test_fixture_arrays_only_for_explicit_files():
    data = json.dumps([listing()]).encode()
    assert len(parse_bulk(data).entries) == 1
    with pytest.raises(CSLValidationError):
        parse_bulk(data, require_envelope=True)


def test_bounded_download_and_sources_auth():
    seen = []

    def handle(request):
        seen.append(request)
        if request.url.path.endswith("sources"):
            return httpx.Response(200, json=[{"source": "EL", "last_imported": "published"}])
        return httpx.Response(200, content=envelope([listing()]))

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert parse_bulk(download_bulk(client)).entries[0].name == listing()["name"]
        assert download_stamps("fixture-key", client)["EL"]["last_imported"] == "published"
    assert str(seen[0].url) == BULK_URL
    assert seen[1].headers["subscription-key"] == "fixture-key"
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as client:
        with pytest.raises(CSLDownloadError):
            download_bulk(client)
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    ) as c:
        with pytest.raises(CSLDownloadError):
            download_stamps("fixture-key", c)


def test_repeated_fetches_one_version_and_durable_delta(client, app):
    rows = [listing(i) for i in range(25)]
    with app.state.session_factory() as db:
        first = refresh_csl(db, data=envelope(rows))
        same = refresh_csl(db, data=envelope(rows))
        stamped = refresh_csl(db, data=envelope(rows, timestamp="2026-10-13T06:00:00Z"))
        assert first.version.id == same.version.id == stamped.version.id
        assert first.changed and not same.changed and not stamped.changed
        assert count(db, CSLVersion) == 1 and count(db, CSLFetch) == 3
        assert count(db, CSLEntryRevision) == 25 and count(db, ScreeningTask) == 1
        task = db.get(ScreeningTask, first.pending_task_id)
        assert task.trigger == "list_change" and task.status == "pending"
        assert len(task.entry_ids) == 25 and task.party_ids == []
        assert stamped.fetch.file_sha256 != first.fetch.file_sha256
        assert stamped.fetch.source_list_stamps["EL"]["last_imported"].endswith("06:00:00Z")
        audit_rows = db.scalars(select(AuditLog)).all()
        assert sum(row.action == "csl.fetch_unchanged" for row in audit_rows) == 2


def test_five_add_change_remove_and_immutable_snapshot(client, app):
    original = [listing(i) for i in range(25)]
    updated = [listing(i) for i in range(5, 30)]
    for row in updated[:5]:
        row["alt_names"].append("New alternate name")
    with app.state.session_factory() as db:
        before = refresh_csl(db, data=envelope(original))
        after = refresh_csl(db, data=envelope(updated))
        assert (after.version.added, after.version.changed, after.version.removed) == (5, 5, 5)
        assert count(db, CSLEntry) == 30
        assert (
            len(db.scalars(select(CSLEntry).where(CSLEntry.removed_version.is_(None))).all()) == 25
        )
        assert len(db.get(ScreeningTask, after.pending_task_id).entry_ids) == 10
        old = db.scalar(
            select(CSLEntryRevision).where(
                CSLEntryRevision.version_id == before.version.id,
                CSLEntryRevision.snapshot["csl_id"].as_string() == "listing-5",
            )
        )
        assert old.snapshot["alt_names"] == ["Example 5"]
        assert old.snapshot["raw"] == original[5]
        old.snapshot = {"changed": True}
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    with app.state.engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(text("DELETE FROM csl_entry_revisions"))


def test_a_b_a_reappearance_reuses_identity_but_new_version(client, app):
    original = [listing(i) for i in range(25)]
    data = envelope(original)
    with app.state.session_factory() as db:
        first = refresh_csl(db, data=data)
        original_id = db.scalar(select(CSLEntry.id).where(CSLEntry.csl_id == "listing-0"))
        refresh_csl(db, data=envelope(original[5:]))
        again = refresh_csl(db, data=data)
        assert again.version.id != first.version.id
        assert again.version.file_sha256 == first.version.file_sha256
        assert again.version.added == 5 and again.version.removed == 0
        row = db.scalar(select(CSLEntry).where(CSLEntry.csl_id == "listing-0"))
        assert row.id == original_id and row.removed_version is None
        assert row.first_seen_version == first.version.id
        assert len(db.get(ScreeningTask, again.pending_task_id).entry_ids) == 5


def test_metadata_revision_preserves_raw_without_rescreen(client, app):
    row = listing()
    with app.state.session_factory() as db:
        first = refresh_csl(db, data=envelope([row]))
        row["remarks"] = "New legal context"
        second = refresh_csl(db, data=envelope([row]))
        assert second.version.id != first.version.id
        assert (second.version.added, second.version.changed, second.version.removed) == (0, 0, 0)
        assert second.pending_task_id is None
        assert count(db, CSLEntryRevision) == 2


def test_rejected_and_failed_fetch_leave_active_list(client, app, monkeypatch):
    with app.state.session_factory() as db:
        first = refresh_csl(db, data=envelope([listing(i) for i in range(25)]))
        for payload in [b"bad JSON", envelope([listing(i) for i in range(3)])]:
            with pytest.raises(CSLValidationError):
                refresh_csl(db, data=payload)
        assert count(db, CSLVersion) == 1 and count(db, CSLEntry) == 25
        assert count(db, ScreeningTask) == 1
        assert count(db, CSLFetch) == 3
        assert len(db.scalars(select(CSLFetch).where(CSLFetch.status == "failed")).all()) == 2
        assert db.get(CSLVersion, first.version.id).entry_count == 25
        monkeypatch.setattr(
            "app.csl.service.download_bulk",
            lambda: (_ for _ in ()).throw(CSLDownloadError("Network unavailable")),
        )
        with pytest.raises(CSLDownloadError):
            refresh_csl(db)
        assert count(db, CSLVersion) == 1 and count(db, CSLFetch) == 4


def test_optional_stamps_failure_keeps_bulk_and_never_key(client, app, monkeypatch):
    monkeypatch.setattr(
        "app.csl.service.download_stamps",
        lambda key: (_ for _ in ()).throw(
            CSLDownloadError("Sources API unavailable; using metadata from the bulk download")
        ),
    )
    with app.state.session_factory() as db:
        result = refresh_csl(db, data=envelope([listing()]), api_key="private-fixture-key")
        assert result.fetch.status == "success" and result.fetch.warnings
        assert "EL" in result.fetch.source_list_stamps
        assert "private-fixture-key" not in str(db.scalars(select(AuditLog)).all())


def test_removed_open_hits_closed_by_system_with_admin_trigger(client, app, admin):
    with app.state.session_factory() as db:
        db.info["actor_user_id"] = admin["user"]["id"]
        first = refresh_csl(db, data=envelope([listing(i) for i in range(25)]))
        source = Source(kind="csv", label="Dummy CSV")
        db.add(source)
        db.flush()
        party = Party(source_id=source.id, external_id="p1", name="Example", record_hash="p" * 64)
        run = ScreeningRun(trigger="manual", csl_version_id=first.version.id)
        db.add_all([party, run])
        db.flush()
        entry = db.scalar(select(CSLEntry).where(CSLEntry.csl_id == "listing-0"))
        hit = Hit(
            run_id=run.id,
            party_id=party.id,
            party_hash=party.record_hash,
            csl_entry_id=entry.id,
            entry_hash=entry.entry_hash,
            det_score=95,
            final_confidence=95,
            bucket="likely",
            status="open",
            party_snapshot={"name": party.name},
            entry_snapshot={"name": entry.name},
        )
        db.add(hit)
        db.commit()
        result = refresh_csl(db, data=envelope([listing(i) for i in range(5, 25)]))
        assert result.closed_hits == 1
        assert hit.status == "dispositioned"
        decision = db.scalar(select(Disposition).where(Disposition.hit_id == hit.id))
        assert decision.kind == "auto_cleared" and decision.actor_user_id is None
        assert result.version.id in decision.note
        assert hit.entry_snapshot == {"name": entry.name}
        events = db.scalars(
            select(AuditLog).where(AuditLog.action.in_(["dispositions.create", "csl.hit_closed"]))
        ).all()
        assert events and all(event.actor_user_id is None for event in events)
        assert db.info["actor_user_id"] == admin["user"]["id"]


def test_busy_refresh_and_recover_interrupted_fetch(client, app):
    from datetime import timedelta

    from app.models import utcnow

    with app.state.session_factory() as db:
        busy = CSLFetch(source=BULK_URL)
        db.add(busy)
        db.commit()
        with pytest.raises(CSLBusyError):
            refresh_csl(db, data=envelope([listing()]))
        busy.fetched_at = utcnow() - timedelta(minutes=11)
        db.commit()
        result = refresh_csl(db, data=envelope([listing()]))
        assert result.changed and busy.status == "failed"
        assert "stopped" in busy.error


def test_admin_api_csrf_and_fetch_error(client, app, admin, monkeypatch):
    assert client.get("/api/v1/csl/status").json()["current_version"] is None
    assert client.post("/api/v1/csl/refresh").status_code == 403
    monkeypatch.setattr("app.csl.service.download_bulk", lambda: envelope([listing()]))
    response = client.post("/api/v1/csl/refresh", headers={"X-CSRF-Token": admin["csrf_token"]})
    assert response.status_code == 200, response.text
    assert response.json()["current_version"]["entry_count"] == 1
    assert response.json()["pending_task_id"]
    assert len(client.get("/api/v1/csl/versions").json()) == 1
    assert client.get("/api/v1/csl/versions?limit=0").status_code == 422
    monkeypatch.setattr("app.csl.service.download_bulk", lambda: b"malformed")
    assert (
        client.post(
            "/api/v1/csl/refresh", headers={"X-CSRF-Token": admin["csrf_token"]}
        ).status_code
        == 422
    )
    assert client.get("/api/v1/csl/status").json()["last_fetch"]["status"] == "failed"
    with app.state.session_factory() as db:
        from app.models import User

        db.get(User, admin["user"]["id"]).role = "reviewer"
        db.commit()
    assert client.get("/api/v1/csl/status").status_code == 403
    assert (
        client.post(
            "/api/v1/csl/refresh", headers={"X-CSRF-Token": admin["csrf_token"]}
        ).status_code
        == 403
    )


def test_csl_cli_fixture(config, monkeypatch, tmp_path):
    monkeypatch.setenv("SCREENING_TOOL_SECRET_KEY", config.secret_key.get_secret_value())
    monkeypatch.setenv("SCREENING_TOOL_DATABASE_URL", config.database_url)
    file = tmp_path / "complete.json"
    file.write_bytes(envelope([listing()]))
    result = CliRunner().invoke(cli, ["csl", "refresh", "--file", str(file)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["version"]["entry_count"] == 1
    again = CliRunner().invoke(cli, ["csl", "refresh", "--file", str(file)])
    assert again.exit_code == 0 and not json.loads(again.output)["changed"]
    result = CliRunner().invoke(cli, ["csl", "refresh", "--file", str(tmp_path / "missing")])
    assert result.exit_code == 1 and "Unable to read" in result.output


def test_same_published_id_in_multiple_sources_removal_is_scoped(client, app):
    a = listing()
    b = {**listing(), "source": "SDN", "name": "Separate source listing"}
    data = envelope([a, b])
    assert len(parse_bulk(data).entries) == 2
    with app.state.session_factory() as db:
        first = refresh_csl(db, data=data)
        assert first.version.entry_count == 2
        assert len(db.scalars(select(CSLEntry).where(CSLEntry.csl_id == a["id"])).all()) == 2
        after = refresh_csl(db, data=envelope([b]))
        assert after.version.removed == 1 and after.version.changed == 0
        remaining = db.scalar(select(CSLEntry).where(CSLEntry.removed_version.is_(None)))
        assert remaining.source_list == "SDN" and remaining.csl_id == a["id"]


def test_download_limit_and_error_do_not_expose_key(monkeypatch):
    monkeypatch.setattr("app.csl.fetch.MAX_DOWNLOAD_BYTES", 3)
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"1234"))
    ) as client:
        with pytest.raises(CSLDownloadError, match="size limit"):
            download_bulk(client)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(401))) as client:
        with pytest.raises(CSLDownloadError) as error:
            download_stamps("private-fixture-key", client)
        assert "private-fixture-key" not in str(error.value)


def test_transaction_failure_retains_previous_evidence(client, app, monkeypatch):
    with app.state.session_factory() as db:
        refresh_csl(db, data=envelope([listing()]))
        original_hash = db.scalar(select(CSLEntry.entry_hash))
        row = {**listing(), "name": "Changed name"}

        def failure(**kwargs):
            raise RuntimeError("Injected storage failure")

        monkeypatch.setattr("app.csl.service.CSLEntryRevision", failure)
        with pytest.raises(RuntimeError, match="Injected storage failure"):
            refresh_csl(db, data=envelope([row]))
        assert count(db, CSLVersion) == 1 and count(db, CSLEntryRevision) == 1
        assert count(db, ScreeningTask) == 1
        assert db.scalar(select(CSLEntry.entry_hash)) == original_hash
        fetch = db.scalar(select(CSLFetch).where(CSLFetch.status == "failed"))
        assert fetch.error == "CSL refresh failed; the previous list remains active"


def test_upgrade_populated_baseline_preserves_foreign_keys_and_audit(config):
    from datetime import datetime

    from alembic.config import Config as AlembicConfig
    from sqlalchemy import MetaData

    from alembic import command
    from app.database import create_database_engine, migrate

    engine = create_database_engine(config)
    alembic_config = AlembicConfig("alembic.ini")
    alembic_config.set_main_option("script_location", "alembic")
    with engine.connect() as connection:
        alembic_config.attributes["connection"] = connection
        command.upgrade(alembic_config, "caa3a3b7e05a")
    metadata = MetaData()
    metadata.reflect(bind=engine)
    now = datetime(2026, 10, 13)
    with engine.begin() as connection:
        connection.execute(
            metadata.tables["csl_versions"]
            .insert()
            .values(
                id="version",
                fetched_at=now,
                file_sha256="a" * 64,
                entry_count=1,
                added=1,
                changed=0,
                removed=0,
                source_list_stamps={},
            )
        )
        connection.execute(
            metadata.tables["csl_entries"]
            .insert()
            .values(
                id="entry",
                csl_id="published",
                source_list="EL",
                name="Published name",
                alt_names=[],
                addresses=[],
                countries=[],
                programs=[],
                raw={},
                entry_hash="b" * 64,
                first_seen_version="version",
                last_seen_version="version",
            )
        )
        connection.execute(
            metadata.tables["sources"]
            .insert()
            .values(
                id="source",
                created_at=now,
                kind="csv",
                label="Dummy",
                config={},
                record_count=1,
            )
        )
        connection.execute(
            metadata.tables["parties"]
            .insert()
            .values(
                id="party",
                source_id="source",
                external_id="p",
                party_type="company",
                role="customer",
                name="Party",
                alt_names=[],
                address_lines=[],
                raw={},
                record_hash="c" * 64,
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        connection.execute(
            metadata.tables["screening_runs"]
            .insert()
            .values(
                id="run",
                trigger="manual",
                csl_version_id="version",
                started_at=now,
                parties_screened=1,
                hits_new=1,
                hits_suppressed=0,
                auto_cleared=0,
                settings_snapshot={},
                model_fallbacks=[],
            )
        )
        connection.execute(
            metadata.tables["hits"]
            .insert()
            .values(
                id="hit",
                created_at=now,
                run_id="run",
                party_id="party",
                party_hash="c" * 64,
                csl_entry_id="entry",
                entry_hash="b" * 64,
                det_score=95,
                final_confidence=95,
                bucket="likely",
                status="open",
                explanation={},
                party_snapshot={"name": "Party"},
                entry_snapshot={"name": "Published name"},
            )
        )
        connection.execute(
            metadata.tables["audit_log"]
            .insert()
            .values(
                id="audit",
                created_at=now,
                action="test.legacy",
                target_table="hits",
                target_id="hit",
                after={"unchanged": True},
            )
        )
    migrate(engine)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
        assert connection.exec_driver_sql("SELECT csl_entry_id FROM hits").scalar() == "entry"
        assert (
            connection.exec_driver_sql("SELECT file_sha256 FROM csl_versions").scalar() == "a" * 64
        )
        assert (
            connection.exec_driver_sql("SELECT name FROM csl_entries").scalar() == "Published name"
        )
        with pytest.raises(IntegrityError):
            connection.exec_driver_sql("UPDATE audit_log SET action='rewritten'")
    engine.dispose()


def test_api_and_cli_use_encrypted_optional_key(client, app, admin, config, monkeypatch):
    from app.models import Setting
    from app.security import encrypt_json

    seen = []
    monkeypatch.setattr("app.csl.service.download_bulk", lambda: envelope([listing()]))
    monkeypatch.setattr(
        "app.csl.service.download_stamps",
        lambda key: (
            seen.append(key)
            or {
                "EL": {"source": "EL", "last_imported": "published timestamp"},
            }
        ),
    )
    with app.state.session_factory() as db:
        db.add(
            Setting(
                key="csl_api_key",
                value=encrypt_json({"api_key": "private-fixture-key"}, config),
                encrypted=True,
            )
        )
        db.commit()
    response = client.post("/api/v1/csl/refresh", headers={"X-CSRF-Token": admin["csrf_token"]})
    assert response.status_code == 200 and seen == ["private-fixture-key"]
    assert "private-fixture-key" not in response.text
    assert "sources_api" in response.json()["last_fetch"]["source_list_stamps"]
    monkeypatch.setenv("SCREENING_TOOL_SECRET_KEY", config.secret_key.get_secret_value())
    monkeypatch.setenv("SCREENING_TOOL_DATABASE_URL", config.database_url)
    result = CliRunner().invoke(cli, ["csl", "refresh"])
    assert result.exit_code == 0 and seen == ["private-fixture-key", "private-fixture-key"]
    assert "private-fixture-key" not in result.output
    with app.state.session_factory() as db:
        audit_rows = db.scalars(select(AuditLog)).all()
        assert "private-fixture-key" not in json.dumps([row.after for row in audit_rows])
