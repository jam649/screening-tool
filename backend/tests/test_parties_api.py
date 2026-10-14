from datetime import timedelta

from sqlalchemy import select
from test_review_api import decide
from test_review_api import review_seed as review_seed  # explicitly re-export pytest fixture

from app.models import Party
from app.screening.engine import run_screening
from app.screening.normalize import party_record_hash


def test_party_file_zero_hit_membership_and_later_party(client, admin, review_seed):
    file = client.get(f"/api/v1/parties/{review_seed['zero_id']}").json()
    assert len(file["events"]) == 2
    assert all(event["zero_hits"] for event in file["events"])
    with client.app.state.session_factory() as db:
        party = Party(
            source_id=review_seed["source_id"],
            external_id="later",
            name="Later Party",
            record_hash=party_record_hash({"name": "Later Party"}),
        )
        db.add(party)
        db.commit()
        later_id = party.id
    assert client.get(f"/api/v1/parties/{later_id}").json()["events"] == []
    data = client.get("/api/v1/dashboard").json()
    assert data["open_items"] == 1 and data["likely_matches"] == 1
    assert data["total_parties"] == 3 and data["stale_parties"] == 1
    assert data["parties_screened"] == 2


def test_party_filters_and_suppressed_groups(client, admin, review_seed):
    result = client.get("/api/v1/parties", params={"q": "Acme", "country": "us"}).json()
    assert result["total"] == 1 and result["items"][0]["status"] == "open_hit"
    assert client.get("/api/v1/parties", params={"role": "vendor"}).json()["total"] == 1
    assert client.get("/api/v1/parties", params={"source_id": "missing"}).json()["total"] == 0
    assert decide(client, admin, review_seed["hit_id"]).status_code == 201
    with client.app.state.session_factory() as db:
        run_screening(db)
    file = client.get(f"/api/v1/parties/{review_seed['party_id']}").json()
    assert file["party"]["status"] == "suppressed"
    assert file["events"][0]["previously_cleared"]["count"] == 1
    assert not file["events"][0]["hits"]
    remembered = file["events"][0]["previously_cleared"]["hits"][0]["suppression_disposition"]
    assert remembered["kind"] == "false_positive"
    assert remembered["actor_user_id"] == admin["user"]["id"]
    # A suppression links the original human decision; it adds no fake new decision.
    assert file["events"][0]["previously_cleared"]["hits"][0]["dispositions"] == []
    assert any(row["action"] == "review.disposition" for row in file["audit_log"])
    assert client.get("/api/v1/parties", params={"status": "suppressed"}).json()["total"] == 1


def test_stale_uses_screen_time_and_current_hash(client, admin, review_seed, monkeypatch):
    from app.api import parties as parties_api
    from app.models import utcnow
    from app.reports import metrics

    future = utcnow() + timedelta(days=2)
    monkeypatch.setattr(metrics, "utcnow", lambda: future)
    monkeypatch.setattr(parties_api, "utcnow", lambda: future)
    assert client.get("/api/v1/dashboard").json()["stale_parties"] == 2
    assert client.get("/api/v1/parties", params={"stale": True}).json()["total"] == 2
    monkeypatch.setattr(metrics, "utcnow", utcnow)
    monkeypatch.setattr(parties_api, "utcnow", utcnow)
    with client.app.state.session_factory() as db:
        run_screening(db)
        record = db.get(Party, review_seed["zero_id"])
        record.city = "New city"
        record.record_hash = party_record_hash(
            {"name": record.name, "country_iso2": "CA", "city": "New city"}
        )
        db.commit()
    assert client.get("/api/v1/dashboard").json()["stale_parties"] == 1


def test_runs_and_audit_require_auth_and_include_decision_actor(client, admin, review_seed):
    assert (
        decide(client, admin, review_seed["hit_id"], "escalated", "Investigate").status_code == 201
    )
    audit = client.get("/api/v1/audit-log", params={"action": "review.disposition"}).json()
    assert audit["total"] == 2
    assert all(row["actor_user_id"] == admin["user"]["id"] for row in audit["items"])
    assert client.get("/api/v1/runs").json()["total"] == 2
    client.cookies.clear()
    for path in ("queue", "parties", "runs", "audit-log", "dashboard"):
        assert client.get(f"/api/v1/{path}").status_code == 401


