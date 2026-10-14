"""Subscription custody, real anonymous handshake and durable BC notifications."""

import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_bc_auth import (
    COMPANY,
    RECORD,
    SECRET,
    connect,
    retry,
    subscriptions,
    write,
)
from test_bc_auth import bc_setup as bc_setup

from app.connectors.bc_common import load_source_credentials
from app.models import (
    AuditLog,
    Disposition,
    Hit,
    OperationAttempt,
    Party,
    RunPartyEvidence,
    Source,
    Suppression,
    User,
    utcnow,
)
from app.scheduler.service import dispatch_attempt


def connection(client, source_id):
    response = client.get(f"/api/v1/sources/{source_id}/bc/connection")
    assert response.status_code == 200
    return response.json()


def private(client, source_id):
    with client.app.state.session_factory() as db:
        return load_source_credentials(db.get(Source, source_id), client.app.state.config)


def notification(row, *, change="updated", state=None, resource=None):
    return {
        "subscriptionId": row["subscription_id"],
        "clientState": state if state is not None else row["client_state"],
        "expirationDateTime": row["expiration_date_time"],
        "resource": resource or f"api/v2.0/companies({COMPANY})/{row['resource']}({RECORD})",
        "changeType": change,
        "lastModifiedDateTime": utcnow().isoformat(),
    }


def test_registers_two_resources_and_actual_anonymous_handshake(client, admin, bc_setup):
    source_id, vendor = bc_setup
    connect(client, admin, source_id)
    result = subscriptions(client, admin, source_id)
    assert result["status"] == "success"
    assert vendor.challenge_count == 2 and vendor.calls[("POST", "subscriptions")] == 2
    public = connection(client, source_id)
    assert public["subscription_status"] == "active" and len(public["subscriptions"]) == 2
    state = private(client, source_id)
    assert not state["pending_cleanup"]
    with client.app.state.session_factory() as db:
        source = db.get(Source, source_id)
        audit_json = json.dumps(
            [(row.before, row.after) for row in db.scalars(select(AuditLog))],
            default=str,
        )
        for row in state["subscriptions"]:
            assert row["client_state"] not in source.credentials_enc
            assert row["client_state"] not in audit_json and row["client_state"] not in json.dumps(
                public
            )
        assert SECRET not in audit_json


def test_accepted_201_keeps_identity_when_immediate_list_is_empty(client, admin, bc_setup):
    source_id, vendor = bc_setup
    connect(client, admin, source_id)
    vendor.hidden = True
    result = subscriptions(client, admin, source_id)
    assert result["status"] == "success"
    state = private(client, source_id)
    assert len(state["subscriptions"]) == 2 and not state["pending_cleanup"]
    assert {row["subscription_id"] for row in state["subscriptions"]} == set(vendor.rows)
    vendor.hidden = False
    assert retry(client, admin, source_id)["status"] == "success"
    assert vendor.calls[("POST", "subscriptions")] == 2


def test_accepted_id_without_etag_stays_private_until_trusted_visibility(client, admin, bc_setup):
    source_id, vendor = bc_setup
    connect(client, admin, source_id)
    vendor.no_etag = True
    vendor.hidden = True
    failed = subscriptions(client, admin, source_id)
    assert failed["status"] == "failed"
    backlog = private(client, source_id)["pending_cleanup"]
    assert len(backlog) == 2 and all(row["subscription_id"] in vendor.rows for row in backlog)
    vendor.hidden = False
    vendor.no_etag = False
    for row in vendor.rows.values():
        row["@odata.etag"] = 'W/"became-visible"'
    assert retry(client, admin, source_id)["status"] == "success"
    assert vendor.calls[("POST", "subscriptions")] == 2
    assert not private(client, source_id)["pending_cleanup"]


def test_ambiguous_create_is_adopted_without_a_duplicate_post(client, admin, bc_setup):
    source_id, vendor = bc_setup
    connect(client, admin, source_id)
    vendor.timeout_create = True
    vendor.hidden_entities.add("customers")
    failed = subscriptions(client, admin, source_id)
    assert failed["status"] == "failed"
    backlog = private(client, source_id)["pending_cleanup"]
    assert len(backlog) == 1 and backlog[0]["subscription_id"] is None
    assert retry(client, admin, source_id)["status"] == "failed"
    assert vendor.creations["customers"] == 1 and vendor.creations["vendors"] == 1
    vendor.hidden_entities.clear()
    assert retry(client, admin, source_id)["status"] == "success"
    assert vendor.creations["customers"] == 1 and vendor.creations["vendors"] == 1
    assert connection(client, source_id)["subscription_status"] == "active"


