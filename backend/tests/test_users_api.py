import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from app.models import AuditLog, User, UserInvitation
from app.security import digest


def invite(client, admin, email="reviewer@example.com", role="reviewer"):
    response = client.post(
        "/api/v1/users/invitations",
        json={"email": email, "role": role},
        headers={"x-csrf-token": admin["csrf_token"]},
    )
    assert response.status_code == 201
    result = response.json()
    token = parse_qs(urlsplit(result["invite_url"]).fragment)["token"][0]
    return result, token


def accept(client, token):
    return client.post(
        "/api/v1/auth/invite/accept",
        json={
            "token": token,
            "display_name": "Reviewer",
            "password": "long test reviewer password",
        },
    )


def test_invitation_hash_only_one_use_secure_session_and_roles(client, admin):
    invitation, token = invite(client, admin)
    assert invitation["delivery_pending"] is True
    assert not urlsplit(invitation["invite_url"]).query
    with client.app.state.session_factory() as db:
        row = db.get(UserInvitation, invitation["id"])
        assert row.token_hash == digest(token) and row.token_hash != token
        serialized = json.dumps(
            [{"before": row.before, "after": row.after} for row in db.scalars(select(AuditLog))],
            default=str,
        )
        assert token not in serialized and digest(token) not in serialized
    client.cookies.clear()
    assert client.post("/api/v1/auth/invite/validate", json={"token": token}).status_code == 200
    response = accept(client, token)
    assert response.status_code == 201
    cookie = response.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    reviewer = response.json()
    assert reviewer["user"]["role"] == "reviewer"
    assert accept(client, token).status_code == 400
    for path in ("users", "settings"):
        assert client.get(f"/api/v1/{path}").status_code == 403
    assert (
        client.patch(
            "/api/v1/settings",
            json={"display_name": "Forbidden"},
            headers={"x-csrf-token": reviewer["csrf_token"]},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/users/invitations",
            json={"email": "other@example.com"},
            headers={"x-csrf-token": reviewer["csrf_token"]},
        ).status_code
        == 403
    )


def test_expired_replaced_and_invalid_invites(client, admin):
    first, old_token = invite(client, admin)
    second, token = invite(client, admin)
    assert first["id"] != second["id"]
    assert client.post("/api/v1/auth/invite/validate", json={"token": old_token}).status_code == 400
    with client.app.state.session_factory() as db:
        row = db.get(UserInvitation, second["id"])
        row.expires_at -= timedelta(days=10)
        db.commit()
    assert accept(client, token).status_code == 400
    assert accept(client, "x" * 43).status_code == 400


def test_last_admin_protected_and_deactivation_revokes_session(client, admin):
    headers = {"x-csrf-token": admin["csrf_token"]}
    user_id = admin["user"]["id"]
    for change in ({"active": False}, {"role": "reviewer"}):
        assert (
            client.patch(f"/api/v1/users/{user_id}", json=change, headers=headers).status_code
            == 409
        )
    _, token = invite(client, admin, "second@example.com", "admin")
    first_cookie = client.cookies.get("screening_session")
    second = accept(client, token).json()
    client.cookies.set("screening_session", first_cookie)
    response = client.patch(
        f"/api/v1/users/{second['user']['id']}", json={"active": False}, headers=headers
    )
    assert response.status_code == 200 and response.json()["active"] is False
    assert (
        client.post(
            "/api/v1/auth/login",
            json={"email": "second@example.com", "password": "long test reviewer password"},
        ).status_code
        == 401
    )


def test_concurrent_admin_demotions_leave_one_active_admin(client, admin):
    _, token = invite(client, admin, "second@example.com", "admin")
    first_cookie = client.cookies.get("screening_session")
    second = accept(client, token).json()
    client.cookies.set("screening_session", first_cookie)
    headers = {"x-csrf-token": admin["csrf_token"]}

    def demote(user_id):
        return client.patch(
            f"/api/v1/users/{user_id}", json={"role": "reviewer"}, headers=headers
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(demote, [admin["user"]["id"], second["user"]["id"]]))
    assert results.count(200) == 1
    assert set(results) <= {200, 403, 409}
    with client.app.state.session_factory() as db:
        assert len(list(db.scalars(select(User).where(User.role == "admin", User.active)))) == 1


