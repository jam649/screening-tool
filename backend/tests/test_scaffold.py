import pytest
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import inspect, select, text, update
from sqlalchemy.exc import IntegrityError

from app.api.deps import admin_user
from app.audit import ImmutableAuditError, audit
from app.config import DISCLAIMER, Config
from app.models import AuditLog, User, UserSession
from app.security import COOKIE_NAME, decrypt_json, encrypt_json

SETUP = {
    "email": "admin@example.com",
    "display_name": "Admin",
    "password": "an adequately long test password",
    "disclaimer_acknowledged": True,
}


def test_secret_required_and_cipher_roundtrip(monkeypatch, config):
    monkeypatch.delenv("SCREENING_TOOL_SECRET_KEY", raising=False)
    with pytest.raises(ValidationError):
        Config()
    with pytest.raises(ValidationError):
        Config(secret_key="not a valid key")
    encrypted = encrypt_json({"api_key": "private-test-value"}, config)
    assert "private-test-value" not in encrypted
    assert decrypt_json(encrypted, config) == {"api_key": "private-test-value"}
    assert decrypt_json(encrypt_json({}, config), config) == {}
    other = Config(secret_key=Fernet.generate_key().decode())
    with pytest.raises(InvalidToken):
        decrypt_json(encrypted, other)


def test_baseline_tables_and_migration_reentry(client, app):
    from app.database import migrate

    required = {
        "users",
        "sources",
        "parties",
        "csl_versions",
        "csl_entries",
        "screening_runs",
        "hits",
        "dispositions",
        "suppressions",
        "settings",
        "audit_log",
        "sessions",
    }
    assert required <= set(inspect(app.state.engine).get_table_names())
    migrate(app.state.engine)
    assert client.get("/api/v1/setup").json()["needs_setup"]
    with app.state.engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"


def test_setup_requires_ack_creates_one_admin_and_audit(client, app):
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get("/api/v1/dashboard").status_code == 401
    assert client.get("/api/v1/setup").json()["disclaimer"] == DISCLAIMER
    assert (
        client.post("/api/v1/setup", json={**SETUP, "disclaimer_acknowledged": False}).status_code
        == 422
    )
    response = client.post("/api/v1/setup", json=SETUP)
    assert response.status_code == 201
    assert response.json()["user"]["role"] == "admin"
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    assert "password" not in response.text
    assert not client.get("/api/v1/setup").json()["needs_setup"]
    assert client.get("/api/v1/auth/me").json()["user"]["email"] == SETUP["email"]
    assert (
        client.post("/api/v1/setup", json={**SETUP, "email": "second@example.com"}).status_code
        == 409
    )
    with app.state.session_factory() as db:
        users = db.scalars(select(User)).all()
        assert len(users) == 1
        assert users[0].password_hash.startswith("$argon2id$")
        assert SETUP["password"] not in users[0].password_hash
        actions = db.scalars(select(AuditLog)).all()
        assert {"users.create", "setup.disclaimer_acknowledged"} <= {row.action for row in actions}
        assert SETUP["password"] not in str([row.after for row in actions])
        session = db.scalar(select(UserSession))
        assert session.token_hash != client.cookies[COOKIE_NAME]
        assert session.expires_at.tzinfo is not None


def test_auth_csrf_logout_login_rate_limit_and_inactive(client, app, admin):
    assert client.post("/api/v1/auth/logout").status_code == 403
    csrf = {"X-CSRF-Token": admin["csrf_token"]}
    assert client.post("/api/v1/auth/logout", headers=csrf).status_code == 204
    assert client.get("/api/v1/auth/me").status_code == 401
    good = client.post(
        "/api/v1/auth/login",
        json={"email": SETUP["email"], "password": "test password with enough length"},
    )
    assert good.status_code == 200
    with app.state.session_factory() as db:
        user = db.get(User, admin["user"]["id"])
        user.active = False
        db.commit()
    assert client.get("/api/v1/auth/me").status_code == 401
    for _ in range(5):
        assert (
            client.post(
                "/api/v1/auth/login", json={"email": SETUP["email"], "password": "wrong"}
            ).status_code
            == 401
        )
    blocked = client.post("/api/v1/auth/login", json={"email": SETUP["email"], "password": "wrong"})
    assert blocked.status_code == 429 and "Retry-After" in blocked.headers
    # A process restart does not reset the database-backed limiter.
    with TestClient(app, base_url="https://testserver") as second:
        assert (
            second.post(
                "/api/v1/auth/login", json={"email": SETUP["email"], "password": "wrong"}
            ).status_code
            == 429
        )


