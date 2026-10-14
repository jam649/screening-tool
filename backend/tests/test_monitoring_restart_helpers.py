"""Read-only restart gate helpers; no Docker/runtime restart or SMTP actions."""

import importlib.util
import json
import shutil
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from test_review_api import review_seed as review_seed

from app.canonical import canonical_hash
from app.models import (
    AuditLog,
    Disposition,
    Hit,
    OperationAttempt,
    Party,
    RunPartyEvidence,
    ScreeningRun,
    utcnow,
)
from app.reports.export import export_full_zip
from app.screening.engine import run_screening, snapshot
from app.screening.normalize import party_record_hash

SCRIPT = Path(__file__).resolve().parents[2] / "tests" / "integration" / "monitoring_restart.py"
spec = importlib.util.spec_from_file_location("milestone5_restart_helpers", SCRIPT)
assert spec and spec.loader
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def checkpoint():
    return {
        "fixture_only": True,
        "passed": True,
        "schedule_ids": [*sorted(gate.CRON_IDS), "screen-recovery", "outbox-recovery"],
        "cron_next_runs": {id: "2026-10-15T10:00:00+00:00" for id in gate.CRON_IDS},
    }


def test_restart_requires_explicit_flag_before_loading_checkpoint_or_database(monkeypatch):
    def unexpected():
        pytest.fail("Missing flag must stop before gate construction")

    monkeypatch.setattr(gate, "RestartGate", unexpected)
    with pytest.raises(SystemExit) as exc:
        gate.main([])
    assert exc.value.code == 2


def test_health_wait_retries_unavailable_then_ready_without_login_or_writes():
    observed = []
    now = [0.0]

    def transport(request):
        observed.append((request.method, request.url.path))
        if len(observed) == 1:
            raise httpx.ConnectError("unavailable-private-transport-context")
        if len(observed) == 2:
            return httpx.Response(503, json={"detail": "Starting"})
        return httpx.Response(200, json={"status": "ok"})

    def advance(seconds):
        now[0] += seconds

    with httpx.Client(
        base_url=gate.BASE, trust_env=False, transport=httpx.MockTransport(transport)
    ) as http:
        gate.wait_for_health(http, seconds=1, clock=lambda: now[0], pause=advance)
    assert observed == [("GET", "/api/v1/health")] * 3
    assert now[0] == 0.5


def test_health_wait_is_bounded_and_failure_contains_no_transport_details(capsys):
    now = [0.0]
    observed = []

    def transport(request):
        observed.append(request.method)
        raise httpx.ConnectError("SECRET cookie PRIVATE message body")

    def advance(seconds):
        now[0] += seconds

    with httpx.Client(
        base_url=gate.BASE, trust_env=False, transport=httpx.MockTransport(transport)
    ) as http:
        with pytest.raises(gate.RestartFailure) as exc:
            gate.wait_for_health(http, seconds=1, clock=lambda: now[0], pause=advance)
    assert str(exc.value) == "api_startup_health_timeout"
    assert now[0] == 1 and observed == ["GET"] * 4
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize("invalid", ["failed", "nonfixture", "missing_cron"])
def test_restart_refuses_failed_or_nonfixture_checkpoint(tmp_path, invalid):
    values = checkpoint()
    if invalid == "failed":
        values["passed"] = False
    elif invalid == "nonfixture":
        values["fixture_only"] = False
    else:
        values["cron_next_runs"].pop("csl-daily")
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(values))
    with pytest.raises(gate.RestartFailure):
        gate.read_checkpoint(path)


def test_cron_times_must_persist_while_recovery_interval_wake_is_allowed():
    saved = checkpoint()
    schedules = [
        {"id": id, "next_run_at": saved["cron_next_runs"].get(id, "recovery-woken-now")}
        for id in saved["schedule_ids"]
    ]
    assert gate.require_schedules(saved, schedules) == 5
    schedules[0]["next_run_at"] = "silently-reset-later"
    with pytest.raises(gate.RestartFailure, match="cron_next_runs_exactly_preserved"):
        gate.require_schedules(saved, schedules)
    schedules[0]["next_run_at"] = saved["cron_next_runs"][schedules[0]["id"]]
    schedules.pop()
    with pytest.raises(gate.RestartFailure, match="all_saved_schedule_ids_persisted"):
        gate.require_schedules(saved, schedules)