def test_renew_conflict_refreshes_etag_and_retries_patch_once(client, admin, bc_setup):
    source_id, vendor = bc_setup
    connect(client, admin, source_id)
    assert subscriptions(client, admin, source_id)["status"] == "success"
    for row in vendor.rows.values():
        row["expirationDateTime"] = (utcnow() + timedelta(hours=1)).isoformat()
    vendor.conflict_renew = True
    assert retry(client, admin, source_id)["status"] == "success"
    assert vendor.calls[("PATCH", "subscriptions")] == 3
    assert vendor.calls[("POST", "subscriptions")] == 2
    assert vendor.calls[("DELETE", "subscriptions")] == 0
    assert vendor.challenge_count == 4


def test_missing_subscription_recreated_after_trusted_list(client, admin, bc_setup):
    source_id, vendor = bc_setup
    connect(client, admin, source_id)
    assert subscriptions(client, admin, source_id)["status"] == "success"
    removed = next(iter(vendor.rows))
    del vendor.rows[removed]
    assert retry(client, admin, source_id)["status"] == "success"
    assert vendor.calls[("POST", "subscriptions")] == 3
    assert removed not in {
        row["subscription_id"] for row in private(client, source_id)["subscriptions"]
    }


def test_late_offboard_retains_known_accepted_cleanup_without_registration(client, admin, bc_setup):
    source_id, vendor = bc_setup
    connect(client, admin, source_id)
    vendor.reject_delete = True

    def offboard():
        with client.app.state.session_factory() as db:
            db.get(User, admin["user"]["id"]).role = "reviewer"
            db.commit()

    vendor.on_post = offboard
    result = subscriptions(client, admin, source_id)
    assert result["status"] == "failed"
    state = private(client, source_id)
    assert not state.get("subscriptions")
    assert state["pending_cleanup"][0]["subscription_id"] in vendor.rows
    with client.app.state.session_factory() as db:
        accepted = db.scalar(select(AuditLog).where(AuditLog.action == "bc.subscription_accepted"))
        assert accepted.actor_user_id is None
        assert accepted.after["subscription_id"] in vendor.rows
        assert db.get(Source, source_id).cursor is None


def test_disable_cleanup_preserves_connected_access_token_and_retries(client, admin, bc_setup):
    source_id, vendor = bc_setup
    connect(client, admin, source_id)
    assert subscriptions(client, admin, source_id)["status"] == "success"
    old_access = private(client, source_id)["access_token"]
    vendor.reject_delete = True
    queued = write(
        client, admin, "patch", f"/sources/{source_id}/bc/connection", {"webhooks_enabled": False}
    )
    result = dispatch_attempt(
        client.app.state.session_factory, client.app.state.config, queued.json()["attempt_id"]
    )
    assert result["status"] == "failed"
    public = connection(client, source_id)
    assert public["status"] == "connected" and public["cleanup_status"] == "failed"
    vendor.reject_delete = False
    assert retry(client, admin, source_id)["status"] == "success"
    assert private(client, source_id)["access_token"] == old_access
    assert connection(client, source_id)["status"] == "connected" and not vendor.rows