def test_origin_and_roles(client, app, admin):
    @app.get("/api/v1/test-admin", dependencies=[Depends(admin_user)])
    def admin_only():
        return {"ok": True}

    app.router.routes.insert(0, app.router.routes.pop())
    assert client.get("/api/v1/test-admin").status_code == 200
    assert (
        client.post(
            "/api/v1/auth/logout",
            headers={
                "X-CSRF-Token": admin["csrf_token"],
                "Origin": "https://attacker.example",
            },
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/auth/login",
            json={"email": SETUP["email"], "password": "wrong"},
            headers={"Origin": "https://attacker.example"},
        ).status_code
        == 403
    )
    with app.state.session_factory() as db:
        user = db.get(User, admin["user"]["id"])
        user.role = "reviewer"
        db.commit()
    assert client.get("/api/v1/dashboard").status_code == 200
    assert client.get("/api/v1/test-admin").status_code == 403


def test_expired_session(client, app, admin):
    from datetime import timedelta

    from app.models import utcnow

    with app.state.session_factory() as db:
        row = db.scalar(select(UserSession))
        row.expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert client.get("/api/v1/auth/me").status_code == 401


def test_audit_insert_only_orm_and_sql(client, app, admin):
    with app.state.session_factory() as db:
        row = audit(db, "test.record", "users", admin["user"]["id"], after={"value": 1})
        db.commit()
        row.action = "rewritten"
        with pytest.raises(ImmutableAuditError):
            db.commit()
        db.rollback()
        db.delete(row)
        with pytest.raises(ImmutableAuditError):
            db.commit()
        db.rollback()
        with pytest.raises(ImmutableAuditError):
            db.execute(update(AuditLog).values(action="rewrite"))
        db.rollback()
    for sql in (
        "UPDATE audit_log SET action='rewrite'",
        "DELETE FROM audit_log",
        "INSERT OR REPLACE INTO audit_log SELECT * FROM audit_log LIMIT 1",
    ):
        with app.state.engine.begin() as connection, pytest.raises(IntegrityError):
            connection.execute(text(sql))
    with app.state.session_factory() as db:
        assert db.scalar(select(AuditLog).where(AuditLog.action == "test.record")) is not None


def test_serves_spa_and_does_not_mask_missing_api(client):
    assert client.get("/").status_code == 200
    assert "screening-tool setup" in client.get("/parties/example").text
    assert client.get("/api/v1/does-not-exist").status_code == 404
    assert client.get("/api/v1/health").json() == {"status": "ok"}
    assert client.get("/api/v1/setup").headers["cache-control"] == "no-store"


def test_concurrent_setup_only_one_admin(client, app):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda email: client.post("/api/v1/setup", json={**SETUP, "email": email}),
                ["first@example.com", "second@example.com"],
            )
        )
    assert sorted(response.status_code for response in responses) == [201, 409]
    with app.state.session_factory() as db:
        assert len(db.scalars(select(User)).all()) == 1
        create = db.scalar(select(AuditLog).where(AuditLog.action == "users.create"))
        assert create.actor_user_id == create.target_id


def test_schema_checks_and_historical_evidence(client, app):
    from app.models import CSLEntry, CSLVersion, Hit, Party, ScreeningRun, Source

    with app.state.session_factory() as db:
        source = Source(kind="csv", label="Test source")
        version = CSLVersion(file_sha256="a" * 64, entry_count=1)
        db.add_all([source, version])
        db.flush()
        party = Party(
            source_id=source.id, external_id="party-1", name="Example", record_hash="b" * 64
        )
        entry = CSLEntry(
            csl_id="entry-1",
            source_list="SDN",
            name="Example",
            entry_hash="c" * 64,
            first_seen_version=version.id,
            last_seen_version=version.id,
        )
        run = ScreeningRun(trigger="manual", csl_version_id=version.id)
        db.add_all([party, entry, run])
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
            party_snapshot={"name": "Example"},
            entry_snapshot={"name": "Example"},
        )
        db.add(hit)
        db.commit()
        party.name = "A changed name"
        db.commit()
        assert hit.party_snapshot["name"] == "Example"
        hit.party_snapshot = {"name": "rewritten"}
        with pytest.raises(ImmutableAuditError):
            db.commit()
        db.rollback()
        with pytest.raises(IntegrityError):
            db.execute(text("UPDATE hits SET party_snapshot='{}'"))
        db.rollback()
        with pytest.raises(IntegrityError):
            db.execute(text("UPDATE hits SET det_score=101"))
        db.rollback()
        with pytest.raises(IntegrityError):
            db.execute(text("DELETE FROM parties"))
        db.rollback()


def test_cli_init_and_no_migration_drift(tmp_path, config, monkeypatch):
    from alembic.config import Config as AlembicConfig
    from typer.testing import CliRunner

    from alembic import command
    from app.cli import cli
    from app.database import create_database_engine

    monkeypatch.setenv("SCREENING_TOOL_SECRET_KEY", config.secret_key.get_secret_value())
    monkeypatch.setenv("SCREENING_TOOL_DATABASE_URL", config.database_url)
    result = CliRunner().invoke(cli, ["init"])
    assert result.exit_code == 0, result.output
    assert "Database ready" in result.output
    engine = create_database_engine(config)
    with engine.begin() as connection:
        migration = AlembicConfig("alembic.ini")
        migration.attributes["connection"] = connection
        command.check(migration)
    engine.dispose()
