"""Fixture gate contracts only; never start Docker, public runtimes or live HTTP."""

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text
from test_bc import CONFIG, fixture
from test_bc import connected as connected

from app.connectors.bc import sync_bc_source
from app.connectors.bc_common import load_source_credentials, save_source_credentials
from app.models import Source

GATES = Path(__file__).resolve().parents[2] / "tests" / "integration"
spec = importlib.util.spec_from_file_location("milestone7_gate_helpers", GATES / "bc.py")
assert spec and spec.loader
gate = importlib.util.module_from_spec(spec)
original_path = list(sys.path)
try:
    sys.path.insert(0, str(GATES))
    spec.loader.exec_module(gate)
finally:
    sys.path[:] = original_path


def test_explicit_gate_mode_required_before_database_or_network(monkeypatch):
    monkeypatch.setattr(gate, "BCGate", lambda _: pytest.fail("No gate before explicit flag"))
    with pytest.raises(SystemExit) as error:
        gate.main([])
    assert error.value.code == 2


def test_health_wait_unavailable_then_ready_and_bounded_sanitized_failure():
    now = [0.0]
    responses = iter([httpx.ConnectError("private connection text"), httpx.Response(200)])

    def request(_):
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return result

    with httpx.Client(transport=httpx.MockTransport(request), base_url="http://localhost") as http:
        gate.wait_for_health(
            http,
            seconds=1,
            clock=lambda: now[0],
            pause=lambda delay: now.__setitem__(0, now[0] + delay),
        )
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(503)), base_url="http://localhost"
    ) as http:
        with pytest.raises(gate.GateFailure, match="^fixture_api_health_timeout$"):
            gate.wait_for_health(
                http,
                seconds=1,
                clock=lambda: now[0],
                pause=lambda delay: now.__setitem__(0, now[0] + delay),
            )
    assert now[0] >= 1


def test_private_collection_excludes_public_geography_and_company_identity():
    values = {
        "tenant_id": "public-tenant",
        "company_id": "public-company",
        "environment": "Sandbox_US",
        "access_token": "private-token",
        "pending_cleanup": [{"client_state": "private-state", "client_secret": "private-secret"}],
        "raw": {"state": "MA", "country": "US"},
    }
    assert gate.private_values(values) == {"private-token", "private-state", "private-secret"}


@pytest.mark.parametrize(
    "key", ["access_token", "client_id", "client_state", "credentials_enc", "state"]
)
def test_zip_private_keys_only_allow_exact_redaction_inside_original_audit_snapshots(key):
    gate.verify_redaction(
        {
            "action": "sources.update",
            "target_table": "sources",
            "before": {key: None},
            "after": {key: "[redacted]"},
        }
    )
    for value in (
        {key: "[redacted]"},
        {key: "private-value"},
        {"action": "sources.update", "target_table": "sources", "after": {key: "private-value"}},
    ):
        with pytest.raises(gate.GateFailure, match="zip_private_fields_only_exact_audit_markers"):
            gate.verify_redaction(value)


def test_zip_preserves_structural_geography_without_blanket_state_allowance():
    raw = {
        "displayName": "Dummy",
        "id": gate.ENTITY,
        "country": "US",
        "lastModifiedDateTime": "2026-10-14T12:00:00Z",
        "state": "MA",
    }
    gate.verify_redaction(
        {
            "party_snapshot": {"raw": raw},
            "entry_snapshot": {"addresses": [{"state": "Other recorded region"}]},
        }
    )
    with pytest.raises(gate.GateFailure):
        gate.verify_redaction({"raw": {"state": "MA"}})
    with pytest.raises(gate.GateFailure):
        gate.verify_redaction({"raw": {**raw, "state": "private-oauth-state"}})


def test_fixture_name_change_only_customer_and_type_phase_preserves_updated_name(
    client, tmp_path, monkeypatch
):
    monkeypatch.setattr(gate, "REMOTE_STATE", tmp_path / "synthetic-provider.enc")
    remote = gate.RemoteFixture(client.app.state.config)
    remote.app = client.app

    def entity(resource):
        request = httpx.Request(
            "GET",
            gate.BASE + f"/companies({gate.COMPANY})/{resource}",
            headers={
                "Authorization": "Bearer synthetic-bc-runtime-bearer",
                "Prefer": "odata.maxpagesize=300",
            },
        )
        return remote.request(request).json()["value"][0]

    before = entity("vendors")
    remote.phase = "changed"
    assert entity("customers")["displayName"] == "Example Manufacturing 42 Updated"
    assert entity("vendors")["displayName"] == before["displayName"]
    remote.phase = "type_changed"
    customer = entity("customers")
    assert (
        customer["displayName"] == "Example Manufacturing 42 Updated"
        and customer["type"] == "Person"
    )
    assert entity("vendors")["displayName"] == before["displayName"]