def test_disconnect_clears_tokens_only_after_remote_cleanup(client, admin, bc_setup):
    from app.csl.service import refresh_csl
    from app.screening.engine import run_screening
    from app.screening.normalize import party_record_hash

    source_id, vendor = bc_setup
    connect(client, admin, source_id)
    assert subscriptions(client, admin, source_id)["status"] == "success"
    factory = client.app.state.session_factory
    with factory() as db:
        refresh_csl(
            db,
            data=json.dumps(
                [
                    {
                        "id": "historical-bc-fixture",
                        "source": "Entity List (EL) - Bureau of Industry and Security",
                        "name": "Historical Synthetic Party",
                        "type": "Entity",
                        "addresses": [{"country": "US"}],
                    }
                ]
            ).encode(),
        )
        party = Party(
            source_id=source_id,
            external_id="Customer:" + RECORD,
            name="Historical Synthetic Party",
            party_type="company",
            country_iso2="US",
            raw={"id": RECORD, "displayName": "Historical Synthetic Party"},
            record_hash=party_record_hash(
                {
                    "name": "Historical Synthetic Party",
                    "party_type": "company",
                    "country_iso2": "US",
                }
            ),
        )
        db.add(party)
        db.commit()
        run = run_screening(db, party_ids=[party.id])
        hit = db.scalar(select(Hit).where(Hit.run_id == run.id))
        assert hit is not None
        hit_id = hit.id
    decided = write(
        client,
        admin,
        "post",
        f"/hits/{hit_id}/dispositions",
        {
            "kind": "false_positive",
            "note": "Preserve the original human decision.",
        },
    )
    assert decided.status_code == 201

    def snapshots():
        with factory() as db:
            return {
                model.__tablename__: [
                    {column.name: getattr(row, column.name) for column in model.__table__.columns}
                    for row in db.scalars(select(model).order_by(model.id))
                ]
                for model in (Party, Hit, RunPartyEvidence, Disposition, Suppression)
            }

    original = snapshots()
    assert len(original["run_party_evidence"]) == 1
    assert len(original["dispositions"]) == 1
    assert len(original["suppressions"]) == 1
    vendor.reject_delete = True
    queued = write(client, admin, "post", f"/sources/{source_id}/bc/disconnect")
    failed = dispatch_attempt(factory, client.app.state.config, queued.json()["attempt_id"])
    assert failed["status"] == "failed"
    assert private(client, source_id)["pending_cleanup"]
    failed_connection = connection(client, source_id)
    # This is the actual UI retry predicate: local disconnect completed despite
    # remote DELETE failure, and private remote cleanup remains retryable.
    assert failed_connection["status"] == "disconnected"
    assert failed_connection["cleanup_status"] == "failed"
    assert not failed_connection["webhooks_enabled"]
    assert failed_connection["access_token_configured"]
    assert snapshots() == original
    vendor.reject_delete = False
    queued = write(client, admin, "post", f"/sources/{source_id}/bc/disconnect")
    assert (
        dispatch_attempt(factory, client.app.state.config, queued.json()["attempt_id"])["status"]
        == "success"
    )
    assert not private(client, source_id)
    public = connection(client, source_id)
    assert public["status"] == "disconnected" and public["cleanup_status"] == "completed"
    assert snapshots() == original


def test_secret_verified_receipts_are_atomic_duplicate_safe_and_full_for_deletion(
    client, admin, bc_setup
):
    source_id, _ = bc_setup
    connect(client, admin, source_id)
    assert subscriptions(client, admin, source_id)["status"] == "success"
    row = private(client, source_id)["subscriptions"][0]
    event = notification(row, change="deleted")
    path = f"/api/v1/webhooks/bc/{source_id}"
    for _ in range(2):
        assert client.post(path, json={"value": [event]}, headers={"cookie": ""}).status_code == 200
    with client.app.state.session_factory() as db:
        receipts = list(
            db.scalars(select(AuditLog).where(AuditLog.action == "bc.webhook_received"))
        )
        assert len(receipts) == 1
        attempt = db.get(OperationAttempt, receipts[0].after["attempt_id"])
        assert attempt.parameters["mode"] == "full" and attempt.actor_user_id is None
    valid = notification(row)
    forged = notification(row, state="incorrect-secret")
    response = client.post(path, json={"value": [valid, forged]})
    assert response.status_code == 400 and "incorrect-secret" not in response.text
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "bc.webhook_received")
            )
            == 1
        )


@pytest.mark.parametrize(
    "query",
    [
        "validationToken=first&validationToken=second",
        "validationToken=&unknown=secret",
        "unexpected=must-not-echo",
        "validationToken=%00",
    ],
)
def test_anonymous_challenge_rejects_bad_query_without_echo(client, admin, bc_setup, query):
    source_id, _ = bc_setup
    response = client.post(f"/api/v1/webhooks/bc/{source_id}?{query}", headers={"cookie": ""})
    assert response.status_code == 400
    assert "must-not-echo" not in response.text
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"


