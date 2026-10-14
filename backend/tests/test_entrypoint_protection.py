"""Cross-entrypoint leases and committed results, using only isolated fixture data."""

import hashlib
import io
import json
import zipfile

import pytest
from pypdf import PdfReader
from sqlalchemy import func, select, text
from test_csl import envelope, listing
from test_notifications import configure
from test_review_api import review_seed as review_seed
from test_sources_api import MAPPING, file_content
from typer.testing import CliRunner

from app.cli import cli
from app.csl.service import current_version, refresh_csl
from app.models import (
    AuditLog,
    CSLFetch,
    Hit,
    NotificationBatch,
    OperationAttempt,
    OperationLease,
    Party,
    ScreeningRun,
    ScreeningTask,
    Source,
    User,
)
from app.scheduler.leases import lease_key, protected_operation
from app.scheduler.service import SOURCE_HANDLERS
from app.screening.normalize import party_record_hash


@pytest.fixture
def operator_runner(config, monkeypatch):
    monkeypatch.setenv("SCREENING_TOOL_DATABASE_URL", config.database_url)
    monkeypatch.setenv("SCREENING_TOOL_SECRET_KEY", config.secret_key.get_secret_value())
    monkeypatch.setenv("SCREENING_TOOL_SCHEDULER_ENABLED", "false")
    return CliRunner()


def upload(client, admin, source_id, *, headers=True):
    return client.post(
        f"/api/v1/sources/{source_id}/upload",
        headers={"x-csrf-token": admin["csrf_token"]} if headers else {},
        files={
            "file": (
                "books.csv",
                file_content([["c1", "Example Manufacturing 42", "US", "Boston", ""]]),
                "text/csv",
            )
        },
        data={"mapping": json.dumps(MAPPING)},
    )


def business_counts(factory):
    with factory() as db:
        return {
            model.__tablename__: db.scalar(select(func.count()).select_from(model))
            for model in (CSLFetch, Party, ScreeningRun, Hit, ScreeningTask)
        }


@pytest.mark.parametrize("endpoint", ["screen_all", "screen_party", "csl_refresh", "source_import"])
def test_api_overlap_records_skip_without_business_work(
    client, admin, review_seed, monkeypatch, endpoint
):
    factory = client.app.state.session_factory
    source_id = review_seed["source_id"]
    held = {
        "screen_all": "screen_changed",
        "screen_party": "screen_all",
        "csl_refresh": "csl_refresh",
        "source_import": "source_sync",
    }[endpoint]

    def unexpected(*args, **kwargs):
        pytest.fail("An overlapping operation reached its download/import/screen worker")

    monkeypatch.setattr("app.api.screening.run_screening", unexpected)
    monkeypatch.setattr("app.api.csl.refresh_csl", unexpected)
    monkeypatch.setattr("app.api.sources.sync_csv", unexpected)
    before = business_counts(factory)
    with protected_operation(factory, held, source_id=source_id) as execution:
        if endpoint == "source_import":
            response = upload(client, admin, source_id)
        else:
            body = (
                {"all": True} if endpoint == "screen_all" else {"party_id": review_seed["party_id"]}
            )
            response = client.post(
                "/api/v1/csl/refresh" if endpoint == "csl_refresh" else "/api/v1/screen",
                json=body,
                headers={"x-csrf-token": admin["csrf_token"]},
            )
        assert response.status_code == 409, response.text
        assert business_counts(factory) == before
        with factory() as db:
            skipped = db.scalar(
                select(OperationAttempt).where(OperationAttempt.status == "skipped")
            )
            assert skipped.operation == endpoint
            assert skipped.actor_user_id == admin["user"]["id"]
            assert skipped.error_code == "operation_already_running"
            assert skipped.outcome == {}
            lease = db.scalar(select(OperationLease).where(OperationLease.key == execution.key))
            assert lease.owner_id == execution.owner_id
    with factory() as db:
        assert db.scalar(select(OperationLease.owner_id)) is None


