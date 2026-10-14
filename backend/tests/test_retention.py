"""Exercise real bounded retention, permanent provenance and ordinary-delete denial."""

import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from test_review_api import decide
from test_review_api import review_seed as review_seed

from app.audit import ImmutableAuditError
from app.canonical import canonical_hash
from app.models import (
    AuditLog,
    Disposition,
    Hit,
    Party,
    RetentionPurgeCertificate,
    RunPartyEvidence,
    ScreeningRun,
    Setting,
    User,
)
from app.models.base import utcnow
from app.retention import service
from app.retention.service import (
    RetentionValidationError,
    anniversary,
    apply_retention,
    attest,
    eligibility,
    place_hold,
    purge_plan,
    release_hold,
)


@pytest.fixture
def eligible_bundle(client, admin, review_seed):
    response = decide(client, admin, review_seed["hit_id"], "confirmed_match", "Original review")
    assert response.status_code == 201
    with client.app.state.session_factory() as db:
        actor = db.get(User, admin["user"]["id"])
        party = db.get(Party, review_seed["party_id"])
        party.deleted_at = utcnow()
        db.add(Setting(key="retention_years", value=5))
        proof = attest(
            db,
            party,
            actor,
            event_date=date(2020, 1, 1),
            external_dates_considered=True,
            government_holds_excluded=True,
        )
        db.commit()
        future = utcnow().replace(year=utcnow().year + 6)
        before = list(db.scalars(select(Hit).where(Hit.party_id == party.id)))
        return {
            **review_seed,
            "proof_id": proof.id,
            "future": future,
            "hit_ids": [hit.id for hit in before],
        }


def test_default_disabled_and_five_year_floor(client, admin, review_seed):
    with client.app.state.session_factory() as db:
        assert not purge_plan(db)["enabled"]
        result = apply_retention(db)
        assert result["hits"] == result["dispositions"] == 0
        assert result["certificates"] == []
        assert len(list(db.scalars(select(Hit)))) == 2
        db.add(Setting(key="retention_years", value=4))
        db.commit()
        with pytest.raises(RetentionValidationError):
            purge_plan(db)
    assert (
        client.patch(
            "/api/v1/settings",
            json={"retention_years": 4},
            headers={"x-csrf-token": admin["csrf_token"]},
        ).status_code
        == 422
    )


def test_exact_clock_boundary_and_conservative_leap_anniversary(client, eligible_bundle):
    assert anniversary(date(2020, 2, 29), 5) == date(2025, 3, 1)
    assert anniversary(date(2020, 1, 1), 10000) is None
    with client.app.state.session_factory() as db:
        party = db.get(Party, eligible_bundle["party_id"])
        latest_activity = db.scalar(
            select(func.max(AuditLog.created_at)).where(
                AuditLog.target_table == "parties", AuditLog.target_id == party.id
            )
        )
        boundary = latest_activity.replace(year=latest_activity.year + 5)
        assert eligibility(db, party, now=boundary)["eligible"]
        before = eligibility(db, party, now=boundary - timedelta(microseconds=1))
        assert "within_retention_period" in before["reasons"]
        assert not before["eligible"]


def test_unknown_activity_and_new_external_event_reset(client, admin, eligible_bundle):
    with client.app.state.session_factory() as db:
        party = db.get(Party, eligible_bundle["zero_id"])
        party.deleted_at = utcnow()
        db.commit()
        result = eligibility(db, party, now=eligible_bundle["future"])
        assert "external_event_date_unproven" in result["reasons"]
        party = db.get(Party, eligible_bundle["party_id"])
        actor = db.get(User, admin["user"]["id"])
        attest(
            db,
            party,
            actor,
            event_date=date(2021, 1, 1),
            external_dates_considered=False,
            government_holds_excluded=True,
        )
        db.commit()
        assert (
            "eligibility_not_attested"
            in eligibility(db, party, now=eligible_bundle["future"])["reasons"]
        )
        with pytest.raises(RetentionValidationError, match="future"):
            attest(
                db,
                party,
                actor,
                event_date=utcnow().date() + timedelta(days=1),
                external_dates_considered=True,
                government_holds_excluded=True,
            )
        db.rollback()
        # An event-date-only attestation preserves the whole event day conservatively.
        attest(
            db,
            party,
            actor,
            event_date=utcnow().date(),
            external_dates_considered=True,
            government_holds_excluded=True,
        )
        db.commit()
        proof = eligibility(db, party, now=eligible_bundle["future"])
        assert datetime.fromisoformat(proof["basis_at"]).time().isoformat() == "23:59:59.999999"
        party.city = "A newer party edit"
        db.commit()
        assert eligibility(db, party, now=utcnow())["eligible"] is False