def test_fixture_renewal_forces_actual_remote_expiry_once_without_database_mutation(
    client, tmp_path, monkeypatch
):
    monkeypatch.setattr(gate, "REMOTE_STATE", tmp_path / "synthetic-provider.enc")
    remote = gate.RemoteFixture(client.app.state.config)
    remote.phase = "renew_conflict"
    remote.subscriptions = {
        "synthetic-id": {
            "subscriptionId": "synthetic-id",
            "expirationDateTime": (datetime.now(UTC) + timedelta(days=3)).isoformat(),
            "@odata.etag": 'W/"actual"',
        }
    }
    request = httpx.Request("GET", gate.BASE + "/subscriptions")
    first = remote.subscription(request).json()["value"][0]
    assert (
        timedelta(minutes=59)
        < datetime.fromisoformat(first["expirationDateTime"]) - datetime.now(UTC)
        <= timedelta(hours=1)
    )
    remote.subscriptions["synthetic-id"]["expirationDateTime"] = (
        datetime.now(UTC) + timedelta(days=3)
    ).isoformat()
    second = remote.subscription(request).json()["value"][0]
    assert datetime.fromisoformat(second["expirationDateTime"]) - datetime.now(UTC) > timedelta(
        days=2
    )


def test_readonly_database_rejects_other_installation_before_connect(monkeypatch):
    monkeypatch.setattr(
        gate.sqlite3, "connect", lambda *args, **kwargs: pytest.fail("Not a fixture DB")
    )
    with pytest.raises(gate.GateFailure, match="readonly_fixture_database_only"):
        with gate.readonly_database(SimpleNamespace(database_url="sqlite:///customer.db")):
            pass


def test_gate_zip_checks_actual_api_private_obligations_and_geography(client, admin, connected):
    factory, config = client.app.state.session_factory, client.app.state.config
    private_state = "synthetic-private-gate-check-state"
    with factory() as db:
        source = db.get(Source, connected)
        values = load_source_credentials(source, config)
        values["pending_cleanup"] = [
            {
                "tenant_id": CONFIG.tenant_id,
                "environment": CONFIG.environment,
                "client_id": CONFIG.client_id,
                "client_secret": CONFIG.client_secret,
                "subscription_id": None,
                "client_state": private_state,
                "resource_path": f"api/v2.0/companies({CONFIG.company_id})/customers",
                "notification_url": "https://fixture.example.test/webhook",
            }
        ]
        save_source_credentials(db, source, config, values)
        db.commit()

        def transport(request):
            row = fixture("customer" if request.url.path.endswith("/customers") else "vendor")
            row["state"] = "MA" if request.url.path.endswith("/customers") else "WA"
            return httpx.Response(200, json={"value": [row]})

        sync_bc_source(db, config, source, mode="full", transport=httpx.MockTransport(transport))
        party_id = db.execute(text("SELECT id FROM parties WHERE role='customer'")).scalar_one()
    verifier = gate.BCGate("http://localhost:8000")
    verifier.http.close()
    verifier.http = client
    verifier.source_id, verifier.customer_id, verifier.admin_id = (
        connected,
        party_id,
        admin["user"]["id"],
    )
    verifier.csrf = admin["csrf_token"]
    verifier.sensitive = {
        CONFIG.client_id,
        CONFIG.client_secret,
        values["access_token"],
        private_state,
    }
    verifier.export_privacy()
    assert verifier.evidence["checks"] == ["actual_full_zip_private_cleanup_and_member_checksums"]