def test_saved_ids_are_checked_in_database_beyond_api_fifty_row_projection(
    client, admin, monkeypatch
):
    with client.app.state.session_factory() as db:
        rows = [
            OperationAttempt(operation="screen_all", status="success", finished_at=utcnow())
            for _ in range(65)
        ]
        db.add_all(rows)
        db.commit()
        ids = [row.id for row in rows]
    projected = client.get("/api/v1/monitoring/status").json()["operations"]
    assert len(projected) == 50 and set(ids) - {row["id"] for row in projected}
    verifier = object.__new__(gate.RestartGate)
    verifier.checkpoint = {"operation_ids": ids}
    monkeypatch.setattr(gate, "REFERENCES", {OperationAttempt: ("operation_ids",)})
    with client.app.state.session_factory() as db:
        assert verifier.verify_ids(db) == {"operation_attempts": 65}
        assert len(list(db.scalars(select(OperationAttempt)))) == 65
        verifier.checkpoint["operation_ids"].append("missing-checkpoint-id")
        with pytest.raises(gate.RestartFailure, match="saved_operation_attempts_ids_exist"):
            verifier.verify_ids(db)


def test_restart_failure_never_prints_transport_cookie_or_customer_body(monkeypatch, capsys):
    class FailedGate:
        def run(self):
            raise RuntimeError("SECRET cookie Customer message body PRIVATE")

    monkeypatch.setattr(gate, "RestartGate", FailedGate)
    assert gate.main(["--run-fixture-gate"]) == 1
    captured = capsys.readouterr()
    assert not captured.err
    assert json.loads(captured.out) == {
        "fixture_only": True,
        "passed": False,
        "failed_check": "unexpected_restart_fixture_error",
    }
    assert "SECRET" not in captured.out and "PRIVATE" not in captured.out


def test_restart_proof_checks_real_original_snapshots_and_exported_zero_hit_party(
    client, admin, review_seed, tmp_path, monkeypatch
):
    with client.app.state.session_factory() as db:
        original_hits = {hit.id: gate.immutable_hit(hit) for hit in db.scalars(select(Hit))}
        original_evidence = {
            row.id: canonical_hash(snapshot(row)) for row in db.scalars(select(RunPartyEvidence))
        }
        original_hash = db.get(Party, review_seed["party_id"]).record_hash
    decision = client.post(
        f"/api/v1/hits/{review_seed['hit_id']}/dispositions",
        headers={"x-csrf-token": admin["csrf_token"]},
        json={"kind": "false_positive", "note": "Original restart fixture note"},
    )
    assert decision.status_code == 201
    with client.app.state.session_factory() as db:
        events = {
            event.target_id: snapshot(event)
            for event in db.scalars(select(AuditLog).where(AuditLog.action == "review.disposition"))
        }
        humans = {row.id: canonical_hash(snapshot(row)) for row in db.scalars(select(Disposition))}
        run_screening(db)
        party = db.get(Party, review_seed["party_id"])
        party.city = "Later fixture city"
        party.record_hash = party_record_hash(snapshot(party))
        db.commit()
        run_screening(db)
        checkpoint = {
            "human_disposition_id": decision.json()["disposition"]["id"],
            "admin_id": admin["user"]["id"],
            "original_party_hash": original_hash,
            "changed_party_hash": party.record_hash,
            "human_interaction_audit_sha256": canonical_hash(events),
            "original_hit_fingerprints_sha256": canonical_hash(original_hits),
            "original_human_fingerprints_sha256": canonical_hash(humans),
            "original_evidence_fingerprints_sha256": canonical_hash(original_evidence),
            "runs": len(list(db.scalars(select(ScreeningRun)))),
        }
        artifact = export_full_zip(db)
        try:
            shutil.copyfile(artifact.path, tmp_path / "full-audit.zip")
            checkpoint["full_zip_sha256"] = artifact.sha256
        finally:
            artifact.close()
        verifier = object.__new__(gate.RestartGate)
        verifier.checkpoint = checkpoint
        monkeypatch.setattr(gate, "DIRECTORY", tmp_path)
        proof = verifier.verify_original_evidence(db)
        assert proof["original_hits"] == 2
        assert proof["original_human_decisions"] == 2
        assert proof["participation_rows"] == 8
        assert proof["zero_hit_participations"] == 4
        checkpoint["original_evidence_fingerprints_sha256"] = "wrong-original-evidence"
        with pytest.raises(
            gate.RestartFailure, match="original_participation_fingerprint_unchanged"
        ):
            verifier.verify_original_evidence(db)