def test_global_government_hold_cannot_be_overridden_by_attestation(client, admin, eligible_bundle):
    with client.app.state.session_factory() as db:
        actor = db.get(User, admin["user"]["id"])
        hold = place_hold(db, actor, party_id=None, kind="government", reason="Agency request")
        db.commit()
        assert purge_plan(db, now=eligible_bundle["future"])["hits"] == 0
        with pytest.raises(RetentionValidationError, match="written agency"):
            release_hold(db, hold, actor, reason="Request complete")
        db.rollback()
        release_hold(
            db,
            hold,
            actor,
            reason="Agency released",
            authorization_reference="Fixture written authorization 123",
        )
        db.commit()
        assert purge_plan(db, now=eligible_bundle["future"])["hits"] == 2
        with pytest.raises(RetentionValidationError, match="already released"):
            release_hold(db, hold, actor, reason="Duplicate", authorization_reference="123")


def test_genuine_purge_preserves_original_actor_audit_runs_and_party(
    client, admin, eligible_bundle
):
    factory = client.app.state.session_factory
    with factory() as db:
        actor = db.get(User, admin["user"]["id"])
        actor.display_name = "Later identity"
        db.commit()
        original_audit = set(db.scalars(select(AuditLog.id)))
        original_run_counts = {
            run.id: (run.hits_new, run.hits_suppressed, run.auto_cleared, run.parties_screened)
            for run in db.scalars(select(ScreeningRun))
        }
        plan = purge_plan(db, now=eligible_bundle["future"])
        assert plan["hits"] == plan["dispositions"] == 2
        result = apply_retention(db, now=eligible_bundle["future"])
        assert result["hits"] == 2 and len(result["certificates"]) == 1
        assert not list(db.scalars(select(Hit)))
        assert not list(db.scalars(select(Disposition)))
        assert db.get(Party, eligible_bundle["party_id"]) is not None
        assert original_audit <= set(db.scalars(select(AuditLog.id)))
        assert original_run_counts == {
            run.id: (run.hits_new, run.hits_suppressed, run.auto_cleared, run.parties_screened)
            for run in db.scalars(select(ScreeningRun))
        }
        assert len(list(db.scalars(select(RunPartyEvidence)))) == 4
        certificate = db.scalar(select(RetentionPurgeCertificate))
        assert certificate.sha256 == canonical_hash(certificate.evidence)
        assert certificate.evidence["attestation"]["id"] == eligible_bundle["proof_id"]
        assert certificate.evidence["minimum_years"] == 5
        assert certificate.cutoff_date.year == eligible_bundle["future"].year - 5
        assert all(
            row["actor"]["display_name"] == "Administrator"
            for row in certificate.evidence["dispositions"]
        )
        assert all(
            hit["party_snapshot"]["name"] == "Ácme Manufacturing"
            for hit in certificate.evidence["hits"]
        )
        assert not db.connection().info["retention_delete_capability"]
    file = client.get(f"/api/v1/parties/{eligible_bundle['party_id']}").json()
    assert len(file["events"]) == 2
    assert all(event["zero_hits"] is False for event in file["events"])
    assert all(len(event["purged_comparisons"]) == 1 for event in file["events"])
    assert all(event["party_evidence"]["hits_recorded"] == 1 for event in file["events"])
    assert any(row["action"] == "retention.purged" for row in file["audit_log"])
    with factory() as db:
        assert apply_retention(db, now=eligible_bundle["future"])["certificates"] == []


def test_false_positive_memory_and_retained_anchor_protect_original(client, admin, review_seed):
    assert decide(client, admin, review_seed["hit_id"]).status_code == 201
    with client.app.state.session_factory() as db:
        party = db.get(Party, review_seed["party_id"])
        party.deleted_at = utcnow()
        db.add(Setting(key="retention_years", value=5))
        actor = db.get(User, admin["user"]["id"])
        attest(
            db,
            party,
            actor,
            event_date=date(2020, 1, 1),
            external_dates_considered=True,
            government_holds_excluded=True,
        )
        db.commit()
        future = utcnow().replace(year=utcnow().year + 6)
        result = apply_retention(db, now=future)
        # The suppression protects its decision/anchor, while its unreferenced repeat
        # observation may genuinely qualify for removal.
        assert result["hits"] == 1
        assert db.get(Hit, review_seed["hit_id"]) is not None
        assert len(list(db.scalars(select(Disposition)))) == 1
        assert any(
            "retained_anchor_or_decision_reference" in row["reasons"] for row in result["blocked"]
        )


