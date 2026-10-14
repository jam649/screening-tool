import json
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select, text

from app.config import DISCLAIMER
from app.csl.service import refresh_csl
from app.models import AuditLog, NotificationBatch, NotificationDelivery, Party, Setting, Source
from app.notifications.service import dispatch_outbox
from app.notifications.transport import DeliveryFailure
from app.screening.engine import run_screening
from app.screening.normalize import party_record_hash
from app.security import decrypt_json


def configure(client, admin, **changes):
    body = {
        "smtp_host": "127.0.0.1",
        "smtp_port": 2525,
        "smtp_tls": "none",
        "smtp_from": "screening@example.test",
        "report_recipients": ["one@example.test", "two@example.test"],
        "public_base_url": "https://screening.example.test",
        **changes,
    }
    response = client.patch(
        "/api/v1/settings/notifications", json=body, headers={"x-csrf-token": admin["csrf_token"]}
    )
    assert response.status_code == 200
    return response


def post(client, admin, path, body=None):
    return client.post("/api/v1" + path, json=body, headers={"x-csrf-token": admin["csrf_token"]})


def test_settings_encrypt_replace_clear_and_validation(client, admin):
    result = configure(
        client,
        admin,
        smtp_tls="starttls",
        smtp_username="fixture-user",
        smtp_password="fixture-secret-only",
    )
    assert result.json()["smtp_password_configured"] is True
    assert "fixture-secret-only" not in result.text
    with client.app.state.session_factory() as db:
        row = db.scalar(select(Setting).where(Setting.key == "smtp_credentials"))
        assert row.encrypted and "fixture-secret-only" not in row.value
        audit_rows = json.dumps([row.after for row in db.scalars(select(AuditLog))], default=str)
        assert "fixture-secret-only" not in audit_rows and "fixture-user" not in audit_rows
    assert client.patch(
        "/api/v1/settings/notifications",
        json={"report_time": "08:30"},
        headers={"x-csrf-token": admin["csrf_token"]},
    ).json()["smtp_password_configured"]
    for body in (
        {"smtp_password": ""},
        {"smtp_tls": "none"},
        {"public_base_url": "http://remote.example.test"},
        {"report_recipients": ["a@example.test\r\nInjected: test"]},
        {"smtp_password": "sensitive-echo-value", "bogus": True},
    ):
        response = client.patch(
            "/api/v1/settings/notifications",
            json=body,
            headers={"x-csrf-token": admin["csrf_token"]},
        )
        assert response.status_code == 422
        assert "sensitive-echo-value" not in response.text
    result = client.patch(
        "/api/v1/settings/notifications",
        json={"clear_credentials": True},
        headers={"x-csrf-token": admin["csrf_token"]},
    )
    assert not result.json()["smtp_password_configured"]


def test_partial_retry_frozen_report_boundary_and_no_writer_lock(client, admin, monkeypatch):
    configure(client, admin)
    captured = []

    def transport(settings, *, recipient, payload, message_id):
        # A second connection can take the writer lock during SMTP: delivery owns none.
        with client.app.state.session_factory() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            db.rollback()
        captured.append((recipient, payload, message_id))
        if recipient == "two@example.test" and len(captured) == 2:
            raise DeliveryFailure("recipient_rejected")

    monkeypatch.setattr("app.notifications.service.send_message", transport)
    response = post(client, admin, "/reports/morning/send")
    assert response.status_code == 202
    batch_id = response.json()["batch_id"]
    status = client.get("/api/v1/reports/morning/status").json()
    assert status["last_report_at"] is None
    batch = status["batches"][0]
    assert batch["status"] == "failed"
    assert [d["status"] for d in batch["deliveries"]] == ["sent", "failed"]
    assert "Nothing new to report." in captured[0][1]["text"]
    assert DISCLAIMER in captured[0][1]["text"]
    assert "/queue" in captured[0][1]["html"] and "auto_cleared=true" in captured[0][1]["html"]
    frozen = captured[1]
    configure(
        client,
        admin,
        report_recipients=["different@example.test"],
        smtp_from="changed@example.test",
    )
    assert post(client, admin, f"/notifications/batches/{batch_id}/retry", {}).status_code == 202
    assert len(captured) == 3 and captured[2] == frozen
    status = client.get("/api/v1/reports/morning/status").json()
    assert status["batches"][0]["status"] == "sent"
    assert status["last_report_at"] == status["batches"][0]["window_end"]
    assert post(client, admin, f"/notifications/batches/{batch_id}/retry", {}).status_code == 202
    assert len(captured) == 3


