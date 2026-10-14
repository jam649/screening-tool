"""Synthetic official-contract-derived QBO responses; no live sandbox claims."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import select, text

from app.connectors import qbo
from app.connectors.qbo import (
    QBOClient,
    QBOConnector,
    QBOError,
    normalize_entity,
    sync_qbo_source,
    token_credentials,
)
from app.connectors.qbo_common import (
    AppConfig,
    load_source_credentials,
    save_app_config,
    save_source_credentials,
)
from app.csl.service import refresh_csl
from app.models import AuditLog, Disposition, Hit, Party, ScreeningTask, Source, User
from app.screening.engine import process_pending_tasks, queue_hits, run_screening

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
APP = AppConfig("sandbox", "synthetic-client", "synthetic-secret", generation=1)


def fixture(name):
    return json.loads((FIXTURES / "qbo" / f"{name}.json").read_text())


def query(entity, rows, **metadata):
    return {"QueryResponse": {entity: rows, **metadata}, "time": NOW.isoformat()}


def cdc(customer=None, vendor=None, **metadata):
    return {
        "CDCResponse": [
            {"QueryResponse": [{"Customer": customer or [], "Vendor": vendor or [], **metadata}]}
        ],
        "time": NOW.isoformat(),
    }


def client_for(handler, pauses=None):
    return QBOClient(
        APP,
        transport=httpx.MockTransport(handler),
        pause=(pauses.append if pauses is not None else lambda _: None),
    )


@pytest.fixture
def connected(client, admin, monkeypatch):
    monkeypatch.setattr(qbo, "utcnow", lambda: NOW)
    with client.app.state.session_factory() as db:
        save_app_config(db, client.app.state.config, APP)
        source = Source(
            kind="qbo",
            label="Synthetic Intuit company",
            config={
                "environment": "sandbox",
                "realm_id": "12345",
                "connection_status": "connected",
                "connection_generation": 1,
            },
        )
        db.add(source)
        db.flush()
        credentials = token_credentials(
            fixture("tokens"), realm_id="12345", app_generation=1, source_generation=1, now=NOW
        )
        credentials["client_id"] = APP.client_id
        save_source_credentials(db, source, client.app.state.config, credentials)
        db.commit()
        return source.id


def test_oauth_exact_verified_endpoints_encoding_and_actual_rotating_metadata():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, json={} if request.url.path.endswith("revoke") else fixture("tokens")
        )

    client = client_for(handler)
    try:
        exchanged = client.exchange_code("synthetic-code", "https://fixture.example.test/callback")
        refreshed = client.refresh("synthetic-old-refresh")
        client.revoke("synthetic-refresh-rotated")
    finally:
        client.close()
    assert [str(request.url) for request in requests] == [
        qbo.TOKEN_URL,
        qbo.TOKEN_URL,
        qbo.REVOKE_URL,
    ]
    assert all(
        request.method == "POST" and request.headers["authorization"].startswith("Basic ")
        for request in requests
    )
    assert parse_qs(requests[0].content.decode())["grant_type"] == ["authorization_code"]
    assert parse_qs(requests[1].content.decode())["refresh_token"] == ["synthetic-old-refresh"]
    assert json.loads(requests[2].content) == {"token": "synthetic-refresh-rotated"}
    saved = token_credentials(
        refreshed, realm_id="12345", app_generation=1, source_generation=2, now=NOW
    )
    assert saved["refresh_token"] == exchanged["refresh_token"] == "synthetic-refresh-rotated"
    assert saved["access_expires_at"] == (NOW + timedelta(hours=1)).isoformat()
    assert saved["refresh_hard_expires_at"] == (NOW + timedelta(seconds=157680000)).isoformat()
    assert "synthetic-secret" not in repr(APP)


@pytest.mark.parametrize(
    "change",
    [
        {"refresh_token": None},
        {"access_token": "bad\nheader"},
        {"expires_in": True},
        {"x_refresh_token_expires_in": 0},
        {"token_type": "MAC"},
        {"x_refresh_token_hard_expires_in": 1.5},
    ],
)
def test_token_response_requires_actual_tokens_and_strict_lifetimes(change):
    with pytest.raises(QBOError, match="invalid_token_response"):
        token_credentials(
            fixture("tokens") | change,
            realm_id="12345",
            app_generation=1,
            source_generation=1,
            now=NOW,
        )


def test_get_retries_bounded_but_token_uncertain_post_never_replays():
    pauses, calls = [], []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(429 if len(calls) == 1 else 503, headers={"Retry-After": "100"})
        return httpx.Response(200, json=query("Customer", []))

    client = client_for(handler, pauses)
    assert client.data_json(
        "12345", "fixture-access", "query", {"query": "SELECT * FROM Customer"}
    )["QueryResponse"]
    assert len(calls) == 3 and pauses == [5, 5]
    client.close()
    calls.clear()

    def uncertain(request):
        calls.append(request)
        raise httpx.ReadTimeout("private-token-in-transport-exception")

    client = client_for(uncertain)
    with pytest.raises(QBOError) as exc:
        client.refresh("synthetic-refresh")
    assert len(calls) == 1 and "private-token" not in str(exc.value)
    client.close()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"Location": "https://untrusted.example.test/steal"}),
        httpx.Response(400, json={"error": "invalid_grant", "error_description": "private-secret"}),
        httpx.Response(200, json={"Fault": {"private": "secret"}}),
        httpx.Response(200, json=[]),
        httpx.Response(200, text="private malformed json"),
    ],
)
def test_http_errors_redirects_and_faults_are_sanitized(response):
    calls = []
    client = client_for(lambda request: (calls.append(request), response)[1])
    with pytest.raises(QBOError) as exc:
        client.data_json("12345", "fixture-access", "query", {})
    assert len(calls) == 1 and calls[0].url.host == "sandbox-quickbooks.api.intuit.com"
    assert "private" not in str(exc.value) and "secret" not in str(exc.value)
    client.close()


@pytest.mark.parametrize(
    "realm", ["../../evil", "https://evil.example", "1?token=secret", 12345, ""]
)
def test_untrusted_company_identifiers_never_reach_transport(realm):
    calls = []
    client = client_for(lambda request: calls.append(request))
    with pytest.raises(QBOError, match="invalid_identifier"):
        client.data_json(realm, "fixture", "query", {})
    assert not calls
    client.close()


def test_normalization_identity_types_addresses_aliases_and_unmodified_raw():
    customer, vendor = fixture("customer"), fixture("vendor")
    person, company = normalize_entity("Customer", customer), normalize_entity("Vendor", vendor)
    assert person.external_id == "Customer:1" and company.external_id == "Vendor:1"
    assert person.party_type == "individual" and company.party_type == "company"
    assert (
        company.name == vendor["CompanyName"] and "Synthetic Contact Only" not in company.alt_names
    )
    assert company.alt_names == [vendor["DisplayName"], vendor["PrintOnCheckName"]]
    assert company.address_lines == ["10 Synthetic Wharf", "20 Synthetic Dock"]
    assert person.country_iso2 == "US" and person.city == "Boston"
    assert person.raw == customer and company.raw == vendor
    vendor["CompanyName"] = "Changed mutable input"
    assert company.raw["CompanyName"] != vendor["CompanyName"]
    assert (
        normalize_entity(
            "Customer", {"Id": "2", "Active": True, "DisplayName": "Only Display Name"}
        ).party_type
        == "company"
    )
    assert normalize_entity("Vendor", {"Id": "9", "status": "Deleted"}).deleted
    assert normalize_entity("Customer", {"Id": "9", "Active": False}).name == ""


@pytest.mark.parametrize(
    "raw",
    [
        {"Id": "1", "Active": "false", "DisplayName": "Bad"},
        {"Id": 1, "Active": True, "DisplayName": "Bad"},
        {"Id": "1", "Active": True},
        {"Id": "1", "Active": True, "DisplayName": "Valid", "BillAddr": []},
        {
            "Id": "1",
            "Active": True,
            "DisplayName": "Valid",
            "BillAddr": {"Country": "unknown-land"},
        },
    ],
)
def test_malformed_records_are_rejected_instead_of_partially_reconciled(raw):
    with pytest.raises(QBOError):
        normalize_entity("Customer", raw)


def test_full_query_numeric_pagination_includes_inactive_and_namespace_collisions():
    calls = []

    def handler(request):
        sql = request.url.params["query"]
        calls.append(sql)
        assert "WHERE Active IN (true, false)" in sql
        if "FROM Vendor" in sql:
            return httpx.Response(
                200, json=query("Vendor", [fixture("vendor")], startPosition=1, maxResults=1)
            )
        rows = (
            [fixture("customer"), {"Id": "2", "Active": False}]
            if "STARTPOSITION 1 " in sql
            else [fixture("customer") | {"Id": "3"}]
        )
        return httpx.Response(
            200,
            json=query(
                "Customer", rows, startPosition=1 if len(rows) == 2 else 3, maxResults=len(rows)
            ),
        )

    client = client_for(handler)
    batch = QBOConnector(client, "12345", "access", page_size=2, poll_started_at=NOW).full_sync()
    assert batch.complete and batch.cursor == NOW.isoformat()
    assert [row.external_id for row in batch.parties] == [
        "Customer:1",
        "Customer:2",
        "Customer:3",
        "Vendor:1",
    ]
    assert batch.parties[1].deleted and len(calls) == 3 and "STARTPOSITION 3" in calls[1]
    client.close()


@pytest.mark.parametrize(
    "page",
    [
        {"Invoice": [fixture("customer")]},
        {"Customer": [fixture("customer"), fixture("customer")]},
        {"Customer": [fixture("customer")], "totalCount": 2},
        {"Customer": [fixture("customer")], "maxResults": True},
    ],
)
def test_full_wrong_entity_duplicate_or_incomplete_metadata_never_produces_complete_batch(page):
    client = client_for(lambda _: httpx.Response(200, json={"QueryResponse": page}))
    try:
        with pytest.raises(QBOError):
            QBOConnector(client, "12345", "access", poll_started_at=NOW).full_sync()
    finally:
        client.close()


def test_cdc_overlap_poll_start_cursor_empty_and_tombstone_shape():
    calls = []
    client = client_for(
        lambda request: (calls.append(request), httpx.Response(200, json=fixture("cdc")))[1]
    )
    cursor = NOW - timedelta(hours=1)
    batch = QBOConnector(client, "12345", "access", poll_started_at=NOW).incremental_sync(
        cursor.isoformat()
    )
    assert not batch.complete and batch.cursor == NOW.isoformat()
    assert calls[0].url.params["changedSince"] == (cursor - timedelta(minutes=5)).isoformat()
    assert all(row.deleted for row in batch.parties)
    client.close()
    client = client_for(
        lambda _: httpx.Response(200, json={"CDCResponse": [{"QueryResponse": [{}]}]})
    )
    assert (
        QBOConnector(client, "12345", "access", poll_started_at=NOW)
        .incremental_sync(cursor.isoformat())
        .parties
        == []
    )
    client.close()


@pytest.mark.parametrize(
    "cursor",
    [
        None,
        "not-a-date",
        (NOW - timedelta(days=31)).isoformat(),
        (NOW + timedelta(minutes=1)).isoformat(),
    ],
)
def test_invalid_or_stale_cdc_cursor_uses_full_not_remote_cdc(cursor):
    calls = []
    client = client_for(
        lambda request: (
            calls.append(request.url.path),
            httpx.Response(200, json={"QueryResponse": {}}),
        )[1]
    )
    batch = QBOConnector(client, "12345", "access", poll_started_at=NOW).incremental_sync(cursor)
    assert batch.complete and all(path.endswith("/query") for path in calls)
    client.close()


@pytest.mark.parametrize("saturated", [True, False])
def test_cdc_cap_and_uncertain_count_trigger_validated_full_fallback(saturated):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("cdc"):
            rows = (
                [{"Id": str(i), "status": "Deleted"} for i in range(1000)]
                if saturated
                else [fixture("customer")]
            )
            return httpx.Response(
                200,
                json=cdc(
                    customer=rows,
                    maxResults=len(rows),
                    totalCount=len(rows) + (0 if saturated else 1),
                ),
            )
        return httpx.Response(200, json={"QueryResponse": {}})

    client = client_for(handler)
    batch = QBOConnector(client, "12345", "access", poll_started_at=NOW).incremental_sync(
        (NOW - timedelta(hours=1)).isoformat()
    )
    assert batch.complete and len(calls) == 3 and calls[0].endswith("cdc")
    client.close()


def test_sync_rotation_before_data_no_network_writer_lock_and_partial_failure_atomicity(
    client, admin, connected
):
    factory, config = client.app.state.session_factory, client.app.state.config
    calls = []
    with factory() as db:
        source = db.get(Source, connected)
        credentials = load_source_credentials(source, config)
        credentials["access_expires_at"] = NOW.isoformat()
        save_source_credentials(db, source, config, credentials)
        db.commit()

    def handler(request):
        with factory() as other:
            other.execute(text("BEGIN IMMEDIATE"))
            other.rollback()
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(
                200, json=fixture("tokens") | {"refresh_token": "new-accepted-rotation"}
            )
        with factory() as other:
            assert (
                load_source_credentials(other.get(Source, connected), config)["refresh_token"]
                == "new-accepted-rotation"
            )
        if "Vendor" in request.url.params["query"]:
            return httpx.Response(200, json={"QueryResponse": {"Vendor": "malformed-private-data"}})
        return httpx.Response(200, json=query("Customer", [fixture("customer")]))

    with factory() as db:
        with pytest.raises(QBOError):
            sync_qbo_source(
                db,
                config,
                db.get(Source, connected),
                mode="full",
                transport=httpx.MockTransport(handler),
            )
        assert db.scalar(select(Party)) is None and db.scalar(select(ScreeningTask)) is None
        source = db.get(Source, connected)
        assert source.cursor is None and source.last_synced_at is None
        assert load_source_credentials(source, config)["refresh_token"] == "new-accepted-rotation"
        rotation = db.scalar(select(AuditLog).where(AuditLog.action == "qbo.tokens_rotated"))
        assert rotation.actor_user_id is None and "new-accepted" not in json.dumps(rotation.after)
    assert len(calls) == 3


def test_401_refresh_once_and_invalid_grant_marks_reconnect_without_record_changes(
    client, admin, connected
):
    factory, config = client.app.state.session_factory, client.app.state.config
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(
                400, json={"error": "invalid_grant", "error_description": "private-refresh-token"}
            )
        return httpx.Response(401)

    with factory() as db:
        with pytest.raises(QBOError, match="invalid_grant"):
            sync_qbo_source(
                db,
                config,
                db.get(Source, connected),
                mode="full",
                transport=httpx.MockTransport(handler),
            )
        source = db.get(Source, connected)
        assert source.config["connection_status"] == "reconnect_required" and source.cursor is None
        assert "private-refresh" not in source.last_error
        assert db.scalar(select(Party)) is None
    assert len(calls) == 2


@pytest.mark.parametrize("race", ["offboard", "disconnect", "generation"])
def test_returned_rotation_survives_offboard_or_disconnect_but_data_is_never_applied(
    client, admin, connected, race
):
    factory, config = client.app.state.session_factory, client.app.state.config
    with factory() as db:
        source = db.get(Source, connected)
        credentials = load_source_credentials(source, config)
        credentials["access_expires_at"] = NOW.isoformat()
        save_source_credentials(db, source, config, credentials)
        db.commit()

    def handler(request):
        if request.method == "POST":
            with factory() as other:
                source = other.get(Source, connected)
                if race == "offboard":
                    other.get(User, admin["user"]["id"]).active = False
                elif race == "disconnect":
                    source.config = {**source.config, "connection_status": "disconnecting"}
                else:
                    source.config = {**source.config, "connection_generation": 2}
                other.commit()
            return httpx.Response(
                200, json=fixture("tokens") | {"refresh_token": "accepted-race-rotation"}
            )
        return httpx.Response(
            200,
            json=query(
                "Vendor" if "Vendor" in request.url.params["query"] else "Customer",
                [
                    fixture("vendor")
                    if "Vendor" in request.url.params["query"]
                    else fixture("customer")
                ],
            ),
        )

    with factory() as db:
        db.info["actor_user_id"] = admin["user"]["id"]
        with pytest.raises(QBOError):
            sync_qbo_source(
                db,
                config,
                db.get(Source, connected),
                mode="full",
                transport=httpx.MockTransport(handler),
            )
        assert db.scalar(select(Party)) is None
        source = db.get(Source, connected)
        assert source.cursor is None and source.last_synced_at is None
        assert (
            load_source_credentials(source, config)["refresh_token"] == "accepted-race-rotation"
        ) == (race != "generation")


def test_401_refresh_succeeds_once_then_uses_new_bearer_before_atomic_apply(
    client, admin, connected
):
    factory, config = client.app.state.session_factory, client.app.state.config
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(401)
        if request.method == "POST":
            return httpx.Response(
                200,
                json=fixture("tokens")
                | {"access_token": "new-401-access", "refresh_token": "new-401-refresh"},
            )
        assert request.headers["authorization"] == "Bearer new-401-access"
        entity = "Vendor" if "Vendor" in request.url.params["query"] else "Customer"
        return httpx.Response(200, json=query(entity, [fixture(entity.lower())]))

    with factory() as db:
        result = sync_qbo_source(
            db,
            config,
            db.get(Source, connected),
            mode="full",
            transport=httpx.MockTransport(handler),
        )
        assert result["added"] == 2 and result["record_count"] == 2
        assert (
            load_source_credentials(db.get(Source, connected), config)["refresh_token"]
            == "new-401-refresh"
        )
    assert len(calls) == 4 and sum(request.method == "POST" for request in calls) == 1


@pytest.mark.parametrize("race", ["app_generation", "realm", "fence"])
def test_late_app_realm_or_fence_changes_block_all_party_and_cursor_mutations(
    client, admin, connected, race
):
    from app.scheduler.leases import OperationLeaseLostError

    factory, config = client.app.state.session_factory, client.app.state.config
    changed = []

    def handler(request):
        if not changed:
            with factory() as other:
                if race == "app_generation":
                    save_app_config(
                        other,
                        config,
                        AppConfig(
                            "sandbox", APP.client_id, "replacement-fixture-secret", generation=2
                        ),
                    )
                elif race == "realm":
                    source = other.get(Source, connected)
                    source.config = {**source.config, "realm_id": "99999"}
                other.commit()
            changed.append(True)
        return httpx.Response(
            200,
            json=query(
                "Vendor" if "Vendor" in request.url.params["query"] else "Customer",
                [
                    fixture("vendor")
                    if "Vendor" in request.url.params["query"]
                    else fixture("customer")
                ],
            ),
        )

    class LostExecution:
        def check_fence(self, db):
            raise OperationLeaseLostError("Operation ownership changed")

    with factory() as db:
        if race == "fence":
            db.info["operation_execution"] = LostExecution()
        with pytest.raises((QBOError, OperationLeaseLostError)):
            sync_qbo_source(
                db,
                config,
                db.get(Source, connected),
                mode="full",
                transport=httpx.MockTransport(handler),
            )
        assert db.scalar(select(Party)) is None and db.scalar(select(ScreeningTask)) is None
        source = db.get(Source, connected)
        assert source.cursor is None and source.last_synced_at is None


def test_actual_full_cdc_edit_inactive_reactivation_scoped_screening_and_unchanged_no_write(
    client, admin, connected
):
    factory, config = client.app.state.session_factory, client.app.state.config
    with factory() as db:
        refresh_csl(db, file=FIXTURES / "csl_sample_v1.json")
        entries = json.loads((FIXTURES / "csl_sample_v1.json").read_text())
        name = entries[0]["name"]
    record = {
        "Id": "42",
        "Active": True,
        "CompanyName": name,
        "DisplayName": name,
        "BillAddr": {"Country": "US"},
        "PrimaryPhone": {"FreeFormNumber": "555-0100"},
    }
    state = {"full": True, "record": record}

    def handler(request):
        if request.url.path.endswith("cdc"):
            return httpx.Response(200, json=cdc(customer=[state["record"]]))
        return httpx.Response(
            200,
            json=query("Vendor", [])
            if "Vendor" in request.url.params["query"]
            else query("Customer", [state["record"]]),
        )

    transport = httpx.MockTransport(handler)
    with factory() as db:
        result = sync_qbo_source(
            db, config, db.get(Source, connected), mode="full", transport=transport
        )
        assert result["added"] == 1 and result["pending_task_id"]
        first_runs = process_pending_tasks(db)
        party = db.scalar(select(Party))
        original_raw, original_hash = deepcopy(party.raw), party.record_hash
        assert first_runs and any(run.parties_screened == 1 for run in first_runs)
        assert queue_hits(db)
        selected = queue_hits(db)[0]
        decided = client.post(
            f"/api/v1/hits/{selected.id}/dispositions",
            headers={"x-csrf-token": admin["csrf_token"]},
            json={"kind": "false_positive", "note": "Synthetic QBO human memory proof"},
        )
        assert decided.status_code == 201
        human_ids = set(db.scalars(select(Disposition.id)))
        state["record"] = record | {"PrimaryPhone": {"FreeFormNumber": "555-0101"}}
        unchanged = sync_qbo_source(db, config, db.get(Source, connected), transport=transport)
        assert unchanged["unchanged"] == 1 and not unchanged["task_ids"]
        db.refresh(party)
        assert party.raw == original_raw and party.record_hash == original_hash
        repeated = run_screening(db, party_ids=[party.id])
        assert repeated.hits_suppressed >= 1
        assert set(db.scalars(select(Disposition.id))) == human_ids
        state["record"] = record | {"BillAddr": {"Country": "US", "City": "New fixture city"}}
        changed = sync_qbo_source(db, config, db.get(Source, connected), transport=transport)
        assert changed["changed"] == 1
        task = db.get(ScreeningTask, changed["pending_task_id"])
        assert task.party_ids == [party.id] and task.entry_ids == []
        changed_runs = process_pending_tasks(db)
        reopened = list(
            db.scalars(select(Hit).where(Hit.run_id.in_([run.id for run in changed_runs])))
        )
        assert any(hit.explanation.get("prior_disposition_id") in human_ids for hit in reopened)
        state["record"] = {"Id": "42", "status": "Deleted"}
        deleted = sync_qbo_source(db, config, db.get(Source, connected), transport=transport)
        assert deleted["deleted"] == 1 and not deleted["task_ids"]
        db.refresh(party)
        assert party.deleted_at is not None and party.name == name
        state["record"] = record
        restored = sync_qbo_source(db, config, db.get(Source, connected), transport=transport)
        assert restored["changed"] == 1 and restored["pending_task_id"]
        db.refresh(party)
        assert party.deleted_at is None
        assert all(hit.party_snapshot["name"] == name for hit in db.scalars(select(Hit)))
