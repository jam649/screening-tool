"""Operator behavior uses real migrated databases, leases and private artifacts."""

import hashlib
import json
import os
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from test_bc_auth import bc_setup as bc_setup
from test_bc_auth import connect, write
from test_review_api import review_seed as review_seed
from typer.testing import CliRunner

from app import cli as operator
from app.models import AuditLog, OperationAttempt, OperationLease, Setting, utcnow
from app.private_artifacts import write_private_artifact
from app.reports.export import ExportArtifact
from app.scheduler.leases import protected_operation
from app.scheduler.service import enqueue_operation


def env(monkeypatch, config):
    monkeypatch.setenv("SCREENING_TOOL_SECRET_KEY", config.secret_key.get_secret_value())
    monkeypatch.setenv("SCREENING_TOOL_DATABASE_URL", config.database_url)
    monkeypatch.setenv("SCREENING_TOOL_SCHEDULER_ENABLED", "false")


def artifact(body=b"Synthetic private evidence", *, path=None):
    return ExportArtifact(
        "synthetic.csv",
        "text/csv",
        hashlib.sha256(body).hexdigest(),
        len(body),
        body=None if path else body,
        path=path,
    )


def mounted_operator(monkeypatch, client):
    @contextmanager
    def database():
        yield client.app.state.config, client.app.state.session_factory

    monkeypatch.setattr(operator, "operator_database", database)


def test_init_unbound_then_matching_key_creates_one_binding_and_disposes(config, monkeypatch):
    env(monkeypatch, config)
    engines = []
    create = operator.create_database_engine

    def recording(settings):
        engine = create(settings)
        original = engine.dispose
        calls = []

        def dispose(*args, **kwargs):
            calls.append(True)
            original(*args, **kwargs)

        monkeypatch.setattr(engine, "dispose", dispose)
        engines.append((engine, calls))
        return engine

    monkeypatch.setattr(operator, "create_database_engine", recording)
    for _ in range(2):
        result = CliRunner().invoke(operator.cli, ["init"])
        assert result.exit_code == 0 and "Database ready" in result.output
    assert all(calls == [True] for _, calls in engines)
    with operator.session_factory(engines[-1][0], config)() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(Setting)
                .where(Setting.key == "__encryption_key_canary")
            )
            == 1
        )
        assert db.scalar(select(func.count()).select_from(AuditLog)) == 1
    engines[-1][0].dispose()


def test_init_mismatched_key_fails_before_migrations_and_changes_nothing(
    client, admin, config, monkeypatch
):
    env(monkeypatch, config)
    with client.app.state.session_factory() as db:
        count = db.scalar(select(func.count()).select_from(AuditLog))
        canary = db.scalar(select(Setting).where(Setting.key == "__encryption_key_canary"))
        fingerprint = hashlib.sha256(str(canary.value).encode()).hexdigest()
    monkeypatch.setenv("SCREENING_TOOL_SECRET_KEY", Fernet.generate_key().decode())
    migrations = []
    monkeypatch.setattr(operator, "migrate", lambda engine: migrations.append(engine))
    result = CliRunner().invoke(operator.cli, ["init"])
    assert result.exit_code == 1 and "Database ready" not in result.output
    assert result.output.strip() == (
        "Database initialization failed; check the original encryption key, "
        "configuration and database permissions."
    )
    assert not migrations
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AuditLog)) == count
        canary = db.scalar(select(Setting).where(Setting.key == "__encryption_key_canary"))
        assert hashlib.sha256(str(canary.value).encode()).hexdigest() == fingerprint


def test_init_migration_failure_is_sanitized_and_engine_disposed(config, monkeypatch):
    env(monkeypatch, config)
    engine = operator.create_database_engine(config)
    calls = []
    dispose = engine.dispose

    def close():
        calls.append(True)
        dispose()

    monkeypatch.setattr(engine, "dispose", close)
    monkeypatch.setattr(operator, "create_database_engine", lambda settings: engine)

    def failed(*args):
        raise RuntimeError("private database failure details")

    monkeypatch.setattr(operator, "migrate", failed)
    result = CliRunner().invoke(operator.cli, ["init"])
    assert result.exit_code == 1 and calls == [True]
    assert "private database failure details" not in result.output
    assert "Database ready" not in result.output


@pytest.mark.parametrize("streamed", [False, True])
def test_private_export_atomic_mode_ignores_permissive_umask(tmp_path, monkeypatch, streamed):
    source = tmp_path / "source.zip"
    source.write_bytes(b"Synthetic private evidence")
    destination = tmp_path / "new" / "private.zip"
    content = artifact(path=source if streamed else None)
    observed = []
    link = os.link

    def install(owned, final, **kwargs):
        assert not destination.exists()
        assert Path(owned).stat().st_mode & 0o777 == 0o600
        assert Path(owned).read_bytes() == b"Synthetic private evidence"
        observed.append(True)
        link(owned, final, **kwargs)

    monkeypatch.setattr(os, "link", install)
    previous = os.umask(0)
    try:
        write_private_artifact(content, destination)
    finally:
        os.umask(previous)
    assert observed == [True]
    assert destination.stat().st_mode & 0o777 == 0o600
    assert destination.read_bytes() == b"Synthetic private evidence"
    assert not list(destination.parent.glob(".screening-export-*"))