def test_injected_failure_rolls_back_certificate_rows_and_clears_capability(
    client, eligible_bundle, monkeypatch
):
    original = service.audit

    def fail(db, action, *args, **kwargs):
        if action == "retention.purged":
            raise RuntimeError("Injected transaction failure")
        return original(db, action, *args, **kwargs)

    monkeypatch.setattr(service, "audit", fail)
    with client.app.state.session_factory() as db:
        with pytest.raises(RuntimeError, match="Injected"):
            apply_retention(db, now=eligible_bundle["future"])
        assert len(list(db.scalars(select(Hit)))) == 2
        assert len(list(db.scalars(select(Disposition)))) == 2
        assert not list(db.scalars(select(RetentionPurgeCertificate)))
        assert not db.connection().info["retention_delete_capability"]


def test_default_sql_orm_and_raw_sqlite_cannot_delete_evidence(client, config, eligible_bundle):
    factory = client.app.state.session_factory
    with factory() as db:
        db.delete(db.get(Hit, eligible_bundle["hit_ids"][0]))
        with pytest.raises(ImmutableAuditError):
            db.flush()
        db.rollback()
        with pytest.raises((ImmutableAuditError, IntegrityError)):
            db.execute(delete(Disposition))
        db.rollback()
        with pytest.raises(IntegrityError, match="exact retention"):
            db.execute(text("DELETE FROM hits"))
        db.rollback()
        # A bogus settings flag or unapproved SQL row cannot enable deletion.
        db.info["retention_allowed"] = True
        with pytest.raises(IntegrityError):
            db.execute(text("DELETE FROM dispositions"))
        db.rollback()
    path = config.database_url.removeprefix("sqlite:///")
    with sqlite3.connect(path) as raw:
        with pytest.raises(sqlite3.OperationalError, match="retention_delete_allowed"):
            raw.execute("DELETE FROM hits")


def test_exact_capability_is_atomic_and_pool_never_retains_grants(client, eligible_bundle):
    factory = client.app.state.session_factory
    with factory() as db:
        allowed = eligible_bundle["hit_ids"][0]
        with service._delete_capability(db, {("hits", allowed)}):
            with pytest.raises(IntegrityError):
                db.execute(text("DELETE FROM hits"))
            assert len(list(db.scalars(select(Hit)))) == 2
        assert not db.connection().info["retention_delete_capability"]
        db.rollback()
        db.connection().info["retention_delete_capability"].add(("hits", allowed))
        db.rollback()
        assert not db.connection().info["retention_delete_capability"]
    with factory() as db:
        assert not db.connection().info["retention_delete_capability"]
        with pytest.raises(IntegrityError):
            db.execute(text("DELETE FROM hits"))


@pytest.mark.parametrize(
    "table",
    [
        "retention_attestations",
        "retention_holds",
        "retention_hold_releases",
        "retention_purge_certificates",
        "audit_log",
        "screening_runs",
        "run_party_evidence",
    ],
)
def test_all_permanent_evidence_guards_survive_successful_purge(
    client, admin, eligible_bundle, table
):
    with client.app.state.session_factory() as db:
        actor = db.get(User, admin["user"]["id"])
        hold = place_hold(db, actor, party_id=None, kind="other", reason="Closed fixture hold")
        db.flush()
        release_hold(db, hold, actor, reason="No longer needed")
        db.commit()
        apply_retention(db, now=eligible_bundle["future"])
        with pytest.raises(IntegrityError):
            db.execute(text(f'DELETE FROM "{table}"'))
        db.rollback()
        with pytest.raises(IntegrityError):
            db.execute(text(f'UPDATE "{table}" SET id=id'))
        db.rollback()
        with pytest.raises(IntegrityError):
            db.execute(text(f'INSERT OR REPLACE INTO "{table}" SELECT * FROM "{table}"'))
        db.rollback()


def test_api_attestations_holds_csrf_and_disabled_apply(client, admin, review_seed):
    path = f"/api/v1/retention/parties/{review_seed['party_id']}/eligibility"
    values = {
        "event_date": "2020-01-01",
        "external_dates_considered": True,
        "government_holds_excluded": True,
    }
    assert client.patch(path, json=values).status_code == 403
    headers = {"x-csrf-token": admin["csrf_token"]}
    response = client.patch(path, json=values, headers=headers)
    assert response.status_code == 200
    assert "active_party" in response.json()["reasons"]
    assert "retention_disabled" in response.json()["reasons"]
    assert (
        client.patch(
            path, json={**values, "external_dates_considered": "yes"}, headers=headers
        ).status_code
        == 422
    )
    response = client.post(
        "/api/v1/retention/holds",
        headers=headers,
        json={"kind": "government", "reason": "Agency request"},
    )
    assert response.status_code == 201
    hold_id = response.json()["id"]
    release_path = f"/api/v1/retention/holds/{hold_id}/release"
    assert client.post(release_path, json={"reason": "Release"}, headers=headers).status_code == 422
    assert (
        client.post(
            release_path,
            json={"reason": "Released", "authorization_reference": "ABC"},
            headers=headers,
        ).status_code
        == 201
    )
    status = client.get("/api/v1/retention/status").json()
    assert status["enabled"] is False and status["active_holds"] == []
    assert client.post("/api/v1/retention/apply", headers=headers).json()["hits"] == 0
    assert client.post("/api/v1/retention/preview", headers=headers).json()["enabled"] is False
    client.cookies.clear()
    assert client.get(path).status_code == 401