@pytest.mark.parametrize("mode", ["all", "party", "changed", "sync"])
def test_cli_respects_api_operation_lease(client, admin, review_seed, operator_runner, mode):
    factory = client.app.state.session_factory
    source_id = review_seed["source_id"]
    command = {
        "all": ["screen", "--all"],
        "party": ["screen", "--party", review_seed["party_id"]],
        "changed": ["screen", "--changed"],
        "sync": ["sync", source_id],
    }[mode]
    before = business_counts(factory)
    with protected_operation(
        factory, "source_import" if mode == "sync" else "screen_party", source_id=source_id
    ):
        response = operator_runner.invoke(cli, command)
        assert response.exit_code == 1, response.output
        outcome = json.loads(response.output)
        assert outcome["status"] == "skipped"
        assert outcome["error_code"] == "operation_already_running"
        assert business_counts(factory) == before
    assert lease_key("source_sync", source_id) == lease_key("source_import", source_id)


@pytest.mark.parametrize("endpoint", ["screen", "csl", "upload"])
def test_protected_api_keeps_csrf_and_admin_policy(client, admin, review_seed, endpoint):
    factory = client.app.state.session_factory
    before = business_counts(factory)

    def request(with_csrf):
        if endpoint == "upload":
            return upload(client, admin, review_seed["source_id"], headers=with_csrf)
        return client.post(
            "/api/v1/screen" if endpoint == "screen" else "/api/v1/csl/refresh",
            json={"all": True},
            headers={"x-csrf-token": admin["csrf_token"]} if with_csrf else {},
        )

    assert request(False).status_code == 403
    with factory() as db:
        db.get(User, admin["user"]["id"]).role = "reviewer"
        db.commit()
    assert request(True).status_code == 403
    assert client.get("/api/v1/screen/runs").status_code == 200
    assert client.get("/api/v1/sources").status_code == 200
    assert business_counts(factory) == before
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(OperationAttempt)) == 0


def test_import_commits_when_screening_is_leased_then_cli_consumes_tasks(
    client, admin, operator_runner
):
    factory = client.app.state.session_factory
    source_id = client.post(
        "/api/v1/sources",
        json={"label": "Deferred books"},
        headers={"x-csrf-token": admin["csrf_token"]},
    ).json()["id"]
    with factory() as db:
        refresh_csl(db, data=envelope([listing(42)]))
    with protected_operation(factory, "screen_all"):
        response = upload(client, admin, source_id)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["applied"] and payload["added"] == 1
        assert payload["screening_pending"] and payload["screening_errors"]
        assert payload["screening_run_ids"] == []
        with factory() as db:
            assert db.scalar(select(Party)).name == "Example Manufacturing 42"
            assert len(list(db.scalars(select(ScreeningTask)))) == 2
            assert db.scalar(select(ScreeningRun)) is None
            imported = db.scalar(
                select(OperationAttempt).where(OperationAttempt.operation == "source_import")
            )
            assert imported.status == "success" and imported.outcome["applied"] is True
    screened = operator_runner.invoke(cli, ["screen", "--changed"])
    assert screened.exit_code == 0, screened.output
    run_ids = [run["id"] for run in json.loads(screened.output)]
    assert len(run_ids) == 2
    with factory() as db:
        assert all(task.status == "complete" for task in db.scalars(select(ScreeningTask)))
        assert all(db.get(ScreeningRun, run_id).finished_at for run_id in run_ids)