@pytest.mark.parametrize("kind", ["file", "symlink", "dangling"])
def test_existing_output_is_never_overwritten(tmp_path, kind):
    original = tmp_path / "original"
    original.write_bytes(b"Original operator evidence")
    destination = original if kind == "file" else tmp_path / "output"
    if kind != "file":
        destination.symlink_to(original if kind == "symlink" else tmp_path / "absent")
    with pytest.raises(FileExistsError):
        write_private_artifact(artifact(), destination)
    assert original.read_bytes() == b"Original operator evidence"
    assert not list(tmp_path.glob(".screening-export-*"))
    if kind != "file":
        assert destination.is_symlink()


def test_racing_output_wins_and_temporary_artifact_is_removed(tmp_path, monkeypatch):
    destination = tmp_path / "output"
    link = os.link

    def raced(owned, final, **kwargs):
        destination.write_bytes(b"Concurrent operator evidence")
        link(owned, final, **kwargs)

    monkeypatch.setattr(os, "link", raced)
    with pytest.raises(FileExistsError):
        write_private_artifact(artifact(), destination)
    assert destination.read_bytes() == b"Concurrent operator evidence"
    assert not list(tmp_path.glob(".screening-export-*"))


@pytest.mark.parametrize("failure", ["fsync", "truncated_source"])
def test_failed_write_never_publishes_partial_or_leaves_temporary(tmp_path, monkeypatch, failure):
    destination = tmp_path / "output"
    source = tmp_path / "source"
    source.write_bytes(b"truncated")
    content = artifact(path=source) if failure == "truncated_source" else artifact()
    if failure == "fsync":

        def no_space(*args):
            raise OSError("synthetic disk failure")

        monkeypatch.setattr(os, "fsync", no_space)
    with pytest.raises((OSError, ValueError)):
        write_private_artifact(content, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".screening-export-*"))


@pytest.mark.parametrize("selection", ["party", "run", "zip", "training"])
def test_actual_cli_exports_private_new_files_and_preserves_existing(
    client, admin, config, review_seed, tmp_path, monkeypatch, selection
):
    env(monkeypatch, config)
    output = tmp_path / (selection + ".artifact")
    command = {
        "party": ["export", "--party", review_seed["party_id"], "--format", "csv"],
        "run": ["export", "--run", review_seed["runs"][0], "--format", "pdf"],
        "zip": ["export", "--all", "--format", "zip"],
        "training": ["export", "training-pairs"],
    }[selection] + ["--output", str(output)]
    result = CliRunner().invoke(operator.cli, command)
    assert result.exit_code == 0
    summary = json.loads(result.output)
    assert summary["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert summary["size"] == output.stat().st_size
    assert output.stat().st_mode & 0o777 == 0o600
    assert "Ácme" not in result.output
    original = output.read_bytes()
    with client.app.state.session_factory() as db:
        before = db.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "export.created")
        )
    repeated = CliRunner().invoke(operator.cli, command)
    assert repeated.exit_code == 1 and "already exists" in repeated.output
    assert output.read_bytes() == original
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "export.created")
            )
            == before
        )


def test_cli_bc_subscriptions_uses_real_fenced_service_and_vendor_contract(
    client, admin, bc_setup, monkeypatch
):
    source_id, vendor = bc_setup
    connect(client, admin, source_id)
    assert (
        write(
            client,
            admin,
            "patch",
            f"/sources/{source_id}/bc/connection",
            {"webhooks_enabled": True},
        ).status_code
        == 202
    )
    mounted_operator(monkeypatch, client)
    result = CliRunner().invoke(operator.cli, ["bc", "subscriptions", source_id])
    assert result.exit_code == 0
    summary = json.loads(result.output)
    assert summary["operation"] == "bc_subscriptions" and summary["status"] == "success"
    assert summary["outcome"]["subscription_count"] == 2
    assert vendor.creations == {"customers": 1, "vendors": 1}
    assert vendor.challenge_count == 2
    with client.app.state.session_factory() as db:
        attempt = db.get(OperationAttempt, summary["id"])
        assert attempt.source_id == source_id and attempt.operation == "bc_subscriptions"
    before = dict(vendor.calls)
    with protected_operation(client.app.state.session_factory, "source_sync", source_id=source_id):
        blocked = CliRunner().invoke(operator.cli, ["bc", "subscriptions", source_id])
        assert blocked.exit_code == 1
        assert json.loads(blocked.output)["status"] == "skipped"
    assert dict(vendor.calls) == before


