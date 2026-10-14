"""Model settings security and saved-provider race checks; synthetic transport only."""

import json
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from app.adjudicators.contracts import (
    PRIVACY_NOTICE,
    Evaluation,
    ModelConfig,
    ModelConfigurationError,
)
from app.adjudicators.settings import load_model_config, save_model_config, validate_laya_base_url
from app.models import AuditLog, ScreeningTask, Setting, User, UserSession
from app.reports.export import export_full_zip


def patch(client, admin, **values):
    return client.patch(
        "/api/v1/settings/model",
        json={"expected_generation": 0, **values},
        headers={"x-csrf-token": admin["csrf_token"]},
    )


def connection_test(client, admin, generation):
    return client.post(
        "/api/v1/settings/model/test",
        json={"expected_generation": generation},
        headers={"x-csrf-token": admin["csrf_token"]},
    )


def test_defaults_no_secret_and_authenticated_roles(client, admin):
    response = client.get("/api/v1/settings/model")
    assert response.status_code == 200
    values = response.json()
    assert values["provider"] == "none" and values["w_det"] == 1 and values["w_model"] == 0
    assert values["privacy_notice"] == PRIVACY_NOTICE
    assert "jev_api_key" not in values and not values["jev_api_key_configured"]
    assert patch(client, admin, provider="laya").status_code == 200
    with client.app.state.session_factory() as db:
        db.get(User, admin["user"]["id"]).role = "reviewer"
        db.commit()
    assert client.get("/api/v1/settings/model").status_code == 403
    assert patch(client, admin).status_code == 403
    assert connection_test(client, admin, 1).status_code == 403
    client.cookies.clear()
    assert client.get("/api/v1/settings/model").status_code == 401


def test_hosted_ack_encrypted_keys_and_full_task_atomic(client, admin):
    from app.csl.service import refresh_csl

    with client.app.state.session_factory() as db:
        refresh_csl(
            db,
            data=json.dumps(
                [
                    {
                        "id": "model-policy-test",
                        "source": "Entity List (EL) - Bureau of Industry and Security",
                        "name": "Synthetic Policy Test",
                        "type": "Entity",
                        "alt_names": [],
                        "addresses": [],
                        "programs": [],
                    }
                ]
            ).encode(),
        )
        before_tasks = len(list(db.scalars(select(ScreeningTask))))
    key = " synthetic write-only Jev key "
    assert patch(client, admin, provider="jev", jev_api_key=key).status_code == 422
    assert (
        patch(
            client, admin, provider="jev", jev_api_key=key, hosted_acknowledgment="yes"
        ).status_code
        == 422
    )
    result = patch(
        client, admin, provider="jev", jev_api_key=key, hosted_acknowledgment=PRIVACY_NOTICE
    )
    assert result.status_code == 200
    payload = result.json()
    assert payload["generation"] == 1 and payload["hosted_acknowledgment_valid"]
    assert payload["pending_screening_task_id"]
    assert key not in result.text
    with client.app.state.session_factory() as db:
        row = db.scalar(select(Setting).where(Setting.key == "model_config"))
        assert row.encrypted and key not in str(row.value)
        config = load_model_config(db, client.app.state.config)
        assert config.jev_api_key == key and key not in repr(config)
        assert config.scoring_snapshot()["generation"] == 1
        assert key not in json.dumps(config.scoring_snapshot())
        assert len(list(db.scalars(select(ScreeningTask)))) == before_tasks + 1
        events = list(db.scalars(select(AuditLog)))
        assert key not in str([(e.before, e.after) for e in events])
        proof = next(e for e in events if e.action == "settings.model_hosted_acknowledged")
        assert proof.actor_user_id == admin["user"]["id"]
        assert proof.after["actor"]["role"] == "admin"
        artifact = export_full_zip(db)
        try:
            import zipfile

            with zipfile.ZipFile(artifact.path) as archive:
                assert all(key.encode() not in archive.read(name) for name in archive.namelist())
        finally:
            artifact.close()


