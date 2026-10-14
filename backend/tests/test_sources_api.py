import csv
import io

from sqlalchemy import select
from test_csl import envelope, listing

from app.csl.service import refresh_csl
from app.models import AuditLog, Party, ScreeningRun, ScreeningTask, Source, User
from app.screening.engine import queue_hits

MAPPING = {"name": "Company", "country": "Country", "external_id": "ID", "city": "City"}


def file_content(rows):
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(["ID", "Company", "Country", "City", "Phone"])
    writer.writerows(rows)
    return stream.getvalue().encode()


def test_source_upload_mapping_screening_reupload_rejection_and_audit(client, admin, app):
    headers = {"X-CSRF-Token": admin["csrf_token"]}
    assert client.post("/api/v1/sources", json={"label": "Books"}).status_code == 403
    response = client.post("/api/v1/sources", headers=headers, json={"label": "Books"})
    assert response.status_code == 201
    source_id = response.json()["id"]
    assert "credentials_enc" not in response.json()
    data = file_content([["c1", "Example Manufacturing 42", "US", "Boston", "555-0100"]])
    preview = client.post(
        "/api/v1/sources/preview",
        headers=headers,
        files={"file": ("book.csv", data, "text/csv")},
        data={"source_id": source_id},
    )
    assert preview.status_code == 200
    assert preview.json()["total_rows"] == 1
    assert preview.json()["rows"][0]["Company"] == "Example Manufacturing 42"
    with app.state.session_factory() as db:
        refresh_csl(db, data=envelope([listing(42)]))
    import json

    uploaded = client.post(
        f"/api/v1/sources/{source_id}/upload",
        headers=headers,
        files={"file": ("book.csv", data, "text/csv")},
        data={"mapping": json.dumps(MAPPING)},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["applied"] and uploaded.json()["added"] == 1
    assert uploaded.json()["screening_run_ids"]
    with app.state.session_factory() as db:
        assert len(queue_hits(db)) == 1
        party = db.scalar(select(Party))
        original_hash = party.record_hash
        party_id = party.id
        assert db.get(Source, source_id).config["column_mapping"] == MAPPING
    # Saved mapping is reused. Phone changes are outside the comparison hash.
    repeated = client.post(
        f"/api/v1/sources/{source_id}/upload",
        headers=headers,
        files={
            "file": (
                "book.csv",
                file_content([["c1", "Example Manufacturing 42", "US", "Boston", "555-0101"]]),
                "text/csv",
            )
        },
    )
    assert repeated.json()["unchanged"] == 1
    assert repeated.json()["screening_run_ids"] == []
    rejected = client.post(
        f"/api/v1/sources/{source_id}/upload",
        headers=headers,
        files={
            "file": (
                "book.csv",
                file_content([["c1", "Changed name", "Not-a-country", "Boston", ""]]),
                "text/csv",
            )
        },
    )
    assert rejected.status_code == 200
    assert not rejected.json()["applied"]
    assert rejected.json()["rejected_count"] == 1
    with app.state.session_factory() as db:
        party = db.get(Party, party_id)
        assert party.name == "Example Manufacturing 42"
        assert party.record_hash == original_hash and party.deleted_at is None
        actions = set(db.scalars(select(AuditLog.action)))
        assert {"sources.create", "parties.create", "sources.sync_completed"} <= actions
    assert client.get("/api/v1/sources").json()[0]["health"] == "error"
    assert (
        client.patch(
            f"/api/v1/sources/{source_id}", headers=headers, json={"label": "Renamed books"}
        ).status_code
        == 200
    )
    assert client.get(f"/api/v1/sources/{source_id}").json()["label"] == "Renamed books"


def test_upload_reports_applied_import_and_completed_runs_when_old_task_fails(
    client, admin, app, monkeypatch
):
    import json

    from app.screening import engine

    headers = {"X-CSRF-Token": admin["csrf_token"]}
    created = client.post("/api/v1/sources", headers=headers, json={"label": "New books"})
    source_id = created.json()["id"]
    with app.state.session_factory() as db:
        refreshed = refresh_csl(db, data=envelope([listing(42)]))
        old_task_id = refreshed.pending_task_id
    original_execute = engine._execute

    def execute(db, **kwargs):
        if kwargs["trigger"] == "list_change":
            raise RuntimeError("private-credential-in-exception")
        return original_execute(db, **kwargs)

    monkeypatch.setattr(engine, "_execute", execute)
    response = client.post(
        f"/api/v1/sources/{source_id}/upload",
        headers=headers,
        files={
            "file": (
                "book.csv",
                file_content([["c1", "Example Manufacturing 42", "US", "Boston", ""]]),
                "text/csv",
            )
        },
        data={"mapping": json.dumps(MAPPING)},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["applied"] and payload["added"] == 1 and payload["screening_pending"]
    assert len(payload["screening_run_ids"]) == 1
    assert payload["screening_errors"][0]["task_id"] == old_task_id
    assert "private-credential" not in response.text
    with app.state.session_factory() as db:
        assert db.scalar(select(Party)).name == "Example Manufacturing 42"
        assert db.get(ScreeningTask, old_task_id).status == "failed"
        assert db.get(ScreeningRun, payload["screening_run_ids"][0]).finished_at is not None
        assert len(queue_hits(db)) == 1


def test_preview_caps_and_dummy_download(client, admin):
    headers = {"X-CSRF-Token": admin["csrf_token"]}
    content = file_content(
        [[str(n), f"Synthetic Company {n}", "US", "Boston", ""] for n in range(30)]
    )
    result = client.post(
        "/api/v1/sources/preview",
        headers=headers,
        files={"file": ("book.csv", content, "text/csv")},
    )
    assert result.status_code == 200
    assert len(result.json()["rows"]) == 20 and result.json()["total_rows"] == 30
    assert (
        client.post(
            "/api/v1/sources/preview",
            headers=headers,
            files={"file": ("book.exe", content)},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/sources/preview",
            headers=headers,
            files={"file": ("book.csv", b"x" * (10 * 1024 * 1024 + 1))},
        ).status_code
        == 413
    )
    demo = client.get("/api/v1/sources/demo.csv")
    assert demo.status_code == 200 and "attachment" in demo.headers["content-disposition"]
    assert len(list(csv.DictReader(io.StringIO(demo.text)))) == 2060


def test_reviewer_can_read_source_health_but_cannot_mutate(client, admin, app):
    headers = {"X-CSRF-Token": admin["csrf_token"]}
    assert (
        client.post("/api/v1/sources", headers=headers, json={"label": "Books"}).status_code == 201
    )
    with app.state.session_factory() as db:
        db.get(User, admin["user"]["id"]).role = "reviewer"
        db.commit()
    assert client.get("/api/v1/sources").status_code == 200
    assert (
        client.post("/api/v1/sources", headers=headers, json={"label": "Denied"}).status_code == 403
    )
    assert client.get("/api/v1/sources/demo.csv").status_code == 403


def test_algorithm_token_weights_are_not_redacted_but_credentials_are():
    from app.audit import redact_credentials
    from app.screening.settings import ScreeningSettings

    assert ScreeningSettings().snapshot()["score_weights"]["token_set"] == 0.5
    assert ScreeningSettings().snapshot()["score_weights"]["token_sort"] == 0.25
    assert redact_credentials({"access_token": "private", "token_set": "private"}) == {
        "access_token": "[redacted]",
        "token_set": "[redacted]",
    }