def test_no_etag_obligation_expires_proven_absent_without_blocking_peer_lifecycle(
    client, admin, bc_setup, monkeypatch
):
    import test_bc_auth as fixture

    import app.connectors.bc_auth as auth
    import app.connectors.bc_common as common
    import app.connectors.bc_subscriptions as lifecycle

    source_id, vendor = bc_setup
    connect(client, admin, source_id)
    vendor.no_etag_entities.add("customers")
    vendor.hidden_entities.add("customers")
    assert subscriptions(client, admin, source_id)["status"] == "failed"
    state = private(client, source_id)
    obligation = state["pending_cleanup"][0]
    assert obligation["subscription_id"] in vendor.rows
    assert [row["resource"] for row in state["subscriptions"]] == ["vendors"]
    vendor_id = state["subscriptions"][0]["subscription_id"]
    vendor.rows[vendor_id]["expirationDateTime"] = (utcnow() + timedelta(hours=1)).isoformat()
    assert retry(client, admin, source_id)["status"] == "failed"
    assert vendor.calls[("PATCH", "subscriptions")] == 1
    # Blocked customer visibility cannot prevent deletion of the vendor resource.
    queued = write(client, admin, "post", f"/sources/{source_id}/bc/disconnect")
    assert (
        dispatch_attempt(
            client.app.state.session_factory,
            client.app.state.config,
            queued.json()["attempt_id"],
        )["status"]
        == "failed"
    )
    assert vendor_id not in vendor.rows
    assert (
        private(client, source_id)["pending_cleanup"][0]["subscription_id"]
        == obligation["subscription_id"]
    )
    assert connection(client, source_id)["status"] == "disconnected"
    future = common.utc_time(obligation["expiration_date_time"]) + timedelta(minutes=6)
    for module in (auth, common, lifecycle, fixture):
        monkeypatch.setattr(module, "utcnow", lambda: future)
    del vendor.rows[obligation["subscription_id"]]
    assert retry(client, admin, source_id)["status"] == "success"
    assert not private(client, source_id)
    with client.app.state.session_factory() as db:
        expired = db.scalar(select(AuditLog).where(AuditLog.action == "bc.subscription_expired"))
        assert expired.after["subscription_id"] == obligation["subscription_id"]
    assert vendor.creations["customers"] == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_definite_create_rejection_removes_intent_and_peer_can_renew(
    client, admin, bc_setup, status
):
    source_id, vendor = bc_setup
    connect(client, admin, source_id)
    vendor.reject_create["customers"] = status
    result = subscriptions(client, admin, source_id)
    assert result["status"] == "failed"
    state = private(client, source_id)
    assert not state["pending_cleanup"]
    assert [row["resource"] for row in state["subscriptions"]] == ["vendors"]
    vendor_id = state["subscriptions"][0]["subscription_id"]
    vendor.rows[vendor_id]["expirationDateTime"] = (utcnow() + timedelta(hours=1)).isoformat()
    assert retry(client, admin, source_id)["status"] == "failed"
    assert vendor.calls[("PATCH", "subscriptions")] == 1
    assert not private(client, source_id)["pending_cleanup"]
    del vendor.reject_create["customers"]
    assert retry(client, admin, source_id)["status"] == "success"
    assert vendor.creations["vendors"] == 1
    with client.app.state.session_factory() as db:
        rejected = list(
            db.scalars(select(AuditLog).where(AuditLog.action == "bc.subscription_rejected"))
        )
        assert rejected and all(row.after["http_status"] == status for row in rejected)


@pytest.mark.parametrize("status", [408, 429, 500])
def test_retryable_create_error_keeps_only_its_unknown_intent(client, admin, bc_setup, status):
    source_id, vendor = bc_setup
    connect(client, admin, source_id)
    vendor.reject_create["customers"] = status
    assert subscriptions(client, admin, source_id)["status"] == "failed"
    state = private(client, source_id)
    assert len(state["pending_cleanup"]) == 1
    assert state["pending_cleanup"][0]["subscription_id"] is None
    assert [row["resource"] for row in state["subscriptions"]] == ["vendors"]
    assert retry(client, admin, source_id)["status"] == "failed"
    assert vendor.creations["customers"] == 1 and vendor.creations["vendors"] == 1


def test_changed_public_origin_is_degraded_then_cleans_and_rebinds_both_resources(
    client, admin, bc_setup
):
    source_id, vendor = bc_setup
    connect(client, admin, source_id)
    assert subscriptions(client, admin, source_id)["status"] == "success"
    previous_ids = set(vendor.rows)
    assert (
        write(
            client,
            admin,
            "patch",
            "/settings/notifications",
            {
                "public_base_url": "https://new-screening.example.test",
            },
        ).status_code
        == 200
    )
    public = connection(client, source_id)
    assert public["subscription_status"] == "degraded"
    assert all(
        row["last_error_code"] == "bc_webhook_url_changed" for row in public["subscriptions"]
    )
    assert retry(client, admin, source_id)["status"] == "success"
    assert not previous_ids & set(vendor.rows)
    assert all(
        row["notificationUrl"].startswith("https://new-screening.example.test/")
        for row in vendor.rows.values()
    )
    assert connection(client, source_id)["subscription_status"] == "active"
    assert vendor.calls[("DELETE", "subscriptions")] == 2


