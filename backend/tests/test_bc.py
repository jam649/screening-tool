"""Synthetic Microsoft contracts and real fenced SQLite apply; no live claims."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import select, text

from app.connectors import bc
from app.connectors.bc import (
    BCClient,
    BCConnector,
    BCError,
    normalize_entity,
    sync_bc_source,
    token_credentials,
)
from app.connectors.bc_common import (
    BCAppConfig,
    BCConfig,
    load_source_credentials,
    save_app_config,
    save_source_credentials,
)
from app.csl.service import refresh_csl
from app.models import (
    AuditLog,
    Disposition,
    Hit,
    OperationAttempt,
    Party,
    ScreeningTask,
    Source,
    User,
)
from app.scheduler.leases import operation_session, protected_operation
from app.screening.engine import process_pending_tasks, queue_hits, run_screening

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
TENANT = "11111111-1111-4111-8111-111111111111"
COMPANY = "22222222-2222-4222-8222-222222222222"
CLIENT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
APP = BCAppConfig(CLIENT_ID, "synthetic-bc-client-secret", generation=1)
CONFIG = BCConfig(TENANT, "Synthetic_Sandbox-1", COMPANY, APP.client_id, APP.client_secret, 1, 1)


@pytest.fixture(autouse=True)
def fixture_clock(monkeypatch):
    monkeypatch.setattr(bc, "utcnow", lambda: NOW)


def fixture(name):
    return json.loads((FIXTURES / "bc" / f"{name}.json").read_text())


def collection(rows, **metadata):
    return {"value": rows, **metadata}


def client_for(handler, pauses=None, config=CONFIG):
    return BCClient(
        config,
        transport=httpx.MockTransport(handler),
        pause=(pauses.append if pauses is not None else lambda _: None),
    )


@pytest.fixture
def connected(client, admin, monkeypatch):
    monkeypatch.setattr(bc, "utcnow", lambda: NOW)
    with client.app.state.session_factory() as db:
        save_app_config(db, client.app.state.config, APP)
        source = Source(
            kind="bc",
            label="Synthetic BC company",
            config={
                "tenant_id": TENANT,
                "bc_environment": CONFIG.environment,
                "company_id": COMPANY,
                "connection_status": "connected",
                "connection_generation": 1,
                "last_full_sync_at": NOW.isoformat(),
            },
        )
        db.add(source)
        db.flush()
        credentials = token_credentials(
            fixture("token"),
            tenant_id=TENANT,
            client_id=CLIENT_ID,
            app_generation=1,
            source_generation=1,
            now=NOW,
        )
        save_source_credentials(db, source, client.app.state.config, credentials)
        source.cursor = NOW.isoformat()
        db.commit()
        return source.id


def test_token_form_fixed_origin_opaque_actual_expiry_and_secret_repr():
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json=fixture("token"))

    http = client_for(transport)
    response = http.acquire_token()
    token = token_credentials(
        response,
        tenant_id=TENANT,
        client_id=CLIENT_ID,
        app_generation=1,
        source_generation=1,
        now=NOW,
    )
    assert str(calls[0].url) == f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
    assert parse_qs(calls[0].content.decode()) == {
        "grant_type": ["client_credentials"],
        "client_id": [CLIENT_ID],
        "client_secret": [APP.client_secret],
        "scope": ["https://api.businesscentral.dynamics.com/.default"],
    }
    assert calls[0].headers["content-type"].startswith("application/x-www-form-urlencoded")
    assert token["access_token"] == response["access_token"]
    assert token["access_expires_at"] == (NOW + timedelta(seconds=3599)).isoformat()
    assert "refresh_token" not in token
    assert APP.client_secret not in repr(CONFIG) and CLIENT_ID not in repr(CONFIG)
    http.close()


@pytest.mark.parametrize(
    "change",
    [
        {"token_type": "Basic"},
        {"access_token": ""},
        {"access_token": "line\nbreak"},
        {"expires_in": True},
        {"expires_in": 0},
        {"expires_in": "3599"},
    ],
)
def test_strict_token_response_requires_actual_valid_bearer_metadata(change):
    with pytest.raises(BCError, match="invalid_token_response"):
        token_credentials(
            {**fixture("token"), **change},
            tenant_id=TENANT,
            client_id=CLIENT_ID,
            app_generation=1,
            source_generation=1,
            now=NOW,
        )


def test_bounded_get_retries_but_uncertain_token_post_never_replays():
    calls, pauses = [], []

    def transport(request):
        calls.append(request)
        if request.method == "POST":
            raise httpx.ReadTimeout("private transport message must not escape")
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "10000"})
        return httpx.Response(200, json=fixture("companies"))

    http = client_for(transport, pauses)
    assert http.company_page("synthetic-access")["value"]
    assert pauses == [5, 5] and len(calls) == 3
    with pytest.raises(BCError) as error:
        http.acquire_token()
    assert error.value.code == "transport_failed" and len(calls) == 4
    assert "private transport" not in str(error.value)
    http.close()


@pytest.mark.parametrize(
    "response,code",
    [
        (httpx.Response(401, json={"error_description": "private"}), "unauthorized"),
        (httpx.Response(403), "forbidden"),
        (httpx.Response(302, headers={"Location": "https://evil.example.test"}), "vendor_rejected"),
        (httpx.Response(200, json=[]), "invalid_response"),
        (httpx.Response(200, json={"error": {"message": "private"}}), "invalid_response"),
    ],
)
def test_safe_errors_and_redirects_never_disclose_vendor_text(response, code):
    calls = []

    def transport(request):
        calls.append(request)
        return response

    http = client_for(transport)
    with pytest.raises(BCError) as error:
        http.company_page("synthetic-access")
    assert error.value.code == code and "private" not in str(error.value)
    assert len(calls) == 1
    http.close()


@pytest.mark.parametrize(
    "replacement",
    [
        {"tenant_id": "tenant.example.test"},
        {"environment": "../Production"},
        {"environment": "1invalid"},
        {"environment": "a" * 30},
        {"company_id": "not-a-guid"},
        {"client_id": "common"},
    ],
)
def test_untrusted_config_never_reaches_http(replacement):
    with pytest.raises(BCError):
        client_for(
            lambda _: pytest.fail("No request may escape"), config=replace(CONFIG, **replacement)
        )


def test_discovery_get_and_bound_company_isolation():
    calls = []

    def transport(request):
        calls.append(request)
        if request.url.path.endswith("/companies"):
            return httpx.Response(200, json=fixture("companies"))
        return httpx.Response(200, json=fixture("companies")["value"][0])

    http = client_for(transport, config=replace(CONFIG, company_id=None))
    assert http.company_page("synthetic-access")["value"][0]["id"] == COMPANY
    assert http.get_company("synthetic-access", COMPANY)["id"] == COMPANY
    assert calls[0].url.path == f"/v2.0/{TENANT}/{CONFIG.environment}/api/v2.0/companies"
    assert calls[0].headers["prefer"] == "odata.maxpagesize=300"
    assert "$top" not in calls[0].url.params
    http.close()
    bound = client_for(transport)
    with pytest.raises(BCError, match="company_changed"):
        bound.get_company("synthetic-access", "44444444-4444-4444-8444-444444444444")
    assert len(calls) == 2
    bound.close()


@pytest.mark.parametrize(
    "next_link",
    [
        "https://evil.example.test/customers?$skiptoken=1",
        "http://api.businesscentral.dynamics.com/anything",
        "https://user@api.businesscentral.dynamics.com/anything",
        "https://api.businesscentral.dynamics.com:443/anything",
        "?x=1#fragment",
        "?x=1\n",
        "\\evil.example.test/customers",
        f"/v2.0/{TENANT}/Other/api/v2.0/companies({COMPANY})/customers?$skiptoken=1",
        f"/v2.0/{TENANT}/{CONFIG.environment}/api/v2.0/companies({COMPANY})/vendors?$skiptoken=1",
        f"/v2.0/{TENANT}/{CONFIG.environment}/api/v2.0/companies(44444444-4444-4444-8444-444444444444)/customers?$skiptoken=1",
        f"/v2.0/{TENANT}/{CONFIG.environment}/api/v2.0/companies({COMPANY})%2Fcustomers?$skiptoken=1",
    ],
)
def test_continuation_requires_exact_configured_path_before_another_bearer(next_link):
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json=collection([], **{"@odata.nextLink": next_link}))

    http = client_for(transport)
    with pytest.raises(BCError, match="untrusted_continuation"):
        BCConnector(http, "synthetic-access", poll_started_at=NOW).full_sync()
    assert len(calls) == 1
    http.close()


def test_full_follows_opaque_query_pages_counts_and_namespaced_guid_collision(monkeypatch):
    monkeypatch.setattr(bc, "utcnow", lambda: NOW)
    calls = []
    second = {**fixture("customer"), "id": "44444444-4444-4444-8444-444444444444"}

    def transport(request):
        calls.append(request)
        assert request.headers["prefer"] == "odata.maxpagesize=300"
        assert "$top" not in request.url.params
        if request.url.path.endswith("/vendors"):
            return httpx.Response(200, json=collection([fixture("vendor")], **{"@odata.count": 1}))
        if request.url.query:
            assert request.url.query == b"%24skiptoken=a%2Bb%2Fc"
            return httpx.Response(200, json=collection([second], **{"@odata.count": 2}))
        return httpx.Response(
            200,
            json=collection(
                [fixture("customer")],
                **{"@odata.count": 2, "@odata.nextLink": "?%24skiptoken=a%2Bb%2Fc"},
            ),
        )

    http = client_for(transport)
    batch = BCConnector(http, "synthetic-access", poll_started_at=NOW).full_sync()
    assert batch.complete and batch.cursor == NOW.isoformat() and len(calls) == 3
    assert {party.external_id for party in batch.parties} == {
        "Customer:33333333-3333-4333-8333-333333333333",
        "Customer:44444444-4444-4444-8444-444444444444",
        "Vendor:33333333-3333-4333-8333-333333333333",
    }
    http.close()


def test_normalization_type_posting_blocks_and_complete_raw_fidelity():
    customer, vendor = fixture("customer"), fixture("vendor")
    person = {**customer, "type": "Person", "displayName": "Synthetic Example Person"}
    normalized = normalize_entity("customers", person)
    assert normalized.party_type == "individual" and normalized.deleted is False
    assert normalized.address_lines == ["123 Synthetic Avenue", "Suite 42"]
    assert normalized.country_iso2 == "US" and normalized.city == "Portland"
    assert normalized.raw == person and normalized.raw is not person
    assert normalized.raw["syntheticMetadata"] is not person["syntheticMetadata"]
    assert normalize_entity("vendors", vendor).party_type == "company"
    assert normalize_entity("vendors", vendor).deleted is False
    assert normalize_entity("customers", customer).record_hash != normalized.record_hash
    assert (
        normalize_entity("customers", customer).record_hash
        == normalize_entity(
            "customers", {**customer, "phoneNumber": "new dummy phone", "blocked": " "}
        ).record_hash
    )


@pytest.mark.parametrize(
    "replacement",
    [
        {"id": "invalid"},
        {"type": "Unknown"},
        {"type": None},
        {"displayName": ""},
        {"addressLine1": []},
        {"country": "Not a verified country"},
        {"lastModifiedDateTime": "2026-10-14T12:00:00"},
    ],
)
def test_malformed_party_rejects_whole_batch(replacement):
    with pytest.raises(BCError):
        normalize_entity("customers", {**fixture("customer"), **replacement})


@pytest.mark.parametrize(
    "page,code",
    [
        ({}, "invalid_collection_response"),
        ({"value": None}, "invalid_collection_response"),
        (collection([], **{"@odata.count": True}), "invalid_collection_response"),
        (collection([], **{"@odata.count": -1}), "invalid_collection_response"),
        (collection([], **{"@odata.count": 2}), "incomplete_collection_response"),
        (collection([fixture("customer"), fixture("customer")]), "duplicate_entity"),
        (collection([], **{"@odata.nextLink": None}), "invalid_continuation"),
        (collection([], **{"@odata.nextLink": ""}), "invalid_continuation"),
    ],
)
def test_invalid_counts_duplicates_and_shapes_never_form_complete_batch(page, code):
    http = client_for(lambda _: httpx.Response(200, json=page))
    with pytest.raises(BCError, match=code):
        BCConnector(http, "synthetic-access", poll_started_at=NOW).full_sync()
    http.close()


def test_continuation_cycle_and_page_record_byte_caps_abort(monkeypatch):
    monkeypatch.setattr(bc, "MAX_PAGES", 2)
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json=collection([], **{"@odata.nextLink": str(request.url)}))

    http = client_for(transport)
    with pytest.raises(BCError, match="invalid_continuation"):
        BCConnector(http, "synthetic-access", poll_started_at=NOW).full_sync()
    assert len(calls) == 1
    http.close()

    counter = iter(range(10))
    http = client_for(
        lambda _: httpx.Response(
            200, json=collection([], **{"@odata.nextLink": f"?page={next(counter)}"})
        )
    )
    with pytest.raises(BCError, match="page_limit"):
        BCConnector(http, "synthetic-access", poll_started_at=NOW).full_sync()
    http.close()

    monkeypatch.setattr(bc, "MAX_RECORDS", 0)
    http = client_for(lambda _: httpx.Response(200, json=collection([fixture("customer")])))
    with pytest.raises(BCError, match="record_limit"):
        BCConnector(http, "synthetic-access", poll_started_at=NOW).full_sync()
    http.close()
    monkeypatch.setattr(bc, "MAX_RESPONSE_BYTES", 20)
    http = client_for(lambda _: httpx.Response(200, content=b"x" * 21))
    with pytest.raises(BCError, match="response_too_large"):
        http.company_page("synthetic-access")
    http.close()


def test_incremental_overlap_ties_cursor_start_and_concurrent_modification(monkeypatch):
    monkeypatch.setattr(bc, "utcnow", lambda: NOW + timedelta(seconds=10))
    calls = []

    def transport(request):
        calls.append(request)
        assert request.url.params["$filter"] == (
            f"lastModifiedDateTime gt {(NOW - timedelta(minutes=5)).isoformat()}"
        )
        raw = fixture("customer" if request.url.path.endswith("/customers") else "vendor")
        # A record modified during paging may be later than the poll start.
        raw["lastModifiedDateTime"] = (NOW + timedelta(seconds=5)).isoformat()
        return httpx.Response(200, json=collection([raw]))

    http = client_for(transport)
    batch = BCConnector(http, "synthetic-access", poll_started_at=NOW).incremental_sync(
        NOW.isoformat()
    )
    assert not batch.complete and batch.cursor == NOW.isoformat()
    assert len(batch.parties) == 2 and len(calls) == 2
    http.close()


@pytest.mark.parametrize("cursor", [None, "bad", (NOW + timedelta(seconds=1)).isoformat()])
def test_missing_invalid_future_cursor_uses_unfiltered_full(cursor):
    calls = []

    def transport(request):
        calls.append(request)
        assert "$filter" not in request.url.params
        return httpx.Response(200, json=collection([]))

    http = client_for(transport)
    assert (
        BCConnector(http, "synthetic-access", poll_started_at=NOW).incremental_sync(cursor).complete
    )
    assert len(calls) == 2
    http.close()


def test_anomalous_future_timestamp_never_advances_cursor(monkeypatch):
    monkeypatch.setattr(bc, "utcnow", lambda: NOW)
    raw = {**fixture("customer"), "lastModifiedDateTime": (NOW + timedelta(days=1)).isoformat()}
    http = client_for(lambda _: httpx.Response(200, json=collection([raw])))
    with pytest.raises(BCError, match="future_timestamp"):
        BCConnector(http, "synthetic-access", poll_started_at=NOW).full_sync()
    http.close()


def test_subscription_methods_quote_keys_preserve_actual_etags_and_accepted_missing_etag():
    calls = []
    key = "opaque'key"

    def transport(request):
        calls.append(request)
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.method == "POST":
            return httpx.Response(201, json={"subscriptionId": key})
        if request.method == "PATCH":
            return httpx.Response(200, json=fixture("subscription"), headers={"ETag": 'W/"header"'})
        return httpx.Response(200, json=collection([fixture("subscription")]))

    http = client_for(transport)
    created = http.subscription_request("POST", "synthetic-access", payload={"resource": "dummy"})
    assert (
        created.status_code == 201
        and created.body["subscriptionId"] == key
        and created.etag is None
    )
    assert "synthetic-private-client-state" not in repr(created)
    updated = http.subscription_request(
        "PATCH", "synthetic-access", subscription_id=key, payload={}, etag='W/"before"'
    )
    assert updated.etag == 'W/"header"' and updated.body["@odata.etag"]
    assert b"subscriptions('opaque%27%27key')" in calls[-1].url.raw_path
    assert calls[-1].headers["if-match"] == 'W/"before"'
    listed = http.subscription_request("GET", "synthetic-access")
    assert listed.body["value"][0]["@odata.etag"] == fixture("subscription")["@odata.etag"]
    deleted = http.subscription_request(
        "DELETE", "synthetic-access", subscription_id=key, etag=updated.etag
    )
    assert deleted.status_code == 204 and deleted.body is None and calls[-1].content == b""
    with pytest.raises(BCError, match="invalid_etag"):
        http.subscription_request("DELETE", "synthetic-access", subscription_id=key)
    assert len(calls) == 4
    http.close()


def test_sync_token_saved_before_pages_no_writer_http_partial_failure_and_namespace_fidelity(
    client, admin, connected
):
    factory, config = client.app.state.session_factory, client.app.state.config
    private = {
        "subscriptions": [
            {
                "resource": "customers",
                "subscription_id": "synthetic-private-subscription-id",
                "client_state": "synthetic-private-subscription-proof",
                "resource_path": f"api/v2.0/companies({COMPANY})/customers",
                "notification_url": "https://fixture.example.test/webhook",
            }
        ],
        "pending_cleanup": [
            {
                "tenant_id": TENANT,
                "environment": CONFIG.environment,
                "client_id": CLIENT_ID,
                "client_secret": APP.client_secret,
                "subscription_id": "synthetic-cleanup-subscription-id",
                "client_state": "synthetic-private-cleanup-proof",
                "resource_path": f"api/v2.0/companies({COMPANY})/customers",
                "notification_url": "https://fixture.example.test/webhook",
            }
        ],
    }
    with factory() as db:
        source = db.get(Source, connected)
        values = load_source_credentials(source, config)
        save_source_credentials(
            db, source, config, {**values, **private, "access_expires_at": NOW.isoformat()}
        )
        db.commit()
        initial_cursor = source.cursor
    calls = []

    def transport(request):
        calls.append(request)
        with factory() as other:
            other.execute(text("BEGIN IMMEDIATE"))
            values = load_source_credentials(other.get(Source, connected), config)
            if request.method == "GET":
                assert values["access_token"] == "new-synthetic-bc-access"
                assert values["subscriptions"] == private["subscriptions"]
                assert values["pending_cleanup"] == private["pending_cleanup"]
            other.rollback()
        if request.method == "POST":
            return httpx.Response(
                200, json={**fixture("token"), "access_token": "new-synthetic-bc-access"}
            )
        if request.url.path.endswith("/vendors"):
            return httpx.Response(200, json={"value": "invalid second resource"})
        return httpx.Response(200, json=collection([fixture("customer")]))

    with protected_operation(factory, "source_sync", source_id=connected) as execution:
        with operation_session(factory, execution, admin["user"]["id"]) as db:
            with pytest.raises(BCError, match="invalid_collection_response"):
                sync_bc_source(
                    db,
                    config,
                    db.get(Source, connected),
                    mode="full",
                    transport=httpx.MockTransport(transport),
                )
    with factory() as db:
        source = db.get(Source, connected)
        values = load_source_credentials(source, config)
        assert values["access_token"] == "new-synthetic-bc-access"
        assert source.cursor == initial_cursor and source.record_count == 0
        assert db.scalar(select(Party)) is None and db.scalar(select(ScreeningTask)) is None
        assert "new-synthetic-bc-access" not in source.credentials_enc
        audit_payload = json.dumps(
            [(row.before, row.after) for row in db.scalars(select(AuditLog))], default=str
        )
        assert (
            "new-synthetic-bc-access" not in audit_payload
            and APP.client_secret not in audit_payload
        )
    assert len(calls) == 3


def test_401_acquires_once_then_invalid_client_reconnect_is_truthful(client, admin, connected):
    factory, config = client.app.state.session_factory, client.app.state.config
    calls = []

    def transport(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(
                400,
                json={"error": "invalid_client", "error_description": "private credential detail"},
            )
        return httpx.Response(401)

    with factory() as db:
        with pytest.raises(BCError, match="invalid_client") as error:
            sync_bc_source(
                db, config, db.get(Source, connected), transport=httpx.MockTransport(transport)
            )
        assert "private credential detail" not in str(error.value)
    with factory() as db:
        source = db.get(Source, connected)
        assert source.config["connection_status"] == "reconnect_required"
        assert source.config["webhooks_enabled"] is False
        assert source.cursor == NOW.isoformat() and db.scalar(select(Party)) is None
    assert len(calls) == 2


@pytest.mark.parametrize("accepted", [True, False])
def test_401_reacquires_once_replays_original_page_and_never_loops(
    client, admin, connected, accepted
):
    factory, config = client.app.state.session_factory, client.app.state.config
    calls = []

    def transport(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(
                200, json={**fixture("token"), "access_token": "replacement-synthetic-bc-access"}
            )
        if (
            request.headers["authorization"] != "Bearer replacement-synthetic-bc-access"
            or not accepted
        ):
            return httpx.Response(401)
        assert request.url.params["$filter"].startswith("lastModifiedDateTime gt ")
        return httpx.Response(200, json=collection([]))

    with factory() as db:
        if accepted:
            result = sync_bc_source(
                db, config, db.get(Source, connected), transport=httpx.MockTransport(transport)
            )
            assert result["mode"] == "incremental" and result["record_count"] == 0
        else:
            with pytest.raises(BCError, match="unauthorized"):
                sync_bc_source(
                    db, config, db.get(Source, connected), transport=httpx.MockTransport(transport)
                )
    assert sum(request.method == "POST" for request in calls) == 1
    assert len(calls) == (4 if accepted else 3)
    with factory() as db:
        source = db.get(Source, connected)
        values = load_source_credentials(source, config)
        if accepted:
            assert values["access_token"] == "replacement-synthetic-bc-access"
        else:
            assert "access_token" not in values and "access_expires_at" not in values
        assert source.config["connection_status"] == (
            "connected" if accepted else "reconnect_required"
        )


@pytest.mark.parametrize("accepted", [True, False])
def test_expired_initial_issuance_has_separate_bounded_401_reissue_budget(
    client, admin, connected, accepted
):
    factory, config = client.app.state.session_factory, client.app.state.config
    with factory() as db:
        source = db.get(Source, connected)
        values = load_source_credentials(source, config)
        save_source_credentials(
            db, source, config, {**values, "access_expires_at": NOW.isoformat()}
        )
        db.commit()
    calls, issued = [], []

    def transport(request):
        calls.append(request)
        with factory() as other:
            other.execute(text("BEGIN IMMEDIATE"))
            other.rollback()
        if request.method == "POST":
            token = f"synthetic-separate-budget-{len(issued) + 1}"
            issued.append(token)
            return httpx.Response(200, json={**fixture("token"), "access_token": token})
        if request.headers["authorization"] != "Bearer synthetic-separate-budget-2" or not accepted:
            return httpx.Response(401)
        row = fixture("customer" if request.url.path.endswith("/customers") else "vendor")
        return httpx.Response(200, json=collection([row]))

    with protected_operation(factory, "source_sync", source_id=connected) as execution:
        with operation_session(factory, execution, admin["user"]["id"]) as db:
            if accepted:
                result = sync_bc_source(
                    db,
                    config,
                    db.get(Source, connected),
                    mode="full",
                    transport=httpx.MockTransport(transport),
                )
                assert result["added"] == 2 and result["record_count"] == 2
            else:
                with pytest.raises(BCError, match="unauthorized"):
                    sync_bc_source(
                        db,
                        config,
                        db.get(Source, connected),
                        mode="full",
                        transport=httpx.MockTransport(transport),
                    )
    assert len(issued) == 2 and len(calls) == (5 if accepted else 4)
    with factory() as db:
        source = db.get(Source, connected)
        if accepted:
            assert load_source_credentials(source, config)["access_token"] == issued[-1]
        else:
            assert source.config["connection_status"] == "reconnect_required"
            assert "access_token" not in load_source_credentials(source, config)
            assert source.cursor == NOW.isoformat() and db.scalar(select(Party)) is None


def test_long_pagination_proactively_renews_without_consuming_401_budget(
    client, admin, connected, monkeypatch
):
    factory, config = client.app.state.session_factory, client.app.state.config
    observed, issued, calls = [NOW], [], []
    monkeypatch.setattr(bc, "utcnow", lambda: observed[0])

    def transport(request):
        calls.append(request)
        with factory() as other:
            other.execute(text("BEGIN IMMEDIATE"))
            other.rollback()
        if request.method == "POST":
            token = f"synthetic-long-page-token-{len(issued) + 1}"
            issued.append(token)
            return httpx.Response(200, json={**fixture("token"), "access_token": token})
        if request.url.path.endswith("/customers") and not request.url.query:
            observed[0] = NOW + timedelta(hours=1)
            return httpx.Response(
                200,
                json=collection(
                    [fixture("customer")], **{"@odata.nextLink": "?$skiptoken=long-page"}
                ),
            )
        if request.url.path.endswith("/customers") and len(issued) == 1:
            return httpx.Response(401)
        assert request.headers["authorization"] == "Bearer synthetic-long-page-token-2"
        return httpx.Response(
            200,
            json=collection([] if request.url.path.endswith("/customers") else [fixture("vendor")]),
        )

    with factory() as db:
        result = sync_bc_source(
            db,
            config,
            db.get(Source, connected),
            mode="full",
            transport=httpx.MockTransport(transport),
        )
        assert result["record_count"] == 2 and db.get(Source, connected).cursor == NOW.isoformat()
    assert len(issued) == 2 and len(calls) == 6


def test_proactive_token_acquisitions_have_fixed_whole_sync_ceiling(
    client, admin, connected, monkeypatch
):
    factory, config = client.app.state.session_factory, client.app.state.config
    observed, issued, pages = [NOW], [], []
    monkeypatch.setattr(bc, "utcnow", lambda: observed[0])

    def transport(request):
        if request.method == "POST":
            issued.append(len(issued) + 1)
            return httpx.Response(
                200, json={**fixture("token"), "access_token": f"synthetic-ceiling-{len(issued)}"}
            )
        pages.append(str(request.url))
        observed[0] += timedelta(hours=1)
        return httpx.Response(
            200, json=collection([], **{"@odata.nextLink": f"?$skiptoken=page-{len(pages)}"})
        )

    with factory() as db:
        with pytest.raises(BCError, match="token_acquisition_limit"):
            sync_bc_source(
                db,
                config,
                db.get(Source, connected),
                mode="full",
                transport=httpx.MockTransport(transport),
            )
        assert (
            db.get(Source, connected).cursor == NOW.isoformat() and db.scalar(select(Party)) is None
        )
        assert db.get(Source, connected).config["connection_status"] == "connected"
    assert len(issued) == bc.MAX_TOKEN_ACQUISITIONS


@pytest.mark.parametrize("queue_failure", [False, True])
def test_auth_failure_keeps_current_subscription_custody_system_queue_and_original_evidence(
    client, admin, connected, monkeypatch, queue_failure
):
    from app.connectors.bc_subscriptions import cleanup_obligation
    from app.scheduler import service
    from app.scheduler.runtime import SchedulerRuntime
    from app.screening.engine import snapshot

    factory, config = client.app.state.session_factory, client.app.state.config
    rows = json.loads((FIXTURES / "csl_sample_v1.json").read_text())
    name = next(row["name"] for row in rows if row.get("type", "").lower() != "individual")

    def seed_transport(request):
        row = fixture("customer" if request.url.path.endswith("/customers") else "vendor")
        if request.url.path.endswith("/customers"):
            row["displayName"] = name
        return httpx.Response(200, json=collection([row]))

    with factory() as db:
        refresh_csl(db, file=FIXTURES / "csl_sample_v1.json")
        db.commit()
        source = db.get(Source, connected)
        sync_bc_source(
            db, config, source, mode="full", transport=httpx.MockTransport(seed_transport)
        )
        process_pending_tasks(db)
        party = db.scalar(select(Party).where(Party.role == "customer"))
        selected = next(row for row in queue_hits(db) if row.party_id == party.id)
        decided = client.post(
            f"/api/v1/hits/{selected.id}/dispositions",
            headers={"x-csrf-token": admin["csrf_token"]},
            json={"kind": "false_positive", "note": "Synthetic original BC custody decision"},
        )
        assert decided.status_code == 201
        db.expire_all()
        source = db.get(Source, connected)
        values = load_source_credentials(source, config)
        subscriptions = [
            {
                "resource": entity,
                "subscription_id": f"synthetic-held-{entity}",
                "client_state": f"synthetic-private-held-state-{entity}",
                "resource_path": f"api/v2.0/companies({COMPANY})/{entity}",
                "notification_url": "https://fixture.example.test/webhook",
                "etag": f'W/"held-{entity}"',
                "expiration_date_time": (NOW + timedelta(days=3)).isoformat(),
            }
            for entity in ("customers", "vendors")
        ]
        unknown = cleanup_obligation(
            CONFIG,
            None,
            {
                **subscriptions[0],
                "subscription_id": None,
                "client_state": "synthetic-private-unknown-state",
            },
        )
        save_source_credentials(
            db,
            source,
            config,
            {**values, "subscriptions": subscriptions, "pending_cleanup": [unknown]},
        )
        source.config = {**source.config, "webhooks_enabled": True}
        db.commit()
        party_before = {row.id: snapshot(row) for row in db.scalars(select(Party))}
        hit_before = {row.id: snapshot(row) for row in db.scalars(select(Hit))}
        decision_before = {row.id: snapshot(row) for row in db.scalars(select(Disposition))}
        task_before = set(db.scalars(select(ScreeningTask.id)))
        boundary = source.cursor, source.last_synced_at, source.config["last_full_sync_at"]

    if queue_failure:

        def fail_queue(*args, **kwargs):
            raise BCError("synthetic_queue_failure")

        monkeypatch.setattr(service, "enqueue_bc_subscriptions", fail_queue)
    calls = []

    def transport(request):
        calls.append(request)
        with factory() as other:
            other.execute(text("BEGIN IMMEDIATE"))
            other.rollback()
        return (
            httpx.Response(400, json={"error": "invalid_client"})
            if request.method == "POST"
            else httpx.Response(401)
        )

    with protected_operation(factory, "source_sync", source_id=connected) as execution:
        with operation_session(factory, execution, admin["user"]["id"]) as db:
            with pytest.raises(BCError, match="invalid_client"):
                sync_bc_source(
                    db,
                    config,
                    db.get(Source, connected),
                    mode="full",
                    transport=httpx.MockTransport(transport),
                )
    assert len(calls) == 2
    with factory() as db:
        source = db.get(Source, connected)
        current = load_source_credentials(source, config)
        queued = list(
            db.scalars(
                select(OperationAttempt).where(OperationAttempt.operation == "bc_subscriptions")
            )
        )
        assert {row.id: snapshot(row) for row in db.scalars(select(Party))} == party_before
        assert {row.id: snapshot(row) for row in db.scalars(select(Hit))} == hit_before
        assert {row.id: snapshot(row) for row in db.scalars(select(Disposition))} == decision_before
        assert set(db.scalars(select(ScreeningTask.id))) == task_before
        assert (
            source.cursor,
            source.last_synced_at,
            source.config["last_full_sync_at"],
        ) == boundary
        if queue_failure:
            assert (
                not queued
                and current["subscriptions"] == subscriptions
                and current["pending_cleanup"] == [unknown]
            )
            assert (
                current["access_token"] == values["access_token"]
                and source.config["connection_status"] == "connected"
            )
            return
        assert (
            source.config["connection_status"] == "reconnect_required"
            and source.config["webhooks_enabled"] is False
        )
        assert source.config["cleanup_status"] == "pending" and current["subscriptions"] == []
        assert "access_token" not in current and "access_expires_at" not in current
        assert current["pending_cleanup"][0] == unknown
        assert current["pending_cleanup"][1:] == [
            cleanup_obligation(CONFIG, values["access_token"], row) for row in subscriptions
        ]
        assert len(queued) == 1 and queued[0].actor_user_id is None and queued[0].status == "queued"
        recorded = db.scalar(select(AuditLog).where(AuditLog.action == "bc.auth_cleanup_queued"))
        assert recorded.actor_user_id is None and recorded.after["attempt_id"] == queued[0].id
        content = json.dumps(
            [(row.before, row.after) for row in db.scalars(select(AuditLog))], default=str
        )
        assert (
            APP.client_secret not in content
            and "synthetic-private-held-state" not in content
            and "synthetic-private-unknown-state" not in content
        )
    runtime = SchedulerRuntime(factory, config)
    try:
        jobs = {row.id: row for row in runtime.scheduler.get_jobs()}
        assert "source-" + connected not in jobs
        assert "bc-subscriptions-" + connected in jobs
        assert jobs["bc-subscriptions-" + connected].trigger.interval == timedelta(hours=12)
    finally:
        runtime.shutdown()


def test_auth_failure_at_cleanup_capacity_keeps_every_unknown_and_known_obligation(
    client, admin, connected
):
    from app.connectors.bc_subscriptions import cleanup_obligation

    factory, config = client.app.state.session_factory, client.app.state.config
    subscription = {
        "resource": "customers",
        "subscription_id": "synthetic-known-at-capacity",
        "client_state": "synthetic-known-capacity-state",
        "resource_path": f"api/v2.0/companies({COMPANY})/customers",
        "notification_url": "https://fixture.example.test/webhook",
        "etag": 'W/"known-capacity-etag"',
    }
    unknowns = [
        cleanup_obligation(
            CONFIG,
            None,
            {
                **subscription,
                "subscription_id": None,
                "client_state": f"synthetic-unknown-capacity-{index}",
            },
        )
        for index in range(32)
    ]
    with factory() as db:
        source = db.get(Source, connected)
        values = load_source_credentials(source, config)
        save_source_credentials(
            db,
            source,
            config,
            {**values, "subscriptions": [subscription], "pending_cleanup": unknowns},
        )
        db.commit()

        def transport(request):
            return (
                httpx.Response(400, json={"error": "invalid_client"})
                if request.method == "POST"
                else httpx.Response(401)
            )

        with pytest.raises(BCError, match="invalid_client"):
            sync_bc_source(db, config, source, transport=httpx.MockTransport(transport))
    with factory() as db:
        source = db.get(Source, connected)
        values = load_source_credentials(source, config)
        assert values["pending_cleanup"] == unknowns and values["subscriptions"] == [subscription]
        assert source.config["cleanup_status"] == "pending" and "access_token" not in values
        queued = db.scalar(
            select(OperationAttempt).where(OperationAttempt.operation == "bc_subscriptions")
        )
        assert queued is not None and queued.actor_user_id is None and queued.status == "queued"


def test_token_cache_keeps_latest_cleanup_intent_added_during_http(client, admin, connected):
    factory, config = client.app.state.session_factory, client.app.state.config
    intent = {
        "tenant_id": TENANT,
        "environment": CONFIG.environment,
        "client_id": CLIENT_ID,
        "client_secret": APP.client_secret,
        "subscription_id": None,
        "client_state": "synthetic-late-private-cleanup-state",
        "resource_path": f"api/v2.0/companies({COMPANY})/customers",
        "notification_url": "https://fixture.example.test/webhook",
        "purpose": "cleanup",
        "source_generation": 1,
        "app_generation": 1,
    }
    with factory() as db:
        source = db.get(Source, connected)
        values = load_source_credentials(source, config)
        save_source_credentials(
            db, source, config, {**values, "access_expires_at": NOW.isoformat()}
        )
        db.commit()

    def transport(request):
        with factory() as other:
            other.execute(text("BEGIN IMMEDIATE"))
            source = other.get(Source, connected)
            values = load_source_credentials(source, config)
            if request.method == "POST":
                save_source_credentials(
                    other, source, config, {**values, "pending_cleanup": [intent]}
                )
                other.commit()
            else:
                assert values["pending_cleanup"] == [intent]
                assert values["access_token"] == "synthetic-token-with-late-cleanup"
                other.rollback()
        return httpx.Response(
            200,
            json={**fixture("token"), "access_token": "synthetic-token-with-late-cleanup"}
            if request.method == "POST"
            else collection([]),
        )

    with protected_operation(factory, "source_sync", source_id=connected) as execution:
        with operation_session(factory, execution, admin["user"]["id"]) as db:
            result = sync_bc_source(
                db, config, db.get(Source, connected), transport=httpx.MockTransport(transport)
            )
            assert result["applied"]
    with factory() as db:
        source = db.get(Source, connected)
        assert load_source_credentials(source, config)["pending_cleanup"] == [intent]
        recorded = json.dumps(
            [(row.before, row.after) for row in db.scalars(select(AuditLog))], default=str
        )
        assert intent["client_state"] not in recorded and APP.client_secret not in recorded


@pytest.mark.parametrize("change", ["offboard", "disconnect", "generation", "credential"])
def test_token_acquisition_after_authority_change_discards_stale_token(
    client, admin, connected, change
):
    factory, config = client.app.state.session_factory, client.app.state.config
    with factory() as db:
        source = db.get(Source, connected)
        values = load_source_credentials(source, config)
        save_source_credentials(
            db, source, config, {**values, "access_expires_at": NOW.isoformat()}
        )
        db.commit()
    calls = []

    def transport(request):
        calls.append(request)
        assert request.method == "POST"
        with factory() as other:
            other.execute(text("BEGIN IMMEDIATE"))
            source = other.get(Source, connected)
            if change == "offboard":
                other.get(User, admin["user"]["id"]).active = False
            elif change == "disconnect":
                source.config = {**source.config, "connection_status": "disconnecting"}
            elif change == "generation":
                source.config = {**source.config, "connection_generation": 2}
            else:
                values = load_source_credentials(source, config)
                save_source_credentials(
                    other, source, config, {**values, "access_token": "newer-synthetic-token"}
                )
            other.commit()
        return httpx.Response(
            200, json={**fixture("token"), "access_token": "stale-rejected-synthetic-token"}
        )

    with pytest.raises(BCError):
        with protected_operation(factory, "source_sync", source_id=connected) as execution:
            with operation_session(factory, execution, admin["user"]["id"]) as db:
                sync_bc_source(
                    db, config, db.get(Source, connected), transport=httpx.MockTransport(transport)
                )
    assert len(calls) == 1
    with factory() as db:
        source = db.get(Source, connected)
        assert (
            load_source_credentials(source, config)["access_token"]
            != "stale-rejected-synthetic-token"
        )
        assert source.cursor == NOW.isoformat() and db.scalar(select(Party)) is None
        assert db.scalar(select(ScreeningTask)) is None


def test_failed_full_second_page_keeps_existing_book_cursor_and_full_boundary(
    client, admin, connected, monkeypatch
):
    factory, config = client.app.state.session_factory, client.app.state.config
    fail = False

    def transport(request):
        if fail:
            if request.url.query:
                return httpx.Response(200, json={"value": None})
            return httpx.Response(
                200,
                json=collection(
                    [{**fixture("customer"), "displayName": "Never partially apply this edit"}],
                    **{"@odata.nextLink": "?$skiptoken=synthetic-next-page"},
                ),
            )
        raw = fixture("customer" if request.url.path.endswith("/customers") else "vendor")
        return httpx.Response(200, json=collection([raw]))

    http_transport = httpx.MockTransport(transport)
    with factory() as db:
        sync_bc_source(db, config, db.get(Source, connected), mode="full", transport=http_transport)
        before = {
            row.id: (row.record_hash, deepcopy(row.raw), row.name, row.last_seen_at, row.deleted_at)
            for row in db.scalars(select(Party))
        }
        source = db.get(Source, connected)
        cursor, last_synced, last_full = (
            source.cursor,
            source.last_synced_at,
            source.config["last_full_sync_at"],
        )
        fail = True
        monkeypatch.setattr(bc, "utcnow", lambda: NOW + timedelta(minutes=10))
        with pytest.raises(BCError, match="invalid_collection_response"):
            sync_bc_source(db, config, source, mode="full", transport=http_transport)
        assert {
            row.id: (row.record_hash, row.raw, row.name, row.last_seen_at, row.deleted_at)
            for row in db.scalars(select(Party))
        } == before
        source = db.get(Source, connected)
        assert source.cursor == cursor and source.last_synced_at == last_synced
        assert source.config["last_full_sync_at"] == last_full and source.record_count == 2
        assert db.scalar(select(ScreeningTask)) is None


@pytest.mark.parametrize("change", ["offboard", "disconnect", "app", "credential", "fence"])
def test_late_authority_identity_and_fence_reject_party_and_cursor_writes(
    client, admin, connected, change
):
    factory, config = client.app.state.session_factory, client.app.state.config
    changed = False

    def transport(request):
        nonlocal changed
        with factory() as other:
            other.execute(text("BEGIN IMMEDIATE"))
            source = other.get(Source, connected)
            if not changed:
                if change == "offboard":
                    other.get(User, admin["user"]["id"]).active = False
                elif change == "disconnect":
                    source.config = {**source.config, "connection_status": "disconnecting"}
                elif change == "app":
                    save_app_config(other, config, replace(APP, generation=2))
                elif change == "credential":
                    values = load_source_credentials(source, config)
                    save_source_credentials(
                        other,
                        source,
                        config,
                        {**values, "access_token": "replacement-synthetic-token"},
                    )
                else:
                    other.execute(
                        text("UPDATE operation_leases SET fence=fence+1 WHERE key=:key"),
                        {"key": f"source-sync:{connected}"},
                    )
                other.commit()
                changed = True
            else:
                other.rollback()
        raw = fixture("customer" if request.url.path.endswith("/customers") else "vendor")
        return httpx.Response(200, json=collection([raw]))

    from app.scheduler.leases import OperationLeaseLostError

    with pytest.raises((BCError, OperationLeaseLostError)):
        with protected_operation(factory, "source_sync", source_id=connected) as execution:
            with operation_session(factory, execution, admin["user"]["id"]) as db:
                sync_bc_source(
                    db,
                    config,
                    db.get(Source, connected),
                    mode="full",
                    transport=httpx.MockTransport(transport),
                )
    with factory() as db:
        source = db.get(Source, connected)
        assert source.cursor == NOW.isoformat()
        assert source.config["last_full_sync_at"] == NOW.isoformat()
        assert source.record_count == 0 and db.scalar(select(Party)) is None
        assert db.scalar(select(ScreeningTask)) is None


def test_actual_full_incremental_type_edit_hard_delete_screening_and_unchanged_no_write(
    client, admin, connected
):
    factory, config = client.app.state.session_factory, client.app.state.config
    listings = json.loads((FIXTURES / "csl_sample_v1.json").read_text())
    exact = next(row for row in listings if row.get("type", "").lower() != "individual")
    customer = {**fixture("customer"), "displayName": exact["name"]}
    vendor = fixture("vendor")
    state = {"customers": [customer], "vendors": [vendor]}

    def transport(request):
        entity = "customers" if request.url.path.endswith("/customers") else "vendors"
        return httpx.Response(200, json=collection(state[entity]))

    http_transport = httpx.MockTransport(transport)
    with factory() as db:
        refresh_csl(db, file=FIXTURES / "csl_sample_v1.json")
        db.commit()
        first = sync_bc_source(
            db, config, db.get(Source, connected), mode="full", transport=http_transport
        )
        assert first["added"] == 2 and first["task_ids"]
        runs = process_pending_tasks(db)
        party = db.scalar(select(Party).where(Party.role == "customer"))
        party_id, old_hash, old_raw = party.id, party.record_hash, deepcopy(party.raw)
        hits = [row for row in queue_hits(db) if row.party_id == party_id]
        assert hits and runs
        original = hits[0]
        decided = client.post(
            f"/api/v1/hits/{original.id}/dispositions",
            headers={"x-csrf-token": admin["csrf_token"]},
            json={"kind": "false_positive", "note": "Synthetic BC original decision"},
        )
        assert decided.status_code == 201
        original_id, human_ids = original.id, set(db.scalars(select(Disposition.id)))

        state["customers"] = [{**customer, "phoneNumber": "dummy metadata edit", "blocked": " "}]
        repeated = sync_bc_source(db, config, db.get(Source, connected), transport=http_transport)
        assert repeated["unchanged"] == 2 and repeated["task_ids"] == []
        party = db.get(Party, party_id)
        assert party.raw == old_raw and party.record_hash == old_hash
        repeated_run = run_screening(db, party_ids=[party_id])
        assert repeated_run.hits_suppressed >= 1
        assert set(db.scalars(select(Disposition.id))) == human_ids

        # Only type changes; keep published identity and names stable.
        state["customers"] = [{**customer, "type": "Person"}]
        edited = sync_bc_source(db, config, db.get(Source, connected), transport=http_transport)
        assert edited["changed"] == 1 and edited["task_ids"]
        task = db.get(ScreeningTask, edited["task_ids"][0])
        assert task.party_ids == [party_id]
        changed_runs = process_pending_tasks(db)
        reopened = list(
            db.scalars(select(Hit).where(Hit.run_id.in_([run.id for run in changed_runs])))
        )
        assert any(hit.explanation.get("prior_disposition_id") in human_ids for hit in reopened)
        assert db.get(Party, party_id).record_hash != old_hash
        assert db.get(Hit, original_id).party_hash == old_hash
        assert all(
            db.get(Disposition, id).note == "Synthetic BC original decision"
            and db.get(Disposition, id).actor_user_id == admin["user"]["id"]
            for id in human_ids
        )

        # An incremental absence is not a deletion; successful full absence is.
        state["vendors"] = []
        incremental = sync_bc_source(
            db, config, db.get(Source, connected), transport=http_transport
        )
        assert incremental["deleted"] == 0
        full = sync_bc_source(
            db, config, db.get(Source, connected), mode="full", transport=http_transport
        )
        assert full["deleted"] == 1 and full["record_count"] == 1
        assert db.scalar(select(Party).where(Party.role == "vendor")).deleted_at
        assert db.get(Source, connected).config["last_full_sync_at"] == NOW.isoformat()


def test_daily_reconciliation_due_overrides_recent_incremental_cursor(client, admin, connected):
    factory, config = client.app.state.session_factory, client.app.state.config
    with factory() as db:
        source = db.get(Source, connected)
        source.config = {
            **source.config,
            "last_full_sync_at": (NOW - timedelta(days=1)).isoformat(),
        }
        db.commit()
    calls = []

    def transport(request):
        calls.append(request)
        assert "$filter" not in request.url.params
        return httpx.Response(200, json=collection([]))

    with factory() as db:
        result = sync_bc_source(
            db, config, db.get(Source, connected), transport=httpx.MockTransport(transport)
        )
        assert (
            result["mode"] == "full"
            and db.get(Source, connected).config["last_full_sync_at"] == NOW.isoformat()
        )
    assert len(calls) == 2