def test_uncertain_delivery_requires_explicit_retry(client, admin, monkeypatch):
    configure(client, admin)
    calls = []

    def transport(*args, **kwargs):
        calls.append(kwargs["message_id"])
        if len(calls) == 1:
            raise DeliveryFailure("delivery_uncertain", uncertain=True)

    monkeypatch.setattr("app.notifications.service.send_message", transport)
    result = post(client, admin, "/notifications/test", {"recipient": "one@example.test"})
    batch_id = result.json()["batch_id"]
    status = client.get("/api/v1/notifications/status").json()
    assert status["batches"][0]["status"] == "uncertain"
    assert status["health"][0]["kind"] == "delivery_uncertain"
    dispatch_outbox(client.app.state.session_factory, client.app.state.config)
    assert len(calls) == 1
    assert post(client, admin, f"/notifications/batches/{batch_id}/retry", {}).status_code == 409
    assert (
        post(
            client, admin, f"/notifications/batches/{batch_id}/retry", {"retry_uncertain": True}
        ).status_code
        == 202
    )
    assert len(calls) == 2 and calls[0] == calls[1]


def test_invitation_outbox_encrypted_and_revocation_cancels(client, admin, monkeypatch):
    configure(client, admin)
    monkeypatch.setattr(
        "app.notifications.service.send_message",
        lambda *a, **k: (_ for _ in ()).throw(DeliveryFailure("smtp_connection_failed")),
    )
    response = post(client, admin, "/users/invitations", {"email": "invite@example.test"})
    assert response.status_code == 201 and response.json()["delivery"] == "queued"
    result = response.json()
    token = parse_qs(urlsplit(result["invite_url"]).fragment)["token"][0]
    with client.app.state.session_factory() as db:
        batch = db.get(NotificationBatch, result["batch_id"])
        assert token not in batch.payload_enc
        assert (
            f"/accept-invite#token={token}"
            in decrypt_json(batch.payload_enc, client.app.state.config)["text"]
        )
        assert token not in json.dumps(
            [row.after for row in db.scalars(select(AuditLog))], default=str
        )
    assert post(client, admin, f"/users/invitations/{result['id']}/revoke").status_code == 200
    dispatch_outbox(
        client.app.state.session_factory,
        client.app.state.config,
        batch_id=result["batch_id"],
        force=True,
    )
    with client.app.state.session_factory() as db:
        delivery = db.scalar(
            select(NotificationDelivery).where(NotificationDelivery.batch_id == result["batch_id"])
        )
        assert delivery.status == "cancelled" and delivery.attempts == 1


