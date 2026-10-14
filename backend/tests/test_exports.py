"""Actual immutable evidence, safe downloads and original Unicode preservation."""

import csv
import hashlib
import io
import json
import os
import zipfile
from copy import deepcopy
from datetime import timedelta

import pytest
from pypdf import PdfReader
from sqlalchemy import select
from test_retention import eligible_bundle as eligible_bundle
from test_review_api import decide
from test_review_api import review_seed as review_seed

from app.audit import audit
from app.config import DISCLAIMER
from app.models import AuditLog, Party, ScreeningRun, Source, User, utcnow
from app.reports.export import (
    _Evidence,
    csv_cell,
    export_full_zip,
    export_party,
    export_run,
)
from app.retention.service import apply_retention
from app.screening.engine import run_screening
from app.screening.normalize import party_record_hash


def csv_document(body):
    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
    document = {}
    for row in rows:
        if row["section"] == "ITA disclaimer":
            continue
        try:
            value = json.loads(row["evidence"])
        except json.JSONDecodeError:
            value = row["evidence"]
        if row["index"] != "":
            document.setdefault(row["section"], []).append(value)
        else:
            document[row["section"]] = value
    return document


def test_party_export_immutable_inputs_original_actor_and_suppression(client, admin, review_seed):
    assert (
        decide(client, admin, review_seed["hit_id"], note="Actual reviewer note").status_code == 201
    )
    with client.app.state.session_factory() as db:
        run_screening(db)
        party = db.get(Party, review_seed["party_id"])
        party.name = "Mutable current name"
        party.record_hash = party_record_hash({"name": party.name, "country_iso2": "US"})
        db.get(User, admin["user"]["id"]).display_name = "Changed actor identity"
        db.commit()
        original_scope = deepcopy(db.get(ScreeningRun, review_seed["runs"][0]).settings_snapshot)
        artifact = export_party(db, party.id, "csv", actor_user_id=admin["user"]["id"])
        document = csv_document(artifact.body)
        assert document["current_record"]["party"]["name"] == "Mutable current name"
        events = document["events"]
        original_hits = [h for e in events for h in e["comparisons"]]
        assert all(h["party_snapshot"]["name"] == "Ácme Manufacturing" for h in original_hits)
        suppressed = [h for h in original_hits if h["status"] == "suppressed"]
        assert suppressed[0]["suppression_disposition"]["actor"]["display_name"] == "Administrator"
        assert suppressed[0]["dispositions"] == []
        assert original_hits[0]["explanation"]["matched_tokens"] != "[redacted]"
        assert events[0]["run"]["settings_snapshot"]["score_weights"]["token_set"] == 0.5
        assert db.get(ScreeningRun, review_seed["runs"][0]).settings_snapshot == original_scope
        event = db.scalar(select(AuditLog).where(AuditLog.action == "export.created"))
        assert event.actor_user_id == admin["user"]["id"]
        assert event.after["sha256"] == hashlib.sha256(artifact.body).hexdigest()
        assert event.after["selection"] == {"party_id": party.id}


def test_zero_hit_snapshots_and_party_scope_exclude_other_party(client, admin, review_seed):
    with client.app.state.session_factory() as db:
        party = db.get(Party, review_seed["zero_id"])
        party.city = "Later current city"
        db.commit()
        document = csv_document(export_party(db, party.id, "csv").body)
        for event in document["events"]:
            assert event["zero_hits"] is True
            assert event["participation"]["origin"] == "run_party_evidence"
            assert event["participation"]["party_snapshot"]["city"] is None
            scope = event["run"]["settings_snapshot"]["scope"]
            assert scope["party_count"] == 2
            assert scope["party_id"] == party.id
            assert "entry_hashes" not in scope and "party_hashes" not in scope
            assert len(scope["scope_sha256"]) == 64
        assert review_seed["party_id"] not in json.dumps(document)


