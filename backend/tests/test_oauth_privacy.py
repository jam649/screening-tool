"""Callback credentials disappear before dependency URL/error construction."""

import pytest
from fastapi import HTTPException, Request

from app.api.deps import admin_user
from app.models import User

CALLBACK = "/api/v1/connectors/qbo/oauth/callback"


@pytest.mark.parametrize("authenticated", [False, True])
def test_callback_auth_errors_have_no_referrer_and_no_input(client, admin, authenticated):
    if not authenticated:
        client.cookies.clear()
    else:
        with client.app.state.session_factory() as db:
            db.get(User, admin["user"]["id"]).role = "reviewer"
            db.commit()
    response = client.get(
        CALLBACK + "?code=synthetic-private-code&state=synthetic-private-state&realmId=12345"
    )
    assert response.status_code == (403 if authenticated else 401)
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert "synthetic-private" not in response.text


@pytest.mark.parametrize(
    "query,accepted",
    [
        ("code=synthetic-private-code&state=synthetic-private-state&realmId=12345", True),
        ("state=first&state=second&code=synthetic-private-code", False),
        ("code=synthetic-private-code&malformed", False),
        ("code=" + "x" * 16385, False),
        ("&".join(f"field{i}=synthetic-private-code" for i in range(13)), False),
    ],
)
def test_callback_scope_scrub_precedes_dependency_and_url_construction(client, query, accepted):
    observed = {}

    def reject(request: Request):
        observed["query"] = request.scope["query_string"]
        observed["url"] = str(request.url)
        observed["params"] = request.scope["_qbo_callback_params"]
        raise HTTPException(401, str(request.url))

    client.app.dependency_overrides[admin_user] = reject
    response = client.get(CALLBACK + "?" + query)
    assert response.status_code == 401
    assert observed["query"] == b"" and "?" not in observed["url"]
    assert bool(observed["params"]) == accepted
    assert "synthetic-private" not in response.text
    assert response.headers["referrer-policy"] == "no-referrer"


def test_callback_untrusted_host_error_remains_private(client):
    response = client.get(
        CALLBACK + "?code=synthetic-private-code&state=synthetic-private-state",
        headers={"host": "untrusted.example.test"},
    )
    assert response.status_code == 400
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "synthetic-private" not in response.text


def test_qbo_source_environment_and_queue_permissions(client, admin):
    headers = {"x-csrf-token": admin["csrf_token"]}
    payload = {"kind": "qbo", "label": "Synthetic QuickBooks", "environment": "sandbox"}
    assert client.post("/api/v1/sources", json=payload).status_code == 403
    assert (
        client.post(
            "/api/v1/sources", json={"kind": "qbo", "label": "Missing environment"}, headers=headers
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/sources",
            json={"kind": "csv", "label": "CSV", "environment": "sandbox"},
            headers=headers,
        ).status_code
        == 422
    )
    response = client.post("/api/v1/sources", json=payload, headers=headers)
    assert response.status_code == 201
    source = response.json()
    assert source["environment"] == "sandbox" and source["connection_status"] == "configured"
    assert source["next_sync_at"] is None and "credentials" not in response.text
    source_path = "/api/v1/sources/" + source["id"]
    assert (
        client.patch(
            source_path, json={**payload, "environment": "production"}, headers=headers
        ).status_code
        == 422
    )
    assert (
        client.patch(source_path, json={**payload, "label": "Renamed"}, headers=headers).status_code
        == 200
    )
    assert (
        client.post(source_path + "/sync", json={"mode": "full"}, headers=headers).status_code
        == 409
    )
    assert (
        client.post(source_path + "/sync", json={"mode": "unsafe"}, headers=headers).status_code
        == 422
    )
    with client.app.state.session_factory() as db:
        db.get(User, admin["user"]["id"]).role = "reviewer"
        db.commit()
    public = client.get(source_path)
    assert public.status_code == 200 and public.json()["environment"] == "sandbox"
    assert client.post(source_path + "/sync", json={}, headers=headers).status_code == 403
