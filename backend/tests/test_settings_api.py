import pytest
from sqlalchemy import select

from app.models import AuditLog, Setting


def test_settings_validation_and_atomic_audit(client, admin):
    headers = {"x-csrf-token": admin["csrf_token"]}
    defaults = client.get("/api/v1/settings").json()
    assert defaults["retention_years"] is None
    response = client.patch(
        "/api/v1/settings",
        json={
            "display_name": "  Screening desk  ",
            "timezone": "America/New_York",
            "retention_years": 5,
            "review_threshold": 70,
            "auto_clear_threshold": 70,
        },
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["display_name"] == "Screening desk"
    assert client.get("/api/v1/setup").json()["product_name"] == "Screening desk"
    with client.app.state.session_factory() as db:
        row = db.scalar(select(Setting).where(Setting.key == "retention_years"))
        audit = db.scalar(select(AuditLog).where(AuditLog.target_id == row.id))
        assert audit.actor_user_id == admin["user"]["id"]
        assert audit.after["value"] == 5
    assert (
        client.patch(
            "/api/v1/settings", json={"retention_years": None}, headers=headers
        ).status_code
        == 200
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"timezone": "Mars/Olympus"},
        {"timezone": None},
        {"display_name": " "},
        {"retention_years": 4},
        {"retention_years": True},
        {"retention_years": "5"},
        {"candidate_min": 99},
        {"review_threshold": True},
        {"legal_suffixes": "Inc,LLC"},
        {"enabled_source_lists": [""]},
        {"smtp_password": "never store this"},
        {"unknown_setting": "ignored?"},
        {"score_weights": {"token_set": 10}},
    ],
)
def test_settings_reject_invalid_or_sensitive_keys(client, admin, changes):
    response = client.patch(
        "/api/v1/settings", json=changes, headers={"x-csrf-token": admin["csrf_token"]}
    )
    assert response.status_code == 422
    assert client.get("/api/v1/settings").json()["candidate_min"] == 50
    with client.app.state.session_factory() as db:
        assert db.scalar(select(Setting).where(Setting.key == "smtp_password")) is None