def test_legacy_zero_hit_uses_only_genuine_prior_snapshot(client, admin, review_seed):
    with client.app.state.session_factory() as db:
        original = db.get(ScreeningRun, review_seed["runs"][0])
        party = db.get(Party, review_seed["zero_id"])
        snapshot = deepcopy(original.settings_snapshot)
        snapshot["scope"]["party_ids"] = [party.id]
        snapshot["scope"]["party_hashes"] = {party.id: party.record_hash}
        run = ScreeningRun(
            trigger="manual",
            csl_version_id=original.csl_version_id,
            settings_snapshot=snapshot,
            parties_screened=1,
            finished_at=utcnow(),
        )
        db.add(run)
        db.commit()
        run_id = run.id
        context = _Evidence(db)
        proof = context.historical_party(run, party.id, [])
        assert proof["origin"] == "matching_prior_party_audit"
        assert proof["party_snapshot"]["name"] == "Zyzzyva Qwerty"
        # Same current hash is not enough without a real prior event.
        fake = ScreeningRun(
            trigger="manual",
            csl_version_id=original.csl_version_id,
            started_at=party.first_seen_at - timedelta(days=1),
            finished_at=party.first_seen_at - timedelta(hours=1),
            settings_snapshot=snapshot,
            parties_screened=1,
        )
        db.add(fake)
        db.commit()
        proof = _Evidence(db).historical_party(fake, party.id, [])
        assert proof["historical_fields_unavailable"] is True
        assert "party_snapshot" not in proof
        assert csv_document(export_run(db, run_id, "csv").body)["parties"][0]["zero_hits"]


def test_purged_hits_remain_real_comparisons(client, admin, eligible_bundle):
    with client.app.state.session_factory() as db:
        from app.models import Hit

        original_ratios = deepcopy(
            db.get(Hit, eligible_bundle["hit_ids"][0]).explanation["components"]["name_ratios"]
        )
        result = apply_retention(db, now=eligible_bundle["future"])
        assert result["hits"] == 2
        document = csv_document(export_party(db, eligible_bundle["party_id"], "csv").body)
        assert all(not event["zero_hits"] for event in document["events"])
        assert all(event["purged_comparisons"] for event in document["events"])
        for event in document["events"]:
            purged = event["purged_comparisons"][0]
            assert purged["hit"]["party_snapshot"]["name"] == "Ácme Manufacturing"
            assert purged["dispositions"][0]["actor"]["display_name"] == "Administrator"
            assert len(purged["certificate_sha256"]) == 64
            assert purged["hit"]["explanation"]["components"]["name_ratios"] == original_ratios
        run_doc = csv_document(export_run(db, eligible_bundle["runs"][0], "csv").body)
        member = next(p for p in run_doc["parties"] if p["party_id"] == eligible_bundle["party_id"])
        assert not member["zero_hits"] and member["purged_comparisons"]


@pytest.mark.parametrize(
    "prefix", ["=", "+", "-", "@", " \t=", "\r\n@", "\ufeff=", "\x00 +", "\u200b-", "\u001f="]
)
def test_spreadsheet_prefixes_including_controls(prefix):
    assert csv_cell(prefix + "SUM(A1)").startswith("'")
    assert csv_cell('ordinary,"quoted"') == 'ordinary,"quoted"'


def test_current_view_csv_exact_filters_without_pagination(client, admin, review_seed):
    with client.app.state.session_factory() as db:
        for i in range(103):
            name = f"Added fixture {i:03}"
            db.add(
                Party(
                    source_id=review_seed["source_id"],
                    external_id=str(i),
                    name=name,
                    role="vendor",
                    country_iso2="CA",
                    record_hash=party_record_hash({"name": name, "country_iso2": "CA"}),
                )
            )
        db.commit()
    response = client.get(
        "/api/v1/exports/parties.csv",
        params={
            "role": "vendor",
            "country": "ca",
            "status": "unscreened",
            "offset": 100,
            "limit": 1,
        },
    )
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert len([r for r in rows if r["id"] != "ITA disclaimer"]) == 103
    assert all(r["status"] == "unscreened" for r in rows if r["id"] != "ITA disclaimer")
    assert rows[-1]["name"] == DISCLAIMER
    listing = client.get(
        "/api/v1/parties",
        params={"role": "vendor", "country": "ca", "status": "unscreened", "limit": 20000},
    ).json()
    assert {r["id"] for r in rows[:-1]} == {r["id"] for r in listing["items"]}