def test_auto_clear_counts_and_filters_use_last_successful_report(client, admin, review_seed):
    from app.models import Disposition, Hit, Setting, utcnow

    with client.app.state.session_factory() as db:
        observations = list(db.scalars(select(Hit)))
        for hit in observations:
            db.add(
                Disposition(
                    hit_id=hit.id,
                    kind="auto_cleared",
                    actor_user_id=None,
                    note="Synthetic system auto-clear",
                    slider_value_at_time=90,
                )
            )
        db.flush()
        for hit in observations:
            hit.status = "dispositioned"
        db.commit()
    assert client.get("/api/v1/dashboard").json()["auto_cleared_since_report"] == 2
    assert client.get("/api/v1/parties", params={"auto_cleared": True}).json()["total"] == 1
    with client.app.state.session_factory() as db:
        db.add(Setting(key="last_report_at", value=utcnow().isoformat()))
        db.commit()
    assert client.get("/api/v1/dashboard").json()["auto_cleared_since_report"] == 0
    assert client.get("/api/v1/parties", params={"auto_cleared": True}).json()["total"] == 0


def test_delta_run_cannot_claim_new_party_full_list_coverage(client, admin, review_seed):
    import json

    from app.csl.service import refresh_csl
    from app.models import CSLEntry

    with client.app.state.session_factory() as db:
        rows = [entry.raw for entry in db.scalars(select(CSLEntry))]
        rows.append(
            {
                "id": "second",
                "source": "EL",
                "name": "Unrelated Listed Enterprise",
                "type": "Entity",
                "alt_names": [],
                "addresses": [],
                "programs": [],
            }
        )
        refresh_csl(db, data=json.dumps(rows).encode())
        second_entry = db.scalar(select(CSLEntry).where(CSLEntry.csl_id == "second"))
        later = Party(
            source_id=review_seed["source_id"],
            external_id="new-party",
            name="New Unlisted Party",
            record_hash=party_record_hash({"name": "New Unlisted Party"}),
        )
        db.add(later)
        db.commit()
        later_id = later.id
        run_screening(db, trigger="list_change", entry_ids=[second_entry.id])
    dashboard = client.get("/api/v1/dashboard").json()
    assert dashboard["parties_screened"] == 2 and dashboard["total_parties"] == 3
    row = client.get(f"/api/v1/parties/{later_id}").json()["party"]
    assert row["screened_current"] is False and row["status"] == "unscreened"
    assert row["last_screened_at"] is not None  # actual delta participation is recorded
    assert client.get("/api/v1/parties", params={"status": "unscreened"}).json()["total"] == 1
    with client.app.state.session_factory() as db:
        run_screening(db, trigger="party_change", party_ids=[later_id])
    assert client.get("/api/v1/dashboard").json()["parties_screened"] == 3
    assert client.get(f"/api/v1/parties/{later_id}").json()["party"]["screened_current"] is True


def test_metadata_only_version_keeps_proven_matching_coverage(client, admin, review_seed):
    import json

    from app.csl.service import refresh_csl
    from app.models import CSLEntry

    with client.app.state.session_factory() as db:
        row = db.scalar(select(CSLEntry)).raw
        result = refresh_csl(
            db, data=json.dumps([row | {"remarks": "Changed legal context"}]).encode()
        )
        assert result.version.changed == 0 and result.pending_task_id is None
        latest_id = result.version.id
    dashboard = client.get("/api/v1/dashboard").json()
    assert dashboard["csl_version"]["id"] == latest_id
    assert dashboard["parties_screened"] == 2
    assert dashboard["csl_version"]["last_fetched_at"]


def test_escalated_party_never_labeled_no_match(client, admin, review_seed):
    response = decide(client, admin, review_seed["hit_id"], "escalated", "Pending investigation")
    assert response.status_code == 201
    row = client.get(f"/api/v1/parties/{review_seed['party_id']}").json()["party"]
    assert row["status"] == "escalated"
    assert client.get("/api/v1/parties", params={"status": "escalated"}).json()["total"] == 1