def test_cli_partial_screen_failure_keeps_successful_runs_and_safe_outcome(
    client, admin, operator_runner, monkeypatch
):
    from app.screening import engine

    factory = client.app.state.session_factory
    source_id = client.post(
        "/api/v1/sources",
        json={"label": "Partial books"},
        headers={"x-csrf-token": admin["csrf_token"]},
    ).json()["id"]
    with factory() as db:
        failed_task_id = refresh_csl(db, data=envelope([listing(42)])).pending_task_id
    with protected_operation(factory, "screen_all"):
        assert upload(client, admin, source_id).json()["applied"]
    actual_execute = engine._execute

    def fail_old_task(db, **kwargs):
        if kwargs["trigger"] == "list_change":
            raise RuntimeError("private-input-must-not-escape")
        return actual_execute(db, **kwargs)

    monkeypatch.setattr(engine, "_execute", fail_old_task)
    response = operator_runner.invoke(cli, ["screen", "--changed"])
    assert response.exit_code == 1
    assert "private-input" not in response.output
    payload = json.loads(response.output)
    assert payload["status"] == "failed"
    with factory() as db:
        successful = list(db.scalars(select(ScreeningRun)))
        assert len(successful) == 1 and successful[0].finished_at is not None
        assert db.get(ScreeningTask, failed_task_id).status == "failed"
        assert db.scalar(select(Party)).name == "Example Manufacturing 42"
        assert payload["outcome"]["run_ids"] == [successful[0].id]
        assert payload["outcome"]["failed_task_ids"] == [failed_task_id]
        attempt = db.get(OperationAttempt, payload["id"])
        assert attempt.outcome == payload["outcome"]


def test_screen_dispatches_only_after_run_commit_and_lease_release(
    client, admin, review_seed, monkeypatch
):
    checked = []

    def dispatch(factory, config):
        with factory() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            newest = db.scalar(select(ScreeningRun).order_by(ScreeningRun.started_at.desc()))
            assert newest.finished_at is not None
            assert (
                db.scalar(select(OperationLease.owner_id).where(OperationLease.key == "screening"))
                is None
            )
            attempt = db.scalar(
                select(OperationAttempt).where(OperationAttempt.operation == "screen_all")
            )
            assert attempt.status == "success"
            assert attempt.outcome["run_ids"] == [newest.id]
            checked.append(newest.id)
            db.rollback()

    monkeypatch.setattr("app.api.screening.dispatch_outbox", dispatch)
    response = client.post(
        "/api/v1/screen", json={"all": True}, headers={"x-csrf-token": admin["csrf_token"]}
    )
    assert response.status_code == 200, response.text
    assert checked == [response.json()["id"]]


@pytest.mark.parametrize("parent", ["csl", "sync"])
def test_cli_parent_preserves_committed_work_and_partial_child_screening(
    client, admin, operator_runner, monkeypatch, tmp_path, parent
):
    from app.screening import engine

    factory = client.app.state.session_factory
    fixture = envelope([listing(42)])
    with factory() as db:
        refreshed = refresh_csl(db, data=fixture)
        failed_task_id = refreshed.pending_task_id
        source = Source(kind="qbo", label="Fixture API")
        db.add(source)
        db.flush()
        party = Party(
            source_id=source.id,
            external_id="42",
            name="Example Manufacturing 42",
            country_iso2="US",
            record_hash=party_record_hash(
                {"name": "Example Manufacturing 42", "country_iso2": "US"}
            ),
        )
        db.add(party)
        db.flush()
        db.add(
            ScreeningTask(
                trigger="party_change",
                csl_version_id=refreshed.version.id,
                party_ids=[party.id],
                entry_ids=[],
            )
        )
        db.commit()
        source_id, version_id = source.id, refreshed.version.id

    actual_execute = engine._execute

    def fail_list_task(db, **kwargs):
        if kwargs["trigger"] == "list_change":
            raise RuntimeError("private-child-exception")
        return actual_execute(db, **kwargs)

    def fixture_sync(db, config, source):
        source.label = "Committed fixture poll"
        source.record_count = 1
        db.commit()
        return {"unchanged": 1, "record_count": 1}

    monkeypatch.setattr(engine, "_execute", fail_list_task)
    monkeypatch.setitem(SOURCE_HANDLERS, "qbo", fixture_sync)
    if parent == "csl":
        file = tmp_path / "complete.json"
        file.write_bytes(fixture)
        command = ["csl", "refresh", "--file", str(file)]
    else:
        command = ["sync", source_id]
    response = operator_runner.invoke(cli, command)
    assert response.exit_code == 1, response.output
    assert "private-child" not in response.output
    result = json.loads(response.output)
    outcome = result["outcome"]
    assert result["status"] == "failed"
    assert outcome["failed_task_ids"] == [failed_task_id]
    with factory() as db:
        runs = list(db.scalars(select(ScreeningRun)))
        assert len(runs) == 1 and runs[0].finished_at is not None
        assert outcome["run_ids"] == [runs[0].id]
        child = db.get(OperationAttempt, outcome["screening_attempt_id"])
        assert child.status == outcome["screening_status"] == "failed"
        assert child.outcome["run_ids"] == outcome["run_ids"]
        assert db.get(OperationAttempt, result["id"]).outcome == outcome
        if parent == "csl":
            assert outcome["version_id"] == version_id
            assert db.get(CSLFetch, outcome["fetch_id"]).status == "success"
            assert not outcome["changed"]
        else:
            assert outcome["source_id"] == source_id and outcome["record_count"] == 1
            assert db.get(Source, source_id).label == "Committed fixture poll"