def test_zip_shared_manifests_checksums_deleted_privacy_cleanup(
    client, admin, review_seed, tmp_path
):
    with client.app.state.session_factory() as db:
        db.get(Party, review_seed["zero_id"]).deleted_at = utcnow()
        source = db.get(Source, review_seed["source_id"])
        source.config = {
            "client_secret": "DO-NOT-EXPORT",
            "payload_enc": "PRIVATE-ENCRYPTED-PAYLOAD",
            "nested": {"access_token": "NO-TOKEN"},
        }
        source.credentials_enc = "SECRET-ENVELOPE"
        audit(
            db,
            "fixture.secret",
            "settings",
            after={"key": "private_fixture", "value": "PRIVATE-VALUE", "encrypted": False},
        )
        db.commit()
        stored_scope = deepcopy(db.get(ScreeningRun, review_seed["runs"][0]).settings_snapshot)
        artifact = export_full_zip(db, spool_dir=tmp_path)
        assert artifact.body is None and artifact.path.is_file()
        assert os.stat(artifact.path).st_mode & 0o077 == 0
        with zipfile.ZipFile(artifact.path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["selection"]["party_count"] == 2
            for name, expected in manifest["files"].items():
                data = archive.read(name)
                assert hashlib.sha256(data).hexdigest() == expected["sha256"]
                assert not any(
                    s in data
                    for s in [b"DO-NOT-EXPORT", b"NO-TOKEN", b"SECRET-ENVELOPE", b"PRIVATE-VALUE"]
                )
            assert all(not name.startswith("/") and ".." not in name for name in archive.namelist())
            party_doc = json.loads(archive.read(f"parties/{review_seed['zero_id']}.json"))
            assert party_doc["current_record"]["party"]["deleted_at"]
            for run_id in review_seed["runs"]:
                doc = json.loads(archive.read(f"runs/{run_id}.json"))
                assert len(doc["settings_snapshot"]["scope"]["party_ids"]) == 2
                assert doc["settings_snapshot"]["scope"]["entry_hashes"]
            assert review_seed["party_id"] not in json.dumps(party_doc)
        assert db.get(ScreeningRun, review_seed["runs"][0]).settings_snapshot == stored_scope
        path = artifact.path
        artifact.close()
        assert not path.exists()


def test_pdf_exact_original_attachment_unicode_controls_and_footer(client, admin, review_seed):
    original = 'José Αλέξανδρος Александр شركة السلام 東京 नमस्ते 😀 𒀀 𠀀 <script> & "quoted"'
    with client.app.state.session_factory() as db:
        party = db.get(Party, review_seed["zero_id"])
        party.name = original
        party.raw = {"password": "NEVER-IN-PDF", "original": original, "url": "javascript:alert(1)"}
        db.commit()
        artifact = export_party(db, party.id, "pdf")
    reader = PdfReader(io.BytesIO(artifact.body))
    attachment = reader.attachments["evidence.json"][0]
    proof = json.loads(attachment)
    assert proof["current_record"]["party"]["name"] == original
    assert b"NEVER-IN-PDF" not in attachment
    assert proof["current_record"]["party"]["raw"]["url"] is None
    for page in reader.pages:
        text = " ".join(page.extract_text().split())
        assert DISCLAIMER in text
    full_text = " ".join(page.extract_text() for page in reader.pages)
    assert "José" in full_text and "東京" in full_text
    assert all(code in full_text for code in ["[U+1F600]", "[U+12000]", "[U+20000]"])
    assert original == json.loads(attachment)["current_record"]["party"]["name"]


def test_authenticated_attachment_headers_and_missing_invalid_format(client, admin, review_seed):
    paths = [
        f"/parties/{review_seed['party_id']}/export?format=csv",
        f"/runs/{review_seed['runs'][0]}/export?format=csv",
        "/exports/parties.csv",
        "/exports/full.zip",
    ]
    for path in paths:
        response = client.get("/api/v1" + path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.headers["x-export-sha256"] == hashlib.sha256(response.content).hexdigest()
    assert client.get("/api/v1/parties/missing/export").status_code == 404
    assert client.get("/api/v1/runs/missing/export").status_code == 404
    assert (
        client.get(f"/api/v1/parties/{review_seed['party_id']}/export?format=html").status_code
        == 422
    )
    client.cookies.clear()
    for path in paths:
        assert client.get("/api/v1" + path).status_code == 401


def test_invalid_download_does_not_record_success(client, admin, review_seed):
    with client.app.state.session_factory() as db:
        before = list(db.scalars(select(AuditLog.id).where(AuditLog.action == "export.created")))
    assert client.get("/api/v1/parties/../../anything/export").status_code in {404, 405}
    assert client.get("/api/v1/runs/invalid/export").status_code == 404
    with client.app.state.session_factory() as db:
        assert (
            list(db.scalars(select(AuditLog.id).where(AuditLog.action == "export.created")))
            == before
        )


def test_run_pdf_large_scope_is_summary_with_exact_embedded_manifest(client, admin, review_seed):
    with client.app.state.session_factory() as db:
        original = db.get(ScreeningRun, review_seed["runs"][0])
        settings = deepcopy(original.settings_snapshot)
        party_ids = [f"scope-party-{i:05}" for i in range(20000)]
        entry_ids = [f"scope-entry-{i:05}" for i in range(25000)]
        settings["scope"] = {
            "party_ids": party_ids,
            "party_hashes": {p: hashlib.sha256(p.encode()).hexdigest() for p in party_ids},
            "entry_ids": entry_ids,
            "entry_hashes": {e: hashlib.sha256(e.encode()).hexdigest() for e in entry_ids},
        }
        run = ScreeningRun(
            trigger="manual",
            csl_version_id=original.csl_version_id,
            settings_snapshot=settings,
            parties_screened=20000,
            finished_at=utcnow(),
        )
        db.add(run)
        db.commit()
        artifact = export_run(db, run.id, "pdf")
    reader = PdfReader(io.BytesIO(artifact.body))
    text = " ".join(p.extract_text() for p in reader.pages)
    assert "20000 parties; 25000 list entries" in text
    assert "Zero-hit participants: 20000" in text
    assert "scope-party-00000" not in text and "scope-entry-00000" not in text
    assert len(reader.pages) <= 4
    assert len(text) < 12000
    attachment = json.loads(reader.attachments["evidence.json"][0])
    assert attachment["run"]["settings_snapshot"]["scope"] == settings["scope"]
    assert len(attachment["parties"]) == 20000
    assert artifact.size < 3_000_000


def test_historical_sources_and_original_decision_audits_in_run(client, admin, review_seed):
    assert decide(client, admin, review_seed["hit_id"], note="Real decision").status_code == 201
    with client.app.state.session_factory() as db:
        run = run_screening(db)
        source = db.get(Source, review_seed["source_id"])
        source.label = "Renamed source"
        replacement = Source(kind="csv", label="Current replacement")
        db.add(replacement)
        db.flush()
        db.get(Party, review_seed["party_id"]).source_id = replacement.id
        db.commit()
        party = csv_document(export_party(db, review_seed["party_id"], "csv").body)
        historical = party["events"][0]["participation"]["source_snapshot"]
        assert historical["id"] == review_seed["source_id"]
        assert historical["label"] == "Fixture CSV"
        assert any(
            a["target_id"] == review_seed["source_id"] and a["action"] == "sources.create"
            for a in party["audit_log"]
        )
        document = csv_document(export_run(db, run.id, "csv").body)
        assert any(a["target_table"] == "sources" for a in document["audit_log"])
        assert any(a["action"] == "review.disposition" for a in document["audit_log"])
        assert any(a["target_table"] == "suppressions" for a in document["audit_log"])


def test_export_snapshot_allows_concurrent_writer_and_preserves_as_of(
    client, admin, review_seed, monkeypatch
):
    original_party = _Evidence.party

    def concurrent_edit(context, party_id):
        # Export keeps its original read snapshot, while another session can commit.
        with client.app.state.session_factory() as writer:
            writer.get(Party, party_id).name = "Changed concurrently"
            writer.commit()
        return original_party(context, party_id)

    monkeypatch.setattr(_Evidence, "party", concurrent_edit)
    with client.app.state.session_factory() as db:
        result = csv_document(export_party(db, review_seed["party_id"], "csv").body)
        assert result["current_record"]["party"]["name"] == "Ácme Manufacturing"
        assert db.get(Party, review_seed["party_id"]).name == "Changed concurrently"
        log = db.scalar(select(AuditLog).where(AuditLog.action == "export.created"))
        assert log.after["evidence_as_of"] == result["exported_at"]


def test_zip_render_failure_removes_private_spool_and_records_no_success(
    client, admin, review_seed, tmp_path, monkeypatch
):
    from app.reports import export as service

    def fail(document):
        raise RuntimeError("Synthetic render failure")

    monkeypatch.setattr(service, "_evidence_csv", fail)
    with client.app.state.session_factory() as db:
        with pytest.raises(RuntimeError, match="Synthetic render failure"):
            export_full_zip(db, spool_dir=tmp_path / "spool")
        assert list((tmp_path / "spool").iterdir()) == []
        assert not db.scalar(select(AuditLog).where(AuditLog.action == "export.created"))


def test_numeric_weights_exact_across_formats_and_private_tokens_rejected(
    client, admin, review_seed, tmp_path
):
    from app.reports.export import _safe

    for bad in [
        "credential-string",
        {"secret": "value"},
        True,
        float("inf"),
        float("nan"),
        -0.1,
        100.1,
    ]:
        for key in ["token_set", "token_sort"]:
            assert _safe({key: bad})[key] == "[redacted]"
            assert _safe({key: bad}, explanation=True)[key] == "[redacted]"
    assert _safe({"token_sort": 1.1})["token_sort"] == "[redacted]"
    assert _safe({"token_sort": 1.1}, explanation=True)["token_sort"] == 1.1
    assert (
        _safe({"raw": {"explanation": {"matched_tokens": "PRIVATE-RAW-TOKEN"}}})["raw"][
            "explanation"
        ]["matched_tokens"]
        == "[redacted]"
    )
    with client.app.state.session_factory() as db:
        run_id = review_seed["runs"][0]
        run = db.get(ScreeningRun, run_id)
        expected = deepcopy(run.settings_snapshot["score_weights"])
        assert isinstance(expected["token_set"], float) and isinstance(
            expected["token_sort"], float
        )
        party = db.get(Party, review_seed["party_id"])
        party.raw = {
            "token_set": "STRING-CREDENTIAL",
            "token_sort": {"secret": "OBJECT-CREDENTIAL"},
            "explanation": {"matched_tokens": "PRIVATE-RAW-TOKEN"},
        }
        db.commit()
        csv_proof = csv_document(export_run(db, run_id, "csv").body)
        assert csv_proof["run"]["settings_snapshot"]["score_weights"] == expected
        pdf = PdfReader(io.BytesIO(export_run(db, run_id, "pdf").body))
        pdf_proof = json.loads(pdf.attachments["evidence.json"][0])
        assert pdf_proof["run"]["settings_snapshot"]["score_weights"] == expected
        text = " ".join(p.extract_text() for p in pdf.pages)
        assert f"Token set: {expected['token_set']}" in text
        assert f"Token sort: {expected['token_sort']}" in text
        artifact = export_full_zip(db, spool_dir=tmp_path / "weight-spool")
        try:
            with zipfile.ZipFile(artifact.path) as archive:
                zip_run = json.loads(archive.read(f"runs/{run_id}.json"))
                assert zip_run["settings_snapshot"]["score_weights"] == expected
                zip_party = json.loads(archive.read(f"parties/{party.id}.json"))
                assert all(
                    e["run"]["settings_snapshot"]["score_weights"] == expected
                    for e in zip_party["events"]
                )
                serialized = json.dumps(zip_party)
                assert (
                    "STRING-CREDENTIAL" not in serialized and "OBJECT-CREDENTIAL" not in serialized
                )
                assert "PRIVATE-RAW-TOKEN" not in serialized
                assert any(
                    h["explanation"]["matched_tokens"] != "[redacted]"
                    for e in zip_party["events"]
                    for h in e["comparisons"]
                )
        finally:
            artifact.close()


def test_actual_scorer_ratios_survive_party_run_pdf_csv_zip_and_hit_audit(
    client, admin, review_seed, tmp_path
):
    from app.models import CSLEntry, Hit
    from app.screening.score import score_pair
    from app.screening.settings import load_settings

    def verify(document, hit_id, expected):
        members = document.get("events", document.get("parties", []))
        hit = next(h for member in members for h in member["comparisons"] if h["id"] == hit_id)
        assert hit["explanation"]["components"]["name_ratios"] == expected
        creation = next(
            row
            for row in document["audit_log"]
            if row["target_table"] == "hits"
            and row["target_id"] == hit_id
            and (row.get("after") or {}).get("explanation")
        )
        assert creation["after"]["explanation"]["components"]["name_ratios"] == expected

    with client.app.state.session_factory() as db:
        hit = db.get(Hit, review_seed["hit_id"])
        scored = score_pair(
            db.get(Party, hit.party_id), db.get(CSLEntry, hit.csl_entry_id), load_settings(db)
        )
        expected = scored.explanation["components"]["name_ratios"]
        assert expected["token_set"] == expected["token_sort"] == 100.0
        assert hit.explanation["components"]["name_ratios"] == expected
        original_audit = db.scalar(
            select(AuditLog).where(AuditLog.target_id == hit.id, AuditLog.action == "hits.create")
        )
        assert original_audit.after["explanation"]["components"]["name_ratios"] == expected
        party_id, run_id, hit_id = hit.party_id, hit.run_id, hit.id
        for exporter, identity in [(export_party, party_id), (export_run, run_id)]:
            csv_proof = csv_document(exporter(db, identity, "csv").body)
            verify(csv_proof, hit_id, expected)
            pdf = PdfReader(io.BytesIO(exporter(db, identity, "pdf").body))
            pdf_proof = json.loads(pdf.attachments["evidence.json"][0])
            verify(pdf_proof, hit_id, expected)
        artifact = export_full_zip(db, spool_dir=tmp_path / "ratio-spool")
        try:
            with zipfile.ZipFile(artifact.path) as archive:
                verify(json.loads(archive.read(f"parties/{party_id}.json")), hit_id, expected)
                verify(csv_document(archive.read(f"parties/{party_id}.csv")), hit_id, expected)
                records = [json.loads(line) for line in archive.read("audit.jsonl").splitlines()]
                creation = next(
                    row
                    for row in records
                    if row.get("target_id") == hit_id and row.get("action") == "hits.create"
                )
                assert creation["after"]["explanation"]["components"]["name_ratios"] == expected
        finally:
            artifact.close()


@pytest.mark.parametrize("failure", ["send", "cancel", "missing_file"])
def test_private_zip_cleanup_on_asgi_send_cancel_and_stat_failure(
    client, admin, review_seed, tmp_path, failure
):
    import asyncio

    from app.api.exports import download

    with client.app.state.session_factory() as db:
        artifact = export_full_zip(db, spool_dir=tmp_path / "asgi-spool")
    path, directory = artifact.path, artifact.path.parent
    response = download(artifact)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-export-sha256"] == artifact.sha256
    assert response.headers["content-disposition"].startswith("attachment;")
    sent = []

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body":
            if failure == "cancel":
                raise asyncio.CancelledError()
            raise OSError("Synthetic disconnected ASGI transport")

    async def receive():
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/exports/full.zip",
        "headers": [],
        "asgi": {"spec_version": "2.4"},
    }
    expected = asyncio.CancelledError if failure == "cancel" else OSError
    if failure == "missing_file":
        path.unlink()
        expected = RuntimeError
    with pytest.raises(expected):
        asyncio.run(response(scope, receive, send))
    assert not path.exists() and not directory.exists()
    assert artifact._directory is None
    artifact.close()  # Idempotent after a failed/cancelled transfer.
    if failure != "missing_file":
        headers = dict(sent[0]["headers"])
        assert headers[b"cache-control"] == b"no-store"
        assert headers[b"x-export-sha256"].decode() == artifact.sha256
        assert headers[b"content-disposition"].startswith(b"attachment;")