def test_scoring_context_change_cannot_claim_unchanged_coverage(client, admin, review_seed):
    response = client.patch(
        "/api/v1/settings",
        json={"candidate_min": 55},
        headers={"x-csrf-token": admin["csrf_token"]},
    )
    assert response.status_code == 200
    assert client.get("/api/v1/dashboard").json()["parties_screened"] == 0
    row = client.get(f"/api/v1/parties/{review_seed['zero_id']}").json()["party"]
    assert row["status"] == "unscreened"
    with client.app.state.session_factory() as db:
        run_screening(db)
    assert client.get("/api/v1/dashboard").json()["parties_screened"] == 2


def test_party_audit_includes_list_fetch_version_and_completed_task(client, admin, review_seed):
    from app.screening.engine import process_pending_tasks

    with client.app.state.session_factory() as db:
        process_pending_tasks(db)
    file = client.get(f"/api/v1/parties/{review_seed['zero_id']}").json()
    actions = {row["action"] for row in file["audit_log"]}
    assert {
        "csl.fetch_started",
        "csl.version_created",
        "screening.enqueued",
        "screening.task_completed",
        "screening.completed",
    } <= actions


def test_run_and_audit_views_project_manifests_without_changing_evidence(
    client, admin, review_seed
):
    from app.models import AuditLog, ScreeningRun

    run = client.get("/api/v1/runs").json()["items"][0]
    assert run["settings_snapshot"]["scope"] == {"party_count": 2, "entry_count": 1}
    assert run["settings_snapshot"]["hash_rule_version"] == 2
    assert run["settings_snapshot"]["score_weights"]["country_match"] == 5
    file = client.get(f"/api/v1/parties/{review_seed['zero_id']}").json()
    scope = file["events"][0]["run"]["settings_snapshot"]["scope"]
    assert scope["party_id"] == review_seed["zero_id"] and scope["party_hash"]
    assert "party_hashes" not in scope and "entry_hashes" not in scope
    audit = client.get("/api/v1/audit-log", params={"action": "screening.completed"}).json()
    assert all(
        row["after"]["settings_snapshot"]["scope"]["party_count"] == 2 for row in audit["items"]
    )
    with client.app.state.session_factory() as db:
        saved = db.get(ScreeningRun, run["id"])
        assert len(saved.settings_snapshot["scope"]["party_ids"]) == 2
        assert len(saved.settings_snapshot["scope"]["entry_hashes"]) == 1
        event = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "screening.completed", AuditLog.target_id == run["id"]
            )
        )
        assert len(event.after["settings_snapshot"]["scope"]["party_ids"]) == 2


def test_removed_entry_closures_are_not_slider_auto_clears(client, admin, review_seed):
    import json

    from app.csl.service import refresh_csl
    from app.models import CSLEntry

    with client.app.state.session_factory() as db:
        row = db.scalar(select(CSLEntry)).raw
        other = {
            "id": "other",
            "source": "EL",
            "name": "Unrelated Listed Enterprise",
            "type": "Entity",
            "alt_names": [],
            "addresses": [],
            "programs": [],
        }
        refresh_csl(db, data=json.dumps([row, other]).encode())
        removed = refresh_csl(db, data=json.dumps([other]).encode())
        assert removed.closed_hits == 2
    dashboard = client.get("/api/v1/dashboard").json()
    assert dashboard["auto_cleared_since_report"] == 0
    assert dashboard["removed_entry_closures_since_report"] == 1
    assert client.get("/api/v1/parties", params={"auto_cleared": True}).json()["total"] == 0