def test_cli_recovery_runs_actual_queued_jobs_and_truthfully_fails_partial(
    client, admin, review_seed, monkeypatch
):
    factory = client.app.state.session_factory
    good = enqueue_operation(
        factory,
        client.app.state.config,
        "screen_party",
        party_id=review_seed["party_id"],
        actor_user_id=admin["user"]["id"],
    )
    bad = enqueue_operation(
        factory, client.app.state.config, "morning_report", actor_user_id=admin["user"]["id"]
    )
    mounted_operator(monkeypatch, client)
    result = CliRunner().invoke(operator.cli, ["monitor", "recover"])
    assert result.exit_code == 1
    summary = json.loads(result.output)
    assert (
        summary["operation"] == "dispatch_pending"
        and summary["completed"] == 1
        and summary["failed"] == 1
    )
    assert {row["id"] for row in summary["attempts"]} == {good["attempt_id"], bad["attempt_id"]}
    assert "Configure SMTP" not in result.output
    with factory() as db:
        assert db.get(OperationAttempt, good["attempt_id"]).status == "success"
        assert db.get(OperationAttempt, bad["attempt_id"]).status == "failed"
    empty = CliRunner().invoke(operator.cli, ["monitor", "recover"])
    assert empty.exit_code == 0 and json.loads(empty.output)["attempts"] == []


def test_cli_recovery_requeues_dead_owner_and_preserves_party_scope(
    client, admin, review_seed, monkeypatch
):
    factory = client.app.state.session_factory
    with factory() as db:
        db.add(
            OperationLease(
                key="screening",
                owner_id="dead:fixture",
                owner_pid=99999999,
                fence=3,
                expires_at=utcnow() + timedelta(minutes=5),
            )
        )
        attempt = OperationAttempt(
            operation="screen_party",
            party_id=review_seed["party_id"],
            actor_user_id=admin["user"]["id"],
            status="running",
            parameters={},
        )
        db.add(attempt)
        db.commit()
        interrupted_id = attempt.id
    mounted_operator(monkeypatch, client)
    result = CliRunner().invoke(operator.cli, ["monitor", "recover"])
    assert result.exit_code == 0
    summary = json.loads(result.output)
    assert summary["completed"] == 1
    replacement = summary["attempts"][0]
    assert (
        replacement["party_id"] == review_seed["party_id"]
        and replacement["operation"] == "screen_party"
    )
    assert replacement["id"] != interrupted_id
    with factory() as db:
        assert db.get(OperationAttempt, interrupted_id).status == "interrupted"
        assert db.get(OperationAttempt, replacement["id"]).actor_user_id == admin["user"]["id"]
        event = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "monitoring.interrupted", AuditLog.target_id == interrupted_id
            )
        )
        assert event.after["retry_attempt_id"] == replacement["id"]


@pytest.mark.parametrize(
    "command", [["retention", "preview"], ["monitor", "recover"], ["hashes", "upgrade"]]
)
def test_noninit_wrong_key_rejected_before_migration_or_business_write_and_disposes(
    client, admin, config, monkeypatch, command
):
    env(monkeypatch, config)
    with client.app.state.session_factory() as db:
        before_audit = db.scalar(select(func.count()).select_from(AuditLog))
        before_settings = db.scalar(select(func.count()).select_from(Setting))
        canary = db.scalar(select(Setting).where(Setting.key == "__encryption_key_canary"))
        original_hash = hashlib.sha256(str(canary.value).encode()).hexdigest()
    monkeypatch.setenv("SCREENING_TOOL_SECRET_KEY", Fernet.generate_key().decode())
    create = operator.create_database_engine
    disposals, migrations, business_calls = [], [], []

    def recording(settings):
        engine = create(settings)
        dispose = engine.dispose

        def closed():
            disposals.append(True)
            dispose()

        monkeypatch.setattr(engine, "dispose", closed)
        return engine

    monkeypatch.setattr(operator, "create_database_engine", recording)
    monkeypatch.setattr(operator, "migrate", lambda engine: migrations.append(True))
    monkeypatch.setattr(operator, "upgrade_hash_rules", lambda db: business_calls.append(True))
    monkeypatch.setattr(operator, "dispatch_pending", lambda *args: business_calls.append(True))
    monkeypatch.setattr(operator, "purge_plan", lambda *args: business_calls.append(True))
    result = CliRunner().invoke(operator.cli, command)
    assert result.exit_code == 1 and "Database ready" not in result.output
    assert not migrations and not business_calls
    assert disposals == [True]
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AuditLog)) == before_audit
        assert db.scalar(select(func.count()).select_from(Setting)) == before_settings
        assert db.scalar(select(func.count()).select_from(OperationAttempt)) == 0
        canary = db.scalar(select(Setting).where(Setting.key == "__encryption_key_canary"))
        assert hashlib.sha256(str(canary.value).encode()).hexdigest() == original_hash