def test_preserve_clear_stale_generation_noop_and_csrf(client, admin):
    assert (
        client.patch("/api/v1/settings/model", json={"expected_generation": 0}).status_code == 403
    )
    assert patch(client, admin, provider="jev").status_code == 422
    response = patch(client, admin, provider="laya", laya_api_key=" synthetic local ")
    assert response.status_code == 200 and response.json()["generation"] == 1
    assert patch(client, admin, provider="none").status_code == 409
    assert patch(client, admin, expected_generation=1, laya_api_key=None).json()["generation"] == 1
    assert (
        patch(
            client, admin, expected_generation=1, laya_api_key="new", clear_laya_api_key=True
        ).status_code
        == 422
    )
    cleared = patch(client, admin, expected_generation=1, clear_laya_api_key=True)
    assert cleared.status_code == 200 and cleared.json()["generation"] == 2
    assert not cleared.json()["laya_api_key_configured"]


@pytest.mark.parametrize(
    "values",
    [
        {"w_det": True},
        {"w_det": -1, "w_model": 2},
        {"w_det": 0.5},
        {"laya_max_tokens": 8192},
        {"jev_model": "latest"},
        {"password": "private"},
        {"laya_api_key": "contains\ncontrol"},
        {"expected_generation": True},
    ],
)
def test_invalid_inputs_never_write(client, admin, values):
    assert patch(client, admin, **values).status_code == 422
    with client.app.state.session_factory() as db:
        assert db.scalar(select(Setting).where(Setting.key == "model_config")) is None


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "http://192.168.1.1:8009",
        "http://localhost.evil",
        "http://127.0.0.1.evil",
        "http://127.1",
        "http://2130706433",
        "http://localhost@evil",
        "http://user@localhost",
        "http://localhost/v1",
        "http://localhost?",
        "http://localhost#",
        "http://localhost:0",
        "http://localhost:65536",
        "http://localhost:",
        "http://[::1%25lo0]",
        "ftp://localhost",
    ],
)
def test_local_endpoint_rejects_unsafe_urls(url):
    with pytest.raises(ModelConfigurationError):
        validate_laya_base_url(url)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://localhost:8009/", "http://localhost:8009"),
        ("http://127.0.0.1:8009", "http://127.0.0.1:8009"),
        ("https://[::1]:8009/", "https://[::1]:8009"),
        ("http://host.docker.internal:8009", "http://host.docker.internal:8009"),
    ],
)
def test_local_endpoint_canonicalizes_only_same_host(url, expected):
    assert validate_laya_base_url(url) == expected


def test_loader_revalidates_saved_local_url(client, admin):
    with client.app.state.session_factory() as db:
        save_model_config(
            db,
            client.app.state.config,
            replace(ModelConfig(), provider="laya", laya_base_url="https://public.example"),
        )
        db.commit()
    assert client.get("/api/v1/settings/model").status_code == 503


@pytest.mark.parametrize("change", ["role", "session", "generation"])
def test_connection_tests_network_writer_free_late_authority_config(
    client, admin, monkeypatch, change
):
    assert patch(client, admin, provider="laya").status_code == 200

    def evaluate(model, party, entry, explanation, *, transport=None):
        assert party["name"] == entry["name"] == "Synthetic Test Company"
        assert explanation == {"synthetic_connection_test": True}
        with client.app.state.session_factory() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            if change == "role":
                db.get(User, admin["user"]["id"]).role = "reviewer"
            elif change == "session":
                for row in db.scalars(select(UserSession)):
                    db.delete(row)
            else:
                config = load_model_config(db, client.app.state.config)
                save_model_config(db, client.app.state.config, replace(config, generation=2))
            db.commit()
        return Evaluation("laya", "multilingual", model_prob=0.8, returned_model="multilingual")

    monkeypatch.setitem(sys.modules, "app.adjudicators.runner", SimpleNamespace(evaluate=evaluate))
    response = connection_test(client, admin, 1)
    assert response.status_code == (409 if change == "generation" else 403)
    with client.app.state.session_factory() as db:
        assert not list(
            db.scalars(select(AuditLog).where(AuditLog.action == "settings.model_tested"))
        )