def test_csl_api_wakes_scheduler_after_committed_fixture_refresh(client, admin, monkeypatch):
    factory = client.app.state.session_factory
    checked = []

    class Runtime:
        def wake(self):
            with factory() as db:
                db.execute(text("BEGIN IMMEDIATE"))
                task = db.scalar(select(ScreeningTask))
                assert task.status == "pending"
                assert db.scalar(select(OperationLease.owner_id)) is None
                checked.append(task.id)
                db.rollback()

    monkeypatch.setattr("app.api.csl.current_runtime", lambda: Runtime())
    monkeypatch.setattr(
        "app.api.csl.refresh_csl",
        lambda db, **kwargs: refresh_csl(db, data=envelope([listing(42)])),
    )
    response = client.post("/api/v1/csl/refresh", headers={"x-csrf-token": admin["csrf_token"]})
    assert response.status_code == 200, response.text
    assert checked == [response.json()["pending_task_id"]]


def test_cli_fixture_refresh_sync_and_screen_modes_persist_real_evidence(
    client, admin, operator_runner, tmp_path, monkeypatch
):
    factory = client.app.state.session_factory
    complete = tmp_path / "csl.json"
    complete.write_bytes(envelope([listing(42)]))
    refreshed = operator_runner.invoke(cli, ["csl", "refresh", "--file", str(complete)])
    assert refreshed.exit_code == 0, refreshed.output
    assert json.loads(refreshed.output)["version"]["entry_count"] == 1
    with factory() as db:
        source = Source(kind="qbo", label="Fixture API books")
        db.add(source)
        db.commit()
        source_id = source.id

    def fixture_sync(db, config, source):
        party = Party(
            source_id=source.id,
            external_id="fixture-qbo-42",
            name="Example Manufacturing 42",
            country_iso2="US",
            record_hash=party_record_hash(
                {"name": "Example Manufacturing 42", "country_iso2": "US"}
            ),
            raw={"fixture": True},
        )
        db.add(party)
        db.flush()
        db.add(
            ScreeningTask(
                trigger="party_change",
                csl_version_id=current_version(db).id,
                party_ids=[party.id],
                entry_ids=[],
            )
        )
        db.commit()
        return {"added": 1, "record_count": 1}

    monkeypatch.setitem(SOURCE_HANDLERS, "qbo", fixture_sync)
    synced = operator_runner.invoke(cli, ["sync", source_id])
    assert synced.exit_code == 0, synced.output
    assert json.loads(synced.output)["outcome"]["added"] == 1
    with factory() as db:
        party_id = db.scalar(select(Party.id))
        assert db.scalar(select(Hit)).party_snapshot["name"] == "Example Manufacturing 42"
        assert all(task.status == "complete" for task in db.scalars(select(ScreeningTask)))
    for arguments in (["--all"], ["--party", party_id], ["--changed"]):
        response = operator_runner.invoke(cli, ["screen", *arguments])
        assert response.exit_code == 0, response.output
    with factory() as db:
        operations = list(db.scalars(select(OperationAttempt)))
        assert all(row.status == "success" for row in operations)
        assert {"csl_refresh", "source_sync", "screen_all", "screen_party", "screen_changed"} <= {
            row.operation for row in operations
        }