def test_likely_alerts_include_repeats_and_suppression(client, admin):
    configure(client, admin, likely_alerts_enabled=True)
    with client.app.state.session_factory() as db:
        refresh_csl(
            db,
            data=json.dumps(
                [
                    {
                        "id": "acme",
                        "source": "Entity List (EL) - Bureau of Industry and Security",
                        "name": "Acme",
                        "type": "Entity",
                        "addresses": [{"country": "US"}],
                    }
                ]
            ).encode(),
        )
        source = Source(kind="csv", label="Fixture")
        db.add(source)
        db.flush()
        party = Party(
            source_id=source.id,
            external_id="one",
            name="Acme",
            country_iso2="US",
            raw={},
            record_hash=party_record_hash({"name": "Acme", "country_iso2": "US"}),
        )
        db.add(party)
        db.commit()
        first_run_id = run_screening(db).id
        run_screening(db)
        from app.screening.engine import queue_hits

        hit_id = queue_hits(db)[0].id
        assert (
            len(
                list(
                    db.scalars(select(NotificationBatch).where(NotificationBatch.kind == "likely"))
                )
            )
            == 2
        )
    response = post(
        client,
        admin,
        f"/hits/{hit_id}/dispositions",
        {"kind": "false_positive", "note": "Fixture prior decision"},
    )
    assert response.status_code == 201
    with client.app.state.session_factory() as db:
        run = run_screening(db)
        batch = db.scalar(select(NotificationBatch).where(NotificationBatch.run_id == run.id))
        payload = decrypt_json(batch.payload_enc, client.app.state.config)
        assert len(payload["observation_ids"]) == 1
        assert "suppressed observation" in payload["text"] and "Administrator" in payload["text"]
        assert "Fixture prior decision" in payload["text"]
        assert batch.run_id != first_run_id


def test_real_loopback_smtp_mime_alternatives_and_tls_fail_closed(client, admin):
    from email.parser import BytesParser
    from email.policy import default
    from threading import Thread

    from app.notifications.fixture import CaptureServer

    with CaptureServer() as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            configure(
                client,
                admin,
                smtp_port=server.server_address[1],
                report_recipients=["one@example.test"],
            )
            response = post(client, admin, "/reports/morning/send")
            assert response.status_code == 202 and len(server.messages) == 1
            message = BytesParser(policy=default).parsebytes(server.messages[0][1])
            assert message["To"] == "one@example.test"
            assert message["Subject"] == "screening-tool: 0 open items, 0 likely matches"
            assert message.get_content_type() == "multipart/alternative"
            assert [part.get_content_type() for part in message.iter_parts()] == [
                "text/plain",
                "text/html",
            ]
            assert all(DISCLAIMER in part.get_content() for part in message.iter_parts())
            text_body = message.get_body(preferencelist=("plain",)).get_content()
            for earlier, later in zip(
                [
                    "As of",
                    "Open items",
                    "Likely matches",
                    "New hits",
                    "Auto-clears",
                    "List changes",
                ],
                [
                    "Open items",
                    "Likely matches",
                    "New hits",
                    "Auto-clears",
                    "List changes",
                    "Sync health",
                ],
                strict=True,
            ):
                assert text_body.index(earlier) < text_body.index(later)
            configure(client, admin, smtp_port=server.server_address[1], smtp_tls="starttls")
            result = post(client, admin, "/notifications/test", {"recipient": "one@example.test"})
            assert result.status_code == 202 and len(server.messages) == 1
            status = client.get("/api/v1/notifications/status").json()["batches"][0]
            assert status["status"] == "failed"
            assert status["deliveries"][0]["error_code"] == "smtp_tls_or_feature_unavailable"
        finally:
            server.shutdown()
            worker.join(timeout=2)


def test_crashed_sending_truthfully_uncertain_and_report_blocked(client, admin, monkeypatch):
    configure(client, admin, report_recipients=["one@example.test"])
    monkeypatch.setattr(
        "app.notifications.service.send_message",
        lambda *a, **k: (_ for _ in ()).throw(DeliveryFailure("smtp_connection_failed")),
    )
    response = post(client, admin, "/reports/morning/send")
    batch_id = response.json()["batch_id"]
    with client.app.state.session_factory() as db:
        delivery = db.scalar(
            select(NotificationDelivery).where(NotificationDelivery.batch_id == batch_id)
        )
        delivery.status, delivery.retry_at = "sending", None
        db.commit()
    counts = dispatch_outbox(client.app.state.session_factory, client.app.state.config)
    assert counts["uncertain"] == 1 and counts["sent"] == 0
    status = client.get("/api/v1/reports/morning/status").json()
    assert status["last_report_at"] is None and status["batches"][0]["status"] == "uncertain"
    with client.app.state.session_factory() as db:
        from app.reports.morning import enqueue_morning_report

        assert enqueue_morning_report(db, client.app.state.config).id == batch_id


