"""Gate guard/output/MIME helpers only; never execute a Docker gate or runtime."""

import csv
import importlib.util
import io
import json
import stat
import threading
from copy import deepcopy
from pathlib import Path

import pytest
from sqlalchemy import select
from test_notifications import configure
from test_review_api import review_seed as review_seed

from app.canonical import canonical_hash
from app.config import DISCLAIMER
from app.models import AuditLog, Disposition, Hit, Party, ScreeningRun
from app.notifications.fixture import CaptureServer
from app.notifications.settings import SMTPSettings
from app.notifications.transport import send_message
from app.screening.engine import run_screening, snapshot
from app.screening.normalize import party_record_hash

SCRIPT = Path(__file__).resolve().parents[2] / "tests" / "integration" / "monitoring.py"
spec = importlib.util.spec_from_file_location("milestone5_gate_helpers", SCRIPT)
assert spec and spec.loader
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def test_missing_explicit_gate_flag_stops_before_config_database_or_network(monkeypatch):
    def unexpected():
        pytest.fail("Gate construction must require explicit fixture authorization")

    monkeypatch.setattr(gate, "Gate", unexpected)
    with pytest.raises(SystemExit) as exc:
        gate.main([])
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "mutation", ["customer_user", "extra_user", "api_source", "wrong_label", "credentials"]
)
def test_synthetic_installation_guard_refuses_other_installations(mutation):
    users = [{"email": gate.ADMIN_EMAIL, "role": "admin", "active": True}]
    sources = [{"kind": "csv", "label": gate.CSV_LABEL, "credentials_enc": None}]
    gate.require_synthetic_installation(users, sources)
    if mutation == "customer_user":
        users[0]["email"] = "customer@example.com"
    elif mutation == "extra_user":
        users.append(dict(users[0]))
    elif mutation == "api_source":
        sources[0]["kind"] = "qbo"
    elif mutation == "wrong_label":
        sources[0]["label"] = "Customer books"
    else:
        sources[0]["credentials_enc"] = "private-customer-envelope"
    with pytest.raises(gate.GateFailure):
        gate.require_synthetic_installation(users, sources)


def test_failure_output_never_prints_exception_body_or_credentials(monkeypatch, capsys):
    class FailedGate:
        stage = "fixture_check"

        def run(self):
            raise RuntimeError("private-cookie=SECRET SMTP body PRIVATE-CUSTOMER")

    monkeypatch.setattr(gate, "Gate", FailedGate)
    assert gate.main(["--run-fixture-gate"]) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "passed": False,
        "fixture_only": True,
        "stage": "fixture_check",
        "failed_check": "unexpected_fixture_gate_error",
    }
    assert "PRIVATE" not in captured.out and "SECRET" not in captured.out


def test_complete_csv_round_trip_and_private_files_never_overwrite(tmp_path):
    rows = [
        {"external_id": "stable-1", "name": 'Ácme, "LLC"\nSecond line', "party_type": "company"},
        {"external_id": "stable-2", "name": "東京", "party_type": "individual"},
    ]
    body = gate.csv_bytes(rows)
    assert list(csv.DictReader(io.StringIO(body.decode()))) == rows
    destination = tmp_path / "private" / "fixture.csv"
    gate.private_write(destination, body)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
    with pytest.raises(FileExistsError):
        gate.private_write(destination, b"must not replace")
    assert destination.read_bytes() == body


def test_control_mapping_is_explicit_and_saved_book_mapping_is_untouched():
    rows = [
        {
            "external_id": "control",
            "name": "Fixture",
            "party_type": "company",
            "role": "vendor",
            "country": "US",
        }
    ]
    assert json.loads(gate.upload_form({"column_mapping": {}}, rows)["mapping"]) == {
        key: key for key in rows[0]
    }
    original = {
        "column_mapping": {
            "name": "Company",
            "external_id": "ID",
            "address_lines": ["Street", "Extra"],
        }
    }
    frozen = deepcopy(original)
    assert gate.upload_form(original, [{"Company": "Original complete book"}]) == {}
    assert original == frozen
    with pytest.raises(gate.GateFailure, match="unmapped_upload_is_exact_control_schema"):
        gate.upload_form({"column_mapping": {}}, [{"Company": "Unsupported guessing"}])