@pytest.mark.parametrize("invalid", ["source", "state", "resource", "batch_size"])
def test_invalid_anonymous_notifications_never_acquire_writer(
    client, admin, bc_setup, monkeypatch, invalid
):
    import app.connectors.bc_subscriptions as lifecycle

    source_id, _ = bc_setup
    connect(client, admin, source_id)
    assert subscriptions(client, admin, source_id)["status"] == "success"
    event = notification(private(client, source_id)["subscriptions"][0])
    if invalid == "source":
        source_id = "00000000-0000-4000-8000-000000000000"
    elif invalid == "state":
        event["clientState"] = "invalid-private-state"
    elif invalid == "resource":
        event["resource"] = "https://untrusted.example.test/secret"
    events = [event] * (1001 if invalid == "batch_size" else 1)
    writers = []

    def forbidden(db):
        writers.append(True)
        raise AssertionError("Unauthenticated input acquired a writer")

    monkeypatch.setattr(lifecycle, "begin", forbidden)
    response = client.post(
        f"/api/v1/webhooks/bc/{source_id}", json={"value": events}, headers={"cookie": ""}
    )
    assert response.status_code == 400 and not writers
    assert (
        "invalid-private-state" not in response.text
        and "untrusted.example.test" not in response.text
    )


def test_valid_receipt_rechecks_secret_state_under_writer_and_rolls_back(
    client, admin, bc_setup, monkeypatch
):
    import app.connectors.bc_subscriptions as lifecycle
    from app.connectors.bc_common import save_source_credentials

    source_id, _ = bc_setup
    connect(client, admin, source_id)
    assert subscriptions(client, admin, source_id)["status"] == "success"
    event = notification(private(client, source_id)["subscriptions"][0])
    original = lifecycle.begin

    def rotate_before_writer(db):
        with client.app.state.session_factory() as another:
            source = another.get(Source, source_id)
            values = load_source_credentials(source, client.app.state.config)
            values["subscriptions"][0]["client_state"] = "new-private-subscription-state"
            save_source_credentials(another, source, client.app.state.config, values)
            another.commit()
        original(db)

    monkeypatch.setattr(lifecycle, "begin", rotate_before_writer)
    response = client.post(f"/api/v1/webhooks/bc/{source_id}", json={"value": [event]})
    assert response.status_code == 503
    with client.app.state.session_factory() as db:
        assert not db.scalar(select(AuditLog.id).where(AuditLog.action == "bc.webhook_received"))
        assert not db.scalar(
            select(OperationAttempt.id).where(
                OperationAttempt.operation == "source_sync",
                OperationAttempt.actor_user_id.is_(None),
            )
        )


def test_valid_receipt_store_failure_rolls_back_followup_and_returns_retryable_status(
    client, admin, bc_setup, monkeypatch
):
    import app.connectors.bc_subscriptions as lifecycle

    source_id, _ = bc_setup
    connect(client, admin, source_id)
    assert subscriptions(client, admin, source_id)["status"] == "success"
    event = notification(private(client, source_id)["subscriptions"][0])
    original = lifecycle.audit

    def fail_receipt(db, action, *args, **kwargs):
        if action == "bc.webhook_received":
            raise RuntimeError("synthetic private store failure")
        return original(db, action, *args, **kwargs)

    monkeypatch.setattr(lifecycle, "audit", fail_receipt)
    response = client.post(f"/api/v1/webhooks/bc/{source_id}", json={"value": [event]})
    assert response.status_code == 503 and "private store failure" not in response.text
    with client.app.state.session_factory() as db:
        assert not db.scalar(select(AuditLog.id).where(AuditLog.action == "bc.webhook_received"))
        assert not db.scalar(
            select(OperationAttempt.id).where(
                OperationAttempt.operation == "source_sync",
                OperationAttempt.actor_user_id.is_(None),
            )
        )