def test_newer_positive_invalidates_old_exact_suppression_display(client, admin, review_seed):
    from app.screening.engine import queue_hits, snapshot

    response = decide(client, admin, review_seed["hit_id"], "false_positive")
    assert response.status_code == 201
    with client.app.state.session_factory() as db:
        party = db.get(Party, review_seed["party_id"])
        party.city = "Boston"
        party.record_hash = party_record_hash(snapshot(party))
        db.commit()
        run_screening(db)
        changed_hit_id = queue_hits(db)[0].id
    response = decide(
        client, admin, changed_hit_id, "confirmed_match", "Actual match on changed input"
    )
    assert response.status_code == 201
    with client.app.state.session_factory() as db:
        party = db.get(Party, review_seed["party_id"])
        party.city = None
        party.record_hash = party_record_hash(snapshot(party))
        db.commit()
    # The book reverted to old hashes; a newer positive decision prevents presenting
    # old remembered false-positive memory as still governing this pair.
    row = client.get(f"/api/v1/parties/{review_seed['party_id']}").json()["party"]
    assert row["suppressed_pairings"] == 0 and row["status"] != "suppressed"
    with client.app.state.session_factory() as db:
        run_screening(db)
    assert (
        client.get(f"/api/v1/parties/{review_seed['party_id']}").json()["party"]["status"]
        == "open_hit"
    )


def test_party_audit_includes_failed_explicit_scoped_work_without_fake_run(
    client, admin, review_seed, monkeypatch
):
    import pytest

    from app.audit import audit
    from app.models import CSLVersion, ScreeningTask
    from app.screening import engine

    with client.app.state.session_factory() as db:
        # Complete the initial full-list task, then request exact party scope.
        engine.process_pending_tasks(db)
        version = db.scalar(select(CSLVersion))
        task = ScreeningTask(
            trigger="party_change", csl_version_id=version.id, party_ids=[review_seed["zero_id"]]
        )
        db.add(task)
        db.flush()
        audit(
            db,
            "screening.enqueued",
            "screening_tasks",
            task.id,
            after={"party_ids": task.party_ids, "trigger": "party_change"},
        )
        db.commit()
        task_id = task.id

        def fail_candidates(*args):
            raise RuntimeError("Injected scoped failure")

        monkeypatch.setattr(engine.CandidateIndex, "candidates", fail_candidates)
        with pytest.raises(engine.ScreeningTasksFailedError) as failure:
            engine.process_pending_tasks(db)
        assert failure.value.failures and not failure.value.completed_runs
    file = client.get(f"/api/v1/parties/{review_seed['zero_id']}").json()
    task_events = [row for row in file["audit_log"] if row["target_id"] == task_id]
    assert {row["action"] for row in task_events} == {"screening.enqueued", "screening.task_failed"}
    assert len(file["events"]) == 3  # no partial or invented failed screening run


def test_party_views_do_not_load_unrelated_full_run_manifests(client, admin, review_seed):
    from sqlalchemy import event

    from app.models import ScreeningRun

    original = client.get(f"/api/v1/parties/{review_seed['zero_id']}").json()
    original_run_ids = {item["run"]["id"] for item in original["events"]}
    unrelated_run_ids = set()
    with client.app.state.session_factory() as db:
        party = Party(
            source_id=review_seed["source_id"],
            external_id="unrelated-history",
            name="Synthetic Unrelated History Company",
            record_hash=party_record_hash({"name": "Synthetic Unrelated History Company"}),
        )
        db.add(party)
        db.commit()
        for _ in range(6):
            unrelated_run_ids.add(run_screening(db, party_ids=[party.id]).id)
    loaded_run_ids = set()

    def record_full_run_load(run, context):
        loaded_run_ids.add(run.id)

    event.listen(ScreeningRun, "load", record_full_run_load)
    try:
        listed = client.get("/api/v1/parties").json()
        assert listed["total"] == 3
        target = next(row for row in listed["items"] if row["id"] == review_seed["zero_id"])
        assert target["screened_current"] is True
        assert target["last_screened_at"] == original["party"]["last_screened_at"]
        assert target["stale"] is False
        assert not loaded_run_ids

        selected = client.get(f"/api/v1/parties/{review_seed['zero_id']}").json()
        assert {item["run"]["id"] for item in selected["events"]} == original_run_ids
        assert all(item["zero_hits"] and item["party_evidence"] for item in selected["events"])
        completed_audit_runs = {
            row["target_id"]
            for row in selected["audit_log"]
            if row["action"] == "screening.completed"
        }
        assert completed_audit_runs == original_run_ids
        assert loaded_run_ids == original_run_ids
        assert not loaded_run_ids & unrelated_run_ids
    finally:
        event.remove(ScreeningRun, "load", record_full_run_load)