def test_frozen_batch_identity_and_outcome_deletion_guards(client, admin, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    from app.audit import ImmutableAuditError

    configure(client, admin)
    monkeypatch.setattr("app.notifications.service.send_message", lambda *a, **k: None)
    result = post(client, admin, "/notifications/test", {"recipient": "one@example.test"})
    batch_id = result.json()["batch_id"]
    with client.app.state.session_factory() as db:
        batch = db.get(NotificationBatch, batch_id)
        batch.payload_enc = "changed"
        with pytest.raises(ImmutableAuditError):
            db.commit()
        db.rollback()
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "UPDATE notification_deliveries SET recipient='other@example.test' "
                    "WHERE batch_id=:id"
                ),
                {"id": batch_id},
            )
        db.rollback()
        with pytest.raises(IntegrityError):
            db.execute(text("DELETE FROM notification_batches WHERE id=:id"), {"id": batch_id})


def test_real_recipient_rejection_partial_report_retry_preserves_frozen_delivery(client, admin):
    from email.parser import BytesParser
    from email.policy import default
    from threading import Thread

    from app.notifications.fixture import CaptureServer

    with CaptureServer(reject_recipient="two@example.test") as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            configure(client, admin, smtp_port=server.server_address[1])
            result = post(client, admin, "/reports/morning/send")
            assert result.status_code == 202
            batch_id = result.json()["batch_id"]
            status = client.get("/api/v1/reports/morning/status").json()
            batch = status["batches"][0]
            assert batch["status"] == "failed" and status["last_report_at"] is None
            assert [row["status"] for row in batch["deliveries"]] == ["sent", "failed"]
            assert batch["deliveries"][1]["error_code"] == "recipient_rejected"
            assert [recipient for recipient, _ in server.messages] == ["one@example.test"]
            with client.app.state.session_factory() as db:
                failed = db.scalar(
                    select(NotificationDelivery).where(
                        NotificationDelivery.batch_id == batch_id,
                        NotificationDelivery.recipient == "two@example.test",
                    )
                )
                frozen_message_id = failed.message_id
                assert failed.retry_at is not None
            configure(
                client,
                admin,
                smtp_port=server.server_address[1],
                smtp_from="changed@example.test",
                report_recipients=["other@example.test"],
            )
            server.reject_recipient = None
            retry = post(client, admin, f"/notifications/batches/{batch_id}/retry", {})
            assert retry.status_code == 202
            assert [recipient for recipient, _ in server.messages] == [
                "one@example.test",
                "two@example.test",
            ]
            first, retried = [
                BytesParser(policy=default).parsebytes(raw) for _, raw in server.messages
            ]
            assert retried["Message-ID"] == frozen_message_id
            assert first["From"] == retried["From"] == "screening@example.test"
            assert first["Subject"] == retried["Subject"]
            for alternative in ("plain", "html"):
                assert (
                    first.get_body(preferencelist=(alternative,)).get_content()
                    == retried.get_body(preferencelist=(alternative,)).get_content()
                )
            status = client.get("/api/v1/reports/morning/status").json()
            assert status["batches"][0]["status"] == "sent"
            assert status["last_report_at"] == batch["window_end"]
            assert (
                post(client, admin, f"/notifications/batches/{batch_id}/retry", {}).status_code
                == 202
            )
            assert len(server.messages) == 2
        finally:
            server.shutdown()
            worker.join(timeout=2)