def test_gate_imports_fresh_control_source_through_actual_mapped_api(client, admin, review_seed):
    client.headers.update({"x-csrf-token": admin["csrf_token"], "Origin": "https://testserver"})
    created = client.post("/api/v1/sources", json={"label": "Gate control fixture"})
    assert created.status_code == 201 and created.json()["column_mapping"] == {}
    rows = [
        {
            "external_id": "review-control",
            "name": "Ácme Manufacturing",
            "party_type": "individual",
            "role": "vendor",
            "country": "CA",
        },
        {
            "external_id": "likely-control",
            "name": "Ácme Manufacturing",
            "party_type": "company",
            "role": "vendor",
            "country": "US",
        },
    ]
    verifier = object.__new__(gate.Gate)
    verifier.http = client
    verifier.idle = lambda: None
    result = verifier.import_rows(created.json()["id"], rows)
    assert result["applied"] and result["added"] == 2 and result["screening_run_ids"]
    source = client.get(f"/api/v1/sources/{created.json()['id']}").json()
    assert source["column_mapping"] == {key: key for key in rows[0]}
    with client.app.state.session_factory() as db:
        parties = {
            row.external_id: row
            for row in db.scalars(select(Party).where(Party.source_id == source["id"]))
        }
        assert parties["review-control"].party_type == "individual"
        assert parties["review-control"].role == "vendor"
        actual = list(
            db.scalars(select(Hit).where(Hit.party_id.in_([row.id for row in parties.values()])))
        )
        assert any(
            hit.party_id == parties["review-control"].id and hit.det_score == 75 for hit in actual
        )
        assert any(
            hit.party_id == parties["likely-control"].id and hit.det_score == 100 for hit in actual
        )


def test_gate_filter_csv_tracks_actual_successful_report_boundary_without_new_screening(
    client, admin, review_seed, monkeypatch
):
    client.headers.update({"x-csrf-token": admin["csrf_token"], "Origin": "https://testserver"})
    assert (
        client.patch(
            "/api/v1/settings",
            json={
                "auto_clear_enabled": True,
                "auto_clear_threshold": 90,
                "auto_clear_acknowledgment": "I understand",
            },
        ).status_code
        == 200
    )
    source = client.post("/api/v1/sources", json={"label": "Report-window controls"}).json()
    verifier = object.__new__(gate.Gate)
    verifier.http = client
    verifier.idle = lambda: None
    verifier.checkpoint = {"control_source_id": source["id"]}
    verifier.db = client.app.state.session_factory
    verifier.import_rows(
        source["id"],
        [
            {
                "external_id": "window-review",
                "name": "Ácme Manufacturing",
                "party_type": "individual",
                "role": "vendor",
                "country": "CA",
            },
            {
                "external_id": "window-likely",
                "name": "Ácme Manufacturing",
                "party_type": "company",
                "role": "vendor",
                "country": "US",
            },
        ],
    )
    with client.app.state.session_factory() as db:
        verifier.review_party_id = db.scalar(
            select(Party.id).where(Party.external_id == "window-review")
        )
        run_ids = set(db.scalars(select(ScreeningRun.id)))
        original_decisions = {
            row.id: canonical_hash(snapshot(row)) for row in db.scalars(select(Disposition))
        }
    verifier.current_auto_clear_filter(before_report=True)
    assert verifier.checkpoint["pre_report_auto_cleared_control_parties"] == 1
    configure(client, admin)
    monkeypatch.setattr("app.notifications.service.send_message", lambda *args, **kwargs: None)
    sent = client.post("/api/v1/reports/morning/send")
    assert sent.status_code == 202
    report = client.get("/api/v1/reports/morning/status").json()
    assert report["last_report_at"] == sent.json()["window_end"]
    verifier.current_auto_clear_filter(before_report=False)
    assert verifier.checkpoint["post_report_auto_cleared_control_parties"] == 0
    with client.app.state.session_factory() as db:
        assert set(db.scalars(select(ScreeningRun.id))) == run_ids
        assert {
            row.id: canonical_hash(snapshot(row)) for row in db.scalars(select(Disposition))
        } == original_decisions


@pytest.mark.parametrize(
    "mutation",
    [
        "system_actor",
        "later_positive",
        "other_interaction",
        "wrong_hash",
        "wrong_note",
        "wrong_audit_target",
    ],
)
def test_human_authority_rejects_unrelated_or_changed_proof(mutation):
    authority = {
        "queue_hit_id": "clicked-anchor",
        "party_id": "party",
        "csl_entry_id": "entry",
        "party_hash": "original-party-hash",
        "entry_hash": "original-entry-hash",
        "kind": "false_positive",
        "note": "Original human note",
        "actor": {"id": "human", "email": "fixture@example.test", "display_name": "Original actor"},
    }
    hit = {
        "id": "fanout-observation",
        **{key: authority[key] for key in ("party_id", "csl_entry_id", "party_hash", "entry_hash")},
    }
    decision = {
        "id": "fanout-decision",
        "hit_id": hit["id"],
        "actor_user_id": "human",
        "kind": "false_positive",
        "note": authority["note"],
    }
    event = {
        "action": "review.disposition",
        "target_table": "dispositions",
        "target_id": decision["id"],
        "actor_user_id": "human",
        "after": {**deepcopy(authority), "hit_id": hit["id"]},
    }
    gate.require_human_interaction(decision, event, hit, authority)
    if mutation == "system_actor":
        decision["actor_user_id"] = None
    elif mutation == "later_positive":
        decision["kind"] = event["after"]["kind"] = "confirmed_match"
    elif mutation == "other_interaction":
        event["after"]["queue_hit_id"] = "later-clicked-anchor"
    elif mutation == "wrong_hash":
        hit["party_hash"] = "current-edited-hash"
    elif mutation == "wrong_note":
        decision["note"] = "Another person's note"
    else:
        event["target_id"] = "unrelated-decision"
    with pytest.raises(gate.GateFailure, match="original_human_interaction_audit_authority"):
        gate.require_human_interaction(decision, event, hit, authority)