@pytest.mark.parametrize("failed", [False, True])
def test_connection_tests_saved_provider_safe_result_no_task(client, admin, monkeypatch, failed):
    assert patch(client, admin, provider="laya").status_code == 200

    def evaluate(model, party, entry, explanation, *, transport=None):
        with client.app.state.session_factory() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            db.rollback()
        return Evaluation(
            "laya",
            "multilingual",
            model_prob=None if failed else 0.9,
            returned_model=None if failed else "multilingual",
            attempts=2,
            error_code="provider_unavailable" if failed else None,
        )

    monkeypatch.setitem(sys.modules, "app.adjudicators.runner", SimpleNamespace(evaluate=evaluate))
    response = connection_test(client, admin, 1)
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == ("failed" if failed else "connected")
    assert result["model_prob"] == (None if failed else 0.9)
    with client.app.state.session_factory() as db:
        assert not list(db.scalars(select(ScreeningTask)))
        assert (
            len(
                list(db.scalars(select(AuditLog).where(AuditLog.action == "settings.model_tested")))
            )
            == 1
        )


def test_saved_provider_real_transport_preserves_key_and_rejects_redirect(client, admin):
    import httpx

    key = " synthetic key preserved "
    assert (
        patch(
            client, admin, provider="jev", jev_api_key=key, hosted_acknowledgment=PRIVACY_NOTICE
        ).status_code
        == 200
    )
    calls = []

    def request(raw):
        with client.app.state.session_factory() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            db.rollback()
        calls.append(str(raw.url))
        assert raw.headers["authorization"] == "Bearer " + key
        assert str(raw.url) == "https://api.typesafe.ai/v1/systemone"
        payload = json.loads(raw.content)
        assert payload["model"] == "jev-1.13.0"
        assert b"Synthetic Test Company" in raw.content
        assert payload["questions"]["same_party"]["type"] == "noul"
        return httpx.Response(302, headers={"Location": "https://external.example/private"})

    client.app.state.model_transport = httpx.MockTransport(request)
    result = connection_test(client, admin, 1)
    assert result.status_code == 200
    assert result.json()["status"] == "failed"
    assert result.json()["error_code"] == "redirect_rejected"
    assert len(calls) == 1 and key not in result.text
    assert "external.example" not in result.text


def test_model_configuration_and_full_task_roll_back_together(client, admin, monkeypatch):
    import app.api.model_settings as endpoint
    from app.csl.service import refresh_csl

    with client.app.state.session_factory() as db:
        refresh_csl(
            db,
            data=json.dumps(
                [
                    {
                        "id": "model-atomic",
                        "source": "Entity List (EL) - Bureau of Industry and Security",
                        "name": "Synthetic Atomic Test",
                        "type": "Entity",
                        "alt_names": [],
                        "addresses": [],
                        "programs": [],
                    }
                ]
            ).encode(),
        )
        prior_count = len(list(db.scalars(select(ScreeningTask))))
    original = endpoint.audit

    def fail_after_task(db, action, *args, **kwargs):
        if action == "settings.model_changed":
            raise RuntimeError("synthetic store failure")
        return original(db, action, *args, **kwargs)

    monkeypatch.setattr(endpoint, "audit", fail_after_task)
    with pytest.raises(RuntimeError, match="synthetic store failure"):
        patch(client, admin, provider="laya")
    with client.app.state.session_factory() as db:
        assert load_model_config(db, client.app.state.config).generation == 0
        assert len(list(db.scalars(select(ScreeningTask)))) == prior_count