def test_concurrent_invitation_acceptance_creates_one_user(client, admin):
    _, token = invite(client, admin)

    def submit(_):
        return accept(client, token).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(submit, range(2)))
    assert sorted(statuses) == [201, 400]
    with client.app.state.session_factory() as db:
        assert len(list(db.scalars(select(User).where(User.email == "reviewer@example.com")))) == 1


def test_admin_can_withdraw_pending_invitation_and_cannot_revoke_accepted_user(client, admin):
    invitation, token = invite(client, admin)
    headers = {"x-csrf-token": admin["csrf_token"]}
    path = f"/api/v1/users/invitations/{invitation['id']}/revoke"
    assert client.post(path).status_code == 403
    response = client.post(path, headers=headers)
    assert response.status_code == 200 and response.json()["revoked_at"] is not None
    assert client.post(path, headers=headers).status_code == 200
    assert accept(client, token).status_code == 400
    with client.app.state.session_factory() as db:
        events = list(
            db.scalars(select(AuditLog).where(AuditLog.action == "users.invitation_revoked"))
        )
        assert len(events) == 1 and events[0].actor_user_id == admin["user"]["id"]
    invitation, token = invite(client, admin, email="accepted@example.com")
    cookie = client.cookies.get("screening_session")
    assert accept(client, token).status_code == 201
    client.cookies.set("screening_session", cookie)
    assert (
        client.post(
            f"/api/v1/users/invitations/{invitation['id']}/revoke", headers=headers
        ).status_code
        == 409
    )


def test_invitation_acceptance_and_admin_withdrawal_serialize(client, admin):
    invitation, token = invite(client, admin)
    headers = {
        "x-csrf-token": admin["csrf_token"],
        "cookie": f"screening_session={client.cookies.get('screening_session')}",
    }

    def submit(action):
        if action == "accept":
            return accept(client, token).status_code
        return client.post(
            f"/api/v1/users/invitations/{invitation['id']}/revoke", headers=headers
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        accepted, revoked = list(pool.map(submit, ["accept", "revoke"]))
    assert (accepted, revoked) in {(201, 409), (400, 200)}


@pytest.mark.parametrize("change", [{"role": "reviewer"}, {"active": False}])
def test_offboarding_admin_revokes_their_pending_privileged_invitations(client, admin, change):
    _, second_token = invite(client, admin, email="remaining-admin@example.com", role="admin")
    first_cookie = client.cookies.get("screening_session")
    assert accept(client, second_token).status_code == 201
    client.cookies.set("screening_session", first_cookie)
    invitation, token = invite(client, admin, email="future-admin@example.com", role="admin")
    response = client.patch(
        f"/api/v1/users/{admin['user']['id']}",
        json=change,
        headers={"x-csrf-token": admin["csrf_token"]},
    )
    assert response.status_code == 200
    client.cookies.clear()
    assert accept(client, token).status_code == 400
    with client.app.state.session_factory() as db:
        row = db.get(UserInvitation, invitation["id"])
        assert row.revoked_at is not None and row.used_at is None
        assert db.scalar(select(User).where(User.email == "future-admin@example.com")) is None


@pytest.mark.parametrize("change", [{"role": "reviewer"}, {"active": False}])
def test_accept_rechecks_issuer_authority_even_for_unrevoked_legacy_invitation(
    client, admin, change
):
    invitation, token = invite(client, admin, role="admin")
    with client.app.state.session_factory() as db:
        issuer = db.get(User, admin["user"]["id"])
        for key, value in change.items():
            setattr(issuer, key, value)
        db.commit()
        assert db.get(UserInvitation, invitation["id"]).revoked_at is None
    client.cookies.clear()
    assert client.post("/api/v1/auth/invite/validate", json={"token": token}).status_code == 400
    assert accept(client, token).status_code == 400