@pytest.fixture
def unbound_legacy_store(config):
    """A real schema with existing encrypted secrets but no key-binding canary."""
    from app.models import Source
    from app.security import encrypt_json

    engine = operator.create_database_engine(config)
    operator.migrate(engine)
    factory = operator.session_factory(engine, config)
    with factory() as db:
        db.add_all(
            [
                Setting(
                    key="legacy_private_key",
                    value=encrypt_json({"dummy": "setting"}, config),
                    encrypted=True,
                ),
                Source(
                    kind="csv",
                    label="Synthetic legacy source",
                    credentials_enc=encrypt_json({"dummy": "source"}, config),
                ),
            ]
        )
        db.commit()
    try:
        yield engine, factory
    finally:
        engine.dispose()


def test_premigration_validation_is_readonly_then_postmigration_binds(
    config, monkeypatch, unbound_legacy_store
):
    from sqlalchemy import event

    engine, factory = unbound_legacy_store
    with factory() as db:
        before_audit = db.scalar(select(func.count()).select_from(AuditLog))
        before_settings = db.scalar(select(func.count()).select_from(Setting))
    writes, migration_checks = [], []

    def record(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
            writes.append(statement.lstrip().split()[0].upper())

    event.listen(engine, "before_cursor_execute", record)

    def migration_probe(actual_engine):
        assert actual_engine is engine
        assert not writes
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(AuditLog)) == before_audit
            assert db.scalar(select(func.count()).select_from(Setting)) == before_settings
            assert (
                db.scalar(select(Setting).where(Setting.key == "__encryption_key_canary")) is None
            )
        migration_checks.append(True)

    monkeypatch.setattr(operator, "migrate", migration_probe)
    operator._migrate_with_key_binding(engine, config)
    assert migration_checks == [True]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Setting)) == before_settings + 1
        assert db.scalar(select(func.count()).select_from(AuditLog)) == before_audit + 1
        assert (
            db.scalar(select(Setting).where(Setting.key == "__encryption_key_canary")) is not None
        )


def test_failed_migration_does_not_bind_unbound_store_or_append_audit(
    config, monkeypatch, unbound_legacy_store
):
    engine, factory = unbound_legacy_store
    env(monkeypatch, config)
    with factory() as db:
        before_audit = db.scalar(select(func.count()).select_from(AuditLog))
        before_settings = db.scalar(select(func.count()).select_from(Setting))
    monkeypatch.setattr(operator, "create_database_engine", lambda settings: engine)

    def fail_migration(actual_engine):
        raise RuntimeError("private migration failure")

    monkeypatch.setattr(operator, "migrate", fail_migration)
    result = CliRunner().invoke(operator.cli, ["init"])
    assert result.exit_code == 1 and "Database ready" not in result.output
    assert "private migration failure" not in result.output
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(AuditLog)) == before_audit
        assert db.scalar(select(func.count()).select_from(Setting)) == before_settings
        assert db.scalar(select(Setting).where(Setting.key == "__encryption_key_canary")) is None


@pytest.mark.parametrize("storage", ["setting", "source"])
def test_readonly_precheck_still_rejects_wrong_key_for_unbound_existing_secrets(
    config, monkeypatch, storage
):
    from app.models import Source
    from app.security import encrypt_json, validate_key_binding

    engine = operator.create_database_engine(config)
    operator.migrate(engine)
    factory = operator.session_factory(engine, config)
    try:
        with factory() as db:
            encrypted = encrypt_json({"dummy": "Synthetic encrypted secret"}, config)
            if storage == "setting":
                db.add(Setting(key="legacy_private_key", value=encrypted, encrypted=True))
            else:
                db.add(
                    Source(kind="csv", label="Synthetic legacy source", credentials_enc=encrypted)
                )
            db.commit()
            before_audit = db.scalar(select(func.count()).select_from(AuditLog))
        migrations = []
        monkeypatch.setattr(operator, "migrate", lambda actual: migrations.append(True))
        wrong_config = config.model_copy(
            update={"secret_key": type(config.secret_key)(Fernet.generate_key().decode())}
        )
        with pytest.raises(RuntimeError, match="does not match"):
            operator._migrate_with_key_binding(engine, wrong_config)
        assert not migrations
        with factory() as db:
            validate_key_binding(db, config, bind=False)
            assert db.scalar(select(func.count()).select_from(AuditLog)) == before_audit
            assert (
                db.scalar(select(Setting).where(Setting.key == "__encryption_key_canary")) is None
            )
            assert not db.new and not db.dirty
    finally:
        engine.dispose()