def test_saved_laya_test_real_transport_usage_and_optional_private_key(client, admin):
    import httpx

    from app.adjudicators.budget import local_tokens

    key = " synthetic local bearer "
    result = patch(client, admin, provider="laya", laya_api_key=key)
    assert result.status_code == 200

    def request(raw):
        assert str(raw.url) == "http://127.0.0.1:8009/v1/systemone"
        assert raw.headers["authorization"] == "Bearer " + key
        payload = json.loads(raw.content)
        assert payload["model"] == "multilingual" and payload["max_len"] == 1024
        return httpx.Response(
            200,
            json={
                "model": "laya-rl-agent",
                "answers": {"same_party": {"type": "noul", "noul": 0.8}},
                "usage": {"input_tokens": local_tokens(payload["state"]) + 35, "output_tokens": 0},
            },
        )

    client.app.state.model_transport = httpx.MockTransport(request)
    response = connection_test(client, admin, 1)
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "connected" and payload["model_prob"] == 0.8
    assert payload["returned_model"] == "laya-rl-agent"
    assert payload["usage"]["input_tokens"] <= 1024 and payload["usage"]["output_tokens"] == 0
    assert key not in response.text


def test_cleared_jev_key_flag_truthful_while_historical_consent_is_immutable(client, admin):
    from copy import deepcopy

    from app.adjudicators.settings import model_acknowledgment_valid

    assert (
        patch(
            client,
            admin,
            provider="jev",
            jev_api_key="synthetic-clear-proof",
            hosted_acknowledgment=PRIVACY_NOTICE,
        ).status_code
        == 200
    )
    with client.app.state.session_factory() as db:
        previous = load_model_config(db, client.app.state.config)
        reference = previous.hosted_acknowledgment_audit_id
        proof_before = deepcopy(db.get(AuditLog, reference).after)
    response = patch(client, admin, expected_generation=1, provider="none", clear_jev_api_key=True)
    assert response.status_code == 200
    assert not response.json()["jev_api_key_configured"]
    assert not response.json()["hosted_acknowledgment_valid"]
    current = client.get("/api/v1/settings/model")
    assert current.status_code == 200
    assert not current.json()["hosted_acknowledgment_valid"]
    events = client.get("/api/v1/audit-log?action=settings.model_changed&limit=100").json()["items"]
    change = next(event for event in events if event["after"]["generation"] == 2)
    assert change["before"]["hosted_acknowledgment_valid"] is True
    assert change["after"]["hosted_acknowledgment_valid"] is False
    with client.app.state.session_factory() as db:
        saved = load_model_config(db, client.app.state.config)
        assert saved.hosted_acknowledgment_audit_id == reference
        assert db.get(AuditLog, reference).after == proof_before
        assert model_acknowledgment_valid(db, saved)


@pytest.mark.parametrize("field", ["laya_api_key", "jev_api_key"])
@pytest.mark.parametrize("non_ascii", ["é", "\u00a0", "😀"])
def test_non_ascii_key_rejected_before_config_or_task_write(client, admin, field, non_ascii):
    values = {field: "synthetic" + non_ascii + "key"}
    if field == "jev_api_key":
        values["hosted_acknowledgment"] = PRIVACY_NOTICE
    response = patch(client, admin, **values)
    assert response.status_code == 422
    assert response.json()["detail"] == "Key must use printable ASCII characters"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(Setting).where(Setting.key == "model_config")) is None
        assert not list(db.scalars(select(ScreeningTask)))
        assert not list(
            db.scalars(
                select(AuditLog).where(AuditLog.action == "settings.model_hosted_acknowledged")
            )
        )


def test_loader_fails_closed_on_non_ascii_saved_key(client, admin):
    with client.app.state.session_factory() as db:
        save_model_config(
            db,
            client.app.state.config,
            replace(ModelConfig(), provider="laya", laya_api_key="synthetic-é-key"),
        )
        db.commit()
        with pytest.raises(ModelConfigurationError, match="Model configuration unavailable"):
            load_model_config(db, client.app.state.config)
    assert client.get("/api/v1/settings/model").status_code == 503