def test_gate_accepts_actual_fanout_suppression_and_changed_hash_prior_audit(
    client, admin, review_seed
):
    note = "Original gate human interaction"
    response = client.post(
        f"/api/v1/hits/{review_seed['hit_id']}/dispositions",
        headers={"x-csrf-token": admin["csrf_token"]},
        json={"kind": "false_positive", "note": note},
    )
    assert response.status_code == 201 and response.json()["observations_dispositioned"] == 2
    selected_id = response.json()["disposition"]["id"]
    with client.app.state.session_factory() as db:
        events = {
            event.target_id: snapshot(event)
            for event in db.scalars(select(AuditLog).where(AuditLog.action == "review.disposition"))
        }
        authority = deepcopy(events[selected_id]["after"])
        assert len(events) == 2 and selected_id in events
        assert len({event["after"]["hit_id"] for event in events.values()}) == 2
        for id, event in events.items():
            decision = db.get(Disposition, id)
            gate.require_human_interaction(
                snapshot(decision), event, snapshot(db.get(Hit, decision.hit_id)), authority
            )
        before_count = len(list(db.scalars(select(Disposition))))
        repeated = run_screening(db)
        observed = db.scalar(
            select(Hit).where(Hit.run_id == repeated.id, Hit.party_id == review_seed["party_id"])
        )
        assert observed.status == "suppressed"
        linked = db.get(Disposition, observed.explanation["suppression_disposition_id"])
        assert linked.id in events
        gate.require_human_interaction(
            snapshot(linked), events[linked.id], snapshot(db.get(Hit, linked.hit_id)), authority
        )
        assert len(list(db.scalars(select(Disposition)))) == before_count
        party = db.get(Party, review_seed["party_id"])
        party.city = "Changed fixture city"
        party.record_hash = party_record_hash(snapshot(party))
        db.commit()
        changed = run_screening(db)
        reopened = db.scalar(select(Hit).where(Hit.run_id == changed.id, Hit.party_id == party.id))
        assert reopened.status == "open" and reopened.party_hash != authority["party_hash"]
        prior = db.get(Disposition, reopened.explanation["prior_disposition_id"])
        gate.require_human_interaction(
            snapshot(prior), events[prior.id], snapshot(db.get(Hit, prior.hit_id)), authority
        )
        assert reopened.explanation["what_changed"]["party"]["city"]["after"] == party.city


def test_real_loopback_capture_mime_helper_preserves_unicode_id_and_exact_footer(tmp_path):
    payload = {
        "sender": "fixture@example.test",
        "subject": "Synthetic Ácme report",
        "text": "東京 original fixture evidence\n\n" + DISCLAIMER,
        "html": "<p>東京 original fixture evidence</p><p>" + DISCLAIMER + "</p>",
    }
    with CaptureServer(0, output_dir=tmp_path / "mime") as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            settings = SMTPSettings(
                host="127.0.0.1",
                port=server.server_address[1],
                tls="none",
                sender=payload["sender"],
            )
            send_message(
                settings,
                recipient=gate.RECIPIENTS[0],
                payload=payload,
                message_id="<fixture-stable@example.test>",
            )
            assert len(server.messages) == 1
            recipient, raw = server.messages[0]
            message, text, html = gate.message_parts(raw)
            assert recipient == message["To"] == gate.RECIPIENTS[0]
            assert message["Message-ID"] == "<fixture-stable@example.test>"
            assert message["Subject"] == payload["subject"]
            assert text.rstrip("\n") == payload["text"]
            assert html.rstrip("\n") == payload["html"]
            assert DISCLAIMER in text and DISCLAIMER in html
            file = next((tmp_path / "mime").glob("*.eml"))
            assert file.read_bytes() == raw and stat.S_IMODE(file.stat().st_mode) == 0o600
        finally:
            server.shutdown()
            worker.join(timeout=2)
