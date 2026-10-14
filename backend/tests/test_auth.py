from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import AuditLog, LoginAttempt
from app.security import digest, login_identifier

PASSWORD = "test password with enough length"


def test_successful_logins_never_consume_failure_budget(client, app, admin):
    for _ in range(8):
        response = client.post(
            "/api/v1/auth/login", json={"email": "admin@example.com", "password": PASSWORD}
        )
        assert response.status_code == 200
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(LoginAttempt)) == 0


def test_success_clears_only_matching_failed_identity_and_address(client, app, admin):
    for identity in ("admin@example.com", "missing@example.com"):
        assert (
            client.post(
                "/api/v1/auth/login", json={"email": identity, "password": "wrong"}
            ).status_code
            == 401
        )
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": "admin@example.com", "password": PASSWORD}
        ).status_code
        == 200
    )
    with app.state.session_factory() as db:
        attempts = db.scalars(select(LoginAttempt)).all()
        assert len(attempts) == 1
        assert attempts[0].identity_hash == login_identifier(
            "missing@example.com", "identity", app.state.config
        )
        assert attempts[0].identity_hash != digest("missing@example.com")
        failed = db.scalars(select(AuditLog).where(AuditLog.action == "auth.login_failed")).all()
        assert failed[0].target_id == admin["user"]["id"]
        assert failed[1].target_id is None
        assert "admin@example.com" not in str([row.after for row in failed])
        assert "missing@example.com" not in str([row.after for row in failed])


def test_parallel_failed_attempts_are_serialized_and_survive_restart(client, app, admin):
    def fail(_):
        return client.post(
            "/api/v1/auth/login", json={"email": "admin@example.com", "password": "wrong"}
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(fail, range(8)))
    assert sorted(statuses) == [401] * 5 + [429] * 3
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(LoginAttempt)) == 5
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "auth.login_failed")
            )
            == 5
        )
    with TestClient(app, base_url="https://testserver") as restarted:
        assert (
            restarted.post(
                "/api/v1/auth/login", json={"email": "admin@example.com", "password": "wrong"}
            ).status_code
            == 429
        )


def test_nonascii_csrf_is_rejected_without_server_error(client, admin):
    # HTTP transport header bytes are Latin-1; the application still receives Unicode.
    response = client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": b"\xff"})
    assert response.status_code == 403


def test_on_premise_email_can_setup_and_login(client):
    assert (
        client.post(
            "/api/v1/setup",
            json={
                "email": " Admin@corp.local ",
                "display_name": "Local admin",
                "password": PASSWORD,
                "disclaimer_acknowledged": True,
            },
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": "ADMIN@corp.local", "password": PASSWORD}
        ).status_code
        == 200
    )


def test_invalid_setup_email_and_untrusted_host_are_rejected(client):
    assert (
        client.post(
            "/api/v1/setup",
            json={
                "email": "no-address",
                "display_name": "Local admin",
                "password": PASSWORD,
                "disclaimer_acknowledged": True,
            },
        ).status_code
        == 422
    )
    assert client.get("/api/v1/setup", headers={"Host": "attacker.example"}).status_code == 400


def test_explicit_origin_rejects_wrong_scheme_and_allows_configured_origin(client, app):
    app.state.config.allowed_origins = ["https://testserver"]
    body = {"email": "nobody@example.com", "password": "wrong"}
    assert (
        client.post(
            "/api/v1/auth/login", json=body, headers={"Origin": "http://testserver"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/auth/login", json=body, headers={"Origin": "https://testserver"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/auth/login", json=body, headers={"Origin": "https://[malformed"}
        ).status_code
        == 403
    )