@pytest.mark.parametrize(
    "phase,code", [("sender", "sender_rejected"), ("data", "smtp_data_rejected")]
)
def test_actual_smtp_sender_and_data_responses_are_definite_failures(phase, code):
    from threading import Thread

    from app.notifications.fixture import CaptureHandler, CaptureServer
    from app.notifications.settings import SMTPSettings
    from app.notifications.transport import send_message

    class RejectResponse(CaptureHandler):
        def reply(self, value):
            if phase == "sender" and value == b"250 sender accepted":
                value = b"550 sender rejected"
            elif phase == "data" and value == b"250 message captured":
                value = b"554 data rejected"
            super().reply(value)

    with CaptureServer() as server:
        server.RequestHandlerClass = RejectResponse
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            settings = SMTPSettings(
                "127.0.0.1", server.server_address[1], "none", "sender@example.test"
            )
            with pytest.raises(DeliveryFailure) as result:
                send_message(
                    settings,
                    recipient="one@example.test",
                    payload={
                        "sender": settings.sender,
                        "subject": "Fixture",
                        "text": "Fixture",
                        "html": "<p>Fixture</p>",
                    },
                    message_id="<response-fixture@example.test>",
                )
            assert result.value.code == code and result.value.uncertain is False
        finally:
            server.shutdown()
            worker.join(timeout=2)


@pytest.mark.parametrize("failure", ["disconnect", "timeout"])
def test_actual_post_data_disconnect_or_timeout_is_uncertain(failure, monkeypatch):
    import socket
    from threading import Event, Thread

    from app.notifications import transport
    from app.notifications.fixture import CaptureHandler, CaptureServer
    from app.notifications.settings import SMTPSettings

    submitted, release = Event(), Event()

    class LoseAcknowledgment(CaptureHandler):
        def reply(self, value):
            if value == b"250 message captured":
                submitted.set()
                if failure == "disconnect":
                    self.request.shutdown(socket.SHUT_RDWR)
                else:
                    release.wait(2)
                return
            super().reply(value)

    original_smtp = transport.smtplib.SMTP

    def short_timeout(*args, **kwargs):
        kwargs["timeout"] = 0.2
        return original_smtp(*args, **kwargs)

    monkeypatch.setattr(transport.smtplib, "SMTP", short_timeout)
    with CaptureServer() as server:
        server.RequestHandlerClass = LoseAcknowledgment
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            settings = SMTPSettings(
                "127.0.0.1", server.server_address[1], "none", "sender@example.test"
            )
            with pytest.raises(DeliveryFailure) as result:
                transport.send_message(
                    settings,
                    recipient="one@example.test",
                    payload={
                        "sender": settings.sender,
                        "subject": "Fixture",
                        "text": "Fixture",
                        "html": "<p>Fixture</p>",
                    },
                    message_id="<uncertain-fixture@example.test>",
                )
            assert submitted.is_set() and len(server.messages) == 1
            assert result.value.code == "delivery_uncertain" and result.value.uncertain is True
        finally:
            release.set()
            server.shutdown()
            worker.join(timeout=2)


def test_smtp_auth_response_is_definite_failure(monkeypatch):
    import smtplib

    from app.notifications import transport
    from app.notifications.settings import SMTPSettings

    class RejectAuth:
        def __init__(self, *args, **kwargs):
            pass

        def login(self, *args):
            raise smtplib.SMTPAuthenticationError(535, b"Fixture auth rejected")

        def quit(self):
            pass

        def send_message(self, *args, **kwargs):
            pytest.fail("Rejected authentication must never send message data")

    monkeypatch.setattr(transport.smtplib, "SMTP_SSL", RejectAuth)
    settings = SMTPSettings(
        "smtp.example.test", 465, "ssl", "sender@example.test", "fixture", "fixture"
    )
    with pytest.raises(DeliveryFailure) as result:
        transport.send_message(
            settings,
            recipient="one@example.test",
            payload={
                "sender": settings.sender,
                "subject": "Fixture",
                "text": "Fixture",
                "html": "<p>Fixture</p>",
            },
            message_id="<auth-fixture@example.test>",
        )
    assert result.value.code == "smtp_authentication_failed" and result.value.uncertain is False