def test_cli_report_alert_and_disabled_retention_use_real_operation_contract(
    client, admin, review_seed, operator_runner, monkeypatch
):
    configure(client, admin, likely_alerts_enabled=True)
    delivered = []
    monkeypatch.setattr(
        "app.notifications.service.send_message",
        lambda settings, **message: delivered.append(message),
    )
    screened = operator_runner.invoke(cli, ["screen", "--party", review_seed["party_id"]])
    assert screened.exit_code == 0, screened.output
    assert len(delivered) == 2
    alerted = operator_runner.invoke(cli, ["alert", "send"])
    assert alerted.exit_code == 0, alerted.output
    assert len(delivered) == 2  # Already-delivered recipients are not resent.
    reported = operator_runner.invoke(cli, ["report", "send"])
    assert reported.exit_code == 0, reported.output
    assert len(delivered) == 4
    factory = client.app.state.session_factory
    before = business_counts(factory)
    retained = operator_runner.invoke(cli, ["retention", "apply"])
    assert retained.exit_code == 0, retained.output
    assert json.loads(retained.output)["outcome"] == {
        "enabled": False,
        "hits": 0,
        "dispositions": 0,
    }
    assert business_counts(factory) == before
    with factory() as db:
        assert {batch.kind for batch in db.scalars(select(NotificationBatch))} == {
            "morning",
            "likely",
        }
        assert all(batch.status == "sent" for batch in db.scalars(select(NotificationBatch)))


@pytest.mark.parametrize(
    ("selection", "format"),
    [("party", "csv"), ("party", "pdf"), ("run", "csv"), ("run", "pdf"), ("all", "zip")],
)
def test_cli_export_explicit_file_has_exact_checksum_and_recorded_evidence(
    client, admin, review_seed, operator_runner, tmp_path, selection, format
):
    output = tmp_path / "chosen-directory" / f"evidence.{format}"
    scope = (
        ["--all"]
        if selection == "all"
        else [
            f"--{selection}",
            review_seed["party_id"] if selection == "party" else review_seed["runs"][0],
        ]
    )
    missing_output = operator_runner.invoke(cli, ["export", *scope, "--format", format])
    assert missing_output.exit_code != 0 and not output.exists()
    response = operator_runner.invoke(
        cli, ["export", *scope, "--format", format, "--output", str(output)]
    )
    assert response.exit_code == 0, response.output
    receipt = json.loads(response.output)
    body = output.read_bytes()
    assert receipt == {
        "output": str(output),
        "sha256": hashlib.sha256(body).hexdigest(),
        "size": len(body),
    }
    if format == "pdf":
        assert "Ácme Manufacturing" in "\n".join(
            page.extract_text() for page in PdfReader(io.BytesIO(body)).pages
        )
    elif format == "csv":
        assert "Ácme Manufacturing" in body.decode("utf-8-sig")
    else:
        with zipfile.ZipFile(output) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["selection"]["party_count"] == 2
            for path, expected in manifest["files"].items():
                assert hashlib.sha256(archive.read(path)).hexdigest() == expected["sha256"]
    with client.app.state.session_factory() as db:
        event = db.scalar(select(AuditLog).where(AuditLog.action == "export.created"))
        assert event.after["sha256"] == receipt["sha256"]