def test_attestation_must_cover_deletion_and_later_activity(client, admin, review_seed):
    with client.app.state.session_factory() as db:
        actor = db.get(User, admin["user"]["id"])
        party = db.get(Party, review_seed["party_id"])
        db.add(Setting(key="retention_years", value=5))
        attest(
            db,
            party,
            actor,
            event_date=date(2020, 1, 1),
            external_dates_considered=True,
            government_holds_excluded=True,
        )
        db.commit()
        party.deleted_at = utcnow()
        db.commit()
        future = utcnow().replace(year=utcnow().year + 6)
        assert "attestation_predates_activity" in eligibility(db, party, now=future)["reasons"]
        attest(
            db,
            party,
            actor,
            event_date=date(2020, 1, 1),
            external_dates_considered=True,
            government_holds_excluded=True,
        )
        db.commit()
        assert eligibility(db, party, now=future)["eligible"]
        party.city = "Later observed activity"
        db.commit()
        assert "attestation_predates_activity" in eligibility(db, party, now=future)["reasons"]


def test_new_attestation_cannot_lower_previous_external_event(client, admin, eligible_bundle):
    with client.app.state.session_factory() as db:
        actor = db.get(User, admin["user"]["id"])
        party = db.get(Party, eligible_bundle["party_id"])
        known_latest = utcnow().date()
        attest(
            db,
            party,
            actor,
            event_date=known_latest,
            external_dates_considered=True,
            government_holds_excluded=True,
        )
        db.commit()
        attest(
            db,
            party,
            actor,
            event_date=date(2020, 1, 1),
            external_dates_considered=True,
            government_holds_excluded=True,
        )
        db.commit()
        info = eligibility(db, party, now=eligible_bundle["future"])
        assert info["attestation"]["event_date"] == "2020-01-01"
        assert info["latest_attested_event_date"] == known_latest.isoformat()
        assert datetime.fromisoformat(info["basis_at"]) == datetime.combine(
            known_latest, datetime.max.time(), tzinfo=UTC
        )


@pytest.mark.parametrize(
    "reference",
    [
        "queue_anchor_hit_id",
        "prior_disposition_id",
        "suppression_disposition_id",
        "completed_disposition_id",
    ],
)
def test_retained_reference_protects_evidence_without_suppression(
    client, admin, eligible_bundle, reference
):
    with client.app.state.session_factory() as db:
        old = db.get(Hit, eligible_bundle["hit_id"])
        decision = db.scalar(select(Disposition).where(Disposition.hit_id == old.id))
        run = ScreeningRun(
            trigger="manual", csl_version_id=db.get(ScreeningRun, old.run_id).csl_version_id
        )
        db.add(run)
        db.flush()
        retained = Hit(
            run_id=run.id,
            party_id=old.party_id,
            csl_entry_id=old.csl_entry_id,
            party_hash=old.party_hash,
            entry_hash=old.entry_hash,
            det_score=old.det_score,
            model_prob=None,
            final_confidence=old.final_confidence,
            bucket="likely",
            explanation={reference: old.id if reference == "queue_anchor_hit_id" else decision.id},
            party_snapshot=old.party_snapshot,
            entry_snapshot=old.entry_snapshot,
        )
        db.add(retained)
        db.commit()
        attest(
            db,
            db.get(Party, old.party_id),
            db.get(User, admin["user"]["id"]),
            event_date=date(2020, 1, 1),
            external_dates_considered=True,
            government_holds_excluded=True,
        )
        db.commit()
        result = apply_retention(db, now=eligible_bundle["future"])
        assert result["hits"] == 1
        assert db.get(Hit, old.id) is not None
        assert db.get(Disposition, decision.id) is not None
        assert db.get(Hit, retained.id).status == "open"
        assert any(
            row.get("hit_id") == old.id
            and "retained_anchor_or_decision_reference" in row["reasons"]
            for row in result["blocked"]
        )