def custody_fixture(pending_customer=False):
    source_id, admin_id = "synthetic-source-id", "synthetic-admin-id"
    private = {
        "tenant_id": gate.TENANT,
        "client_id": gate.APP_ID,
        "source_generation": 1,
        "app_generation": 1,
        "subscriptions": [],
        "pending_cleanup": [],
    }
    remote = {}
    for entity in ("customers", "vendors"):
        id, state = "synthetic-id-" + entity, "synthetic-secret-state-" + entity
        path = f"api/v2.0/companies({gate.COMPANY})/{entity}"
        url = f"https://screening.example.test/api/v1/webhooks/bc/{source_id}"
        remote[id] = {
            "subscriptionId": id,
            "clientState": state,
            "resource": path,
            "notificationUrl": url,
        }
        row = {
            "subscription_id": id,
            "client_state": state,
            "resource": entity,
            "resource_path": path,
            "notification_url": url,
        }
        if entity == "customers" and pending_customer:
            private["pending_cleanup"].append(
                {
                    **row,
                    "subscription_id": None,
                    "purpose": "activate",
                    "issuer_user_id": admin_id,
                    "tenant_id": gate.TENANT,
                    "environment": gate.ENVIRONMENT,
                    "company_id": gate.COMPANY,
                    "client_id": gate.APP_ID,
                    "client_secret": gate.APP_SECRET,
                    "source_generation": 1,
                    "app_generation": 1,
                }
            )
        else:
            private["subscriptions"].append(row)
    return private, remote, source_id, admin_id


@pytest.mark.parametrize("pending_customer", [True, False])
def test_uncertain_create_union_accepts_exact_pending_or_peer_adopted_customer(pending_customer):
    private, remote, source_id, admin_id = custody_fixture(pending_customer)
    gate.verify_subscription_custody(private, remote, source_id=source_id, admin_id=admin_id)


@pytest.mark.parametrize("pending_customer", [True, False])
@pytest.mark.parametrize("private_slash", [True, False])
@pytest.mark.parametrize("remote_slash", [True, False])
def test_custody_uses_actual_lifecycle_provider_resource_with_only_optional_single_slash(
    pending_customer, private_slash, remote_slash
):
    from app.connectors.bc_subscriptions import resource_path

    private, remote, source_id, admin_id = custody_fixture(pending_customer)
    for row in remote.values():
        entity = row["resource"].rsplit("/", 1)[-1]
        actual = resource_path(gate.COMPANY, entity)
        assert actual.startswith("/api/v2.0/")
        row["resource"] = actual if remote_slash else actual[1:]
    for row in [*private["subscriptions"], *private["pending_cleanup"]]:
        actual = resource_path(gate.COMPANY, row["resource"])
        row["resource_path"] = actual if private_slash else actual[1:]
    gate.verify_subscription_custody(private, remote, source_id=source_id, admin_id=admin_id)


@pytest.mark.parametrize(
    "path",
    [
        f"//api/v2.0/companies({gate.COMPANY})/customers",
        f"https://api.businesscentral.dynamics.com/api/v2.0/companies({gate.COMPANY})/customers",
        f"/api/v2.0/companies({gate.OTHER_COMPANY})/customers",
        f"/api/v2.0/companies({gate.COMPANY})/vendors",
        f"/api/v2.0/companies({gate.COMPANY})/customers/",
        f"/api/v2.0/companies({gate.COMPANY})/customers?x=1",
    ],
)
def test_custody_resource_canonicalization_does_not_normalize_other_paths(path):
    private, remote, source_id, admin_id = custody_fixture()
    remote["synthetic-id-customers"]["resource"] = path
    with pytest.raises(gate.GateFailure, match="uncertain_custody_one_remote_grant_per_resource"):
        gate.verify_subscription_custody(private, remote, source_id=source_id, admin_id=admin_id)


@pytest.mark.parametrize(
    "key,value",
    [
        ("client_state", "different-private-state"),
        ("subscription_id", "different-id"),
        ("resource_path", "api/v2.0/companies(other)/customers"),
        ("notification_url", "https://other.example.test/webhook"),
    ],
)
def test_uncertain_create_adoption_never_accepts_wrong_identity_state_url_or_resource(key, value):
    private, remote, source_id, admin_id = custody_fixture()
    private["subscriptions"][0][key] = value
    with pytest.raises(
        gate.GateFailure, match="uncertain_accepted_grant_exact_pending_or_adopted_custody"
    ):
        gate.verify_subscription_custody(private, remote, source_id=source_id, admin_id=admin_id)


@pytest.mark.parametrize(
    "key,value",
    [
        ("issuer_user_id", "other-admin"),
        ("purpose", "cleanup"),
        ("client_secret", "different-secret"),
        ("source_generation", 2),
        ("app_generation", 2),
    ],
)
def test_uncertain_pending_custody_keeps_original_actor_app_and_generation(key, value):
    private, remote, source_id, admin_id = custody_fixture(True)
    private["pending_cleanup"][0][key] = value
    with pytest.raises(
        gate.GateFailure, match="uncertain_accepted_grant_exact_pending_or_adopted_custody"
    ):
        gate.verify_subscription_custody(private, remote, source_id=source_id, admin_id=admin_id)