def test_subscription_get_releases_origin_read_transaction_before_http(
    client, admin, bc_setup, monkeypatch
):
    import app.connectors.bc as core
    from app.connectors.bc_subscriptions import reconcile_bc_subscriptions
    from app.scheduler.leases import operation_session, protected_operation

    source_id, _ = bc_setup
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
    factory = client.app.state.session_factory
    original = core.BCClient
    observed = []
    with protected_operation(
        factory, "bc_subscriptions", source_id=source_id, actor_user_id=admin["user"]["id"]
    ) as execution:
        with operation_session(factory, execution, admin["user"]["id"]) as db:

            class CheckingClient(original):
                def subscription_request(self, method, *args, **kwargs):
                    if method == "GET":
                        observed.append(db.in_transaction())
                        assert not db.in_transaction()
                    return super().subscription_request(method, *args, **kwargs)

            monkeypatch.setattr(core, "BCClient", CheckingClient)
            result = reconcile_bc_subscriptions(
                db,
                client.app.state.config,
                db.get(Source, source_id),
                transport=client.app.state.bc_transport,
            )
    assert result["error_code"] is None and len(observed) >= 3
    assert not any(observed)


@pytest.mark.parametrize("blocked", [False, True])
def test_system_reconcile_drains_full_backlog_without_losing_retained_subscriptions(
    client, admin, bc_setup, blocked
):
    from app.connectors.bc_common import load_bc_config, save_source_credentials
    from app.connectors.bc_subscriptions import cleanup_obligation
    from app.scheduler.runtime import SchedulerRuntime
    from app.scheduler.service import enqueue_bc_subscriptions

    source_id, vendor = bc_setup
    connect(client, admin, source_id)
    assert subscriptions(client, admin, source_id)["status"] == "success"
    factory, config = client.app.state.session_factory, client.app.state.config
    with factory() as db:
        source = db.get(Source, source_id)
        snapshot = load_bc_config(db, config, source)
        values = load_source_credentials(source, config)
        original = values["subscriptions"]
        prototype = original[0]
        backlog = []
        for index in range(32):
            subscription = {
                **prototype,
                "subscription_id": None if blocked else f"legacy-subscription-{index}",
                "client_state": f"synthetic-private-legacy-state-{index}",
                "etag": None if blocked else f'W/"legacy-{index}"',
                "requested_at": utcnow().isoformat(),
            }
            backlog.append(cleanup_obligation(snapshot, values["access_token"], subscription))
            if not blocked:
                vendor.rows[subscription["subscription_id"]] = {
                    "subscriptionId": subscription["subscription_id"],
                    "notificationUrl": subscription["notification_url"],
                    "resource": subscription["resource_path"],
                    "clientState": subscription["client_state"],
                    "expirationDateTime": subscription["expiration_date_time"],
                    "@odata.etag": subscription["etag"],
                }
        values["pending_cleanup"] = backlog
        source.config = {
            **source.config,
            "connection_status": "disconnected",
            "webhooks_enabled": False,
            "cleanup_status": "pending",
        }
        save_source_credentials(db, source, config, values)
        attempt = enqueue_bc_subscriptions(db, source_id, actor_user_id=None)
        db.commit()
        attempt_id = attempt.id
    result = dispatch_attempt(factory, config, attempt_id)
    public = connection(client, source_id)
    assert public["status"] == "disconnected"
    if blocked:
        assert result["status"] == "failed"
        remaining = private(client, source_id)
        assert len(remaining["pending_cleanup"]) == 32
        assert remaining["subscriptions"] == original
        assert public["cleanup_status"] == "failed"
        assert len(vendor.rows) == 2
        from types import SimpleNamespace

        jobs = set()

        class RecordingScheduler:
            def pause(self):
                pass

            def resume(self):
                pass

            def get_jobs(self):
                return []

            def get_job(self, job_id):
                return None

            def add_job(self, command, **kwargs):
                jobs.add(kwargs["id"])

        runtime = SimpleNamespace(factory=factory, config=config, scheduler=RecordingScheduler())
        # Exercise actual reconciliation eligibility without starting a worker
        # or allowing scheduled vendor/CSL network traffic in this test.
        SchedulerRuntime.reconcile(runtime)
        assert "bc-subscriptions-" + source_id in jobs
    else:
        assert result["status"] == "success"
        assert not private(client, source_id) and not vendor.rows
        assert vendor.calls[("DELETE", "subscriptions")] == 34
        assert public["cleanup_status"] == "completed"
