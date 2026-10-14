import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from app.api.review import safe_source_url
from app.csl.service import refresh_csl
from app.models import AuditLog, Disposition, Hit, Party, Setting, Source, Suppression, User
from app.screening.engine import queue_hits, run_screening
from app.screening.normalize import party_record_hash


@pytest.fixture
def review_seed(client, admin):
    with client.app.state.session_factory() as db:
        refresh_csl(
            db,
            data=json.dumps(
                [
                    {
                        "id": "acme",
                        "source": "Entity List (EL) - Bureau of Industry and Security",
                        "name": "Ácme Manufacturing",
                        "type": "Entity",
                        "alt_names": [],
                        "addresses": [{"country": "US"}],
                        "programs": [],
                        "source_list_url": "https://example.com/list",
                    }
                ]
            ).encode(),
        )
        source = Source(kind="csv", label="Fixture CSV")
        db.add(source)
        db.flush()
        party = Party(
            source_id=source.id,
            external_id="acme",
            name="Ácme Manufacturing",
            country_iso2="US",
            record_hash=party_record_hash({"name": "Ácme Manufacturing", "country_iso2": "US"}),
            raw={},
        )
        unrelated = Party(
            source_id=source.id,
            external_id="other",
            name="Zyzzyva Qwerty",
            role="vendor",
            country_iso2="CA",
            record_hash=party_record_hash({"name": "Zyzzyva Qwerty", "country_iso2": "CA"}),
            raw={},
        )
        db.add_all([party, unrelated])
        db.commit()
        first = run_screening(db)
        second = run_screening(db)
        return {
            "party_id": party.id,
            "zero_id": unrelated.id,
            "source_id": source.id,
            "hit_id": queue_hits(db)[0].id,
            "runs": [first.id, second.id],
        }


def decide(client, admin, hit_id, kind="false_positive", note=""):
    return client.post(
        f"/api/v1/hits/{hit_id}/dispositions",
        json={"kind": kind, "note": note},
        headers={"x-csrf-token": admin["csrf_token"]},
    )


def test_queue_snapshots_tokens_and_all_observation_dispositions(client, admin, review_seed):
    assert client.get("/api/v1/queue").json()["total"] == 1
    detail = client.get(f"/api/v1/hits/{review_seed['hit_id']}").json()
    assert detail["explanation"]["matched_tokens"] != "[redacted]"
    assert detail["party_snapshot"]["name"] == "Ácme Manufacturing"
    response = decide(client, admin, review_seed["hit_id"])
    assert response.status_code == 201
    assert response.json()["observations_dispositioned"] == 2
    assert client.get("/api/v1/queue").json()["total"] == 0
    assert decide(client, admin, review_seed["hit_id"]).status_code == 409
    with client.app.state.session_factory() as db:
        assert len(list(db.scalars(select(Disposition)))) == 2
        assert all(h.status == "dispositioned" for h in db.scalars(select(Hit)))
        suppression = db.scalar(select(Suppression))
        assert suppression.disposition_id == response.json()["disposition"]["id"]
        assert all(d.actor_user_id == admin["user"]["id"] for d in db.scalars(select(Disposition)))
        assert (
            len(list(db.scalars(select(AuditLog).where(AuditLog.action == "review.disposition"))))
            == 2
        )


@pytest.mark.parametrize("kind", ["confirmed_match", "escalated"])
def test_notes_required_and_csrf(client, admin, review_seed, kind):
    assert decide(client, admin, review_seed["hit_id"], kind).status_code == 422
    assert (
        client.post(
            f"/api/v1/hits/{review_seed['hit_id']}/dispositions",
            json={"kind": kind, "note": "Reason"},
        ).status_code
        == 403
    )
    assert decide(client, admin, review_seed["hit_id"], kind, "Reason").status_code == 201


def test_changed_hash_prior_actor_and_immutable_history(client, admin, review_seed):
    assert decide(client, admin, review_seed["hit_id"]).status_code == 201
    with client.app.state.session_factory() as db:
        suppressed_run = run_screening(db)
        assert suppressed_run.hits_suppressed == 1
        party = db.get(Party, review_seed["party_id"])
        party.city = "Boston"
        party.record_hash = party_record_hash(
            {"name": party.name, "country_iso2": "US", "city": "Boston"}
        )
        db.commit()
        run_screening(db)
        current = queue_hits(db)[0]
        user = db.get(User, admin["user"]["id"])
        user.display_name = "Renamed Administrator"
        db.commit()
        current_id = current.id
    detail = client.get(f"/api/v1/hits/{current_id}").json()
    assert detail["prior_disposition"]["actor"]["display_name"] == "Administrator"
    assert detail["prior_disposition"]["what_changed"]["party"]["city"] == {
        "before": None,
        "after": "Boston",
    }
    old = client.get(f"/api/v1/hits/{review_seed['hit_id']}").json()
    assert old["party_snapshot"]["city"] is None
    assert old["dispositions"][0]["actor"]["display_name"] == "Administrator"
    assert decide(client, admin, review_seed["hit_id"]).status_code == 409


def test_superseded_context_decision_applies_all_hash_observations(client, admin, review_seed):
    with client.app.state.session_factory() as db:
        db.add(Setting(key="score_weights", value={"country_match": -20}))
        db.commit()
        run_screening(db)
        current_id = queue_hits(db)[0].id
    assert current_id != review_seed["hit_id"]
    assert decide(client, admin, review_seed["hit_id"]).status_code == 409
    response = decide(client, admin, current_id, "confirmed_match", "Reviewed immutable evidence")
    assert response.status_code == 201
    assert response.json()["observations_dispositioned"] == 3


def test_concurrent_dispositions_only_one_succeeds(client, admin, review_seed):
    def submit(kind):
        return decide(client, admin, review_seed["hit_id"], kind, "Concurrency test").status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(submit, ["false_positive", "confirmed_match"]))
    assert sorted(statuses) == [201, 409]
    with client.app.state.session_factory() as db:
        assert len(list(db.scalars(select(Disposition)))) == 2


def test_source_url_rejects_unsafe_credentials():
    for url in (
        "javascript:alert(1)",
        "https://user:secret@example.com",
        "https://example.com?access_token=secret",
        "https://example.com/\n",
    ):
        assert safe_source_url(url) is None
    assert (
        safe_source_url("https://example.com/list?q=public") == "https://example.com/list?q=public"
    )


def test_reviewer_can_decide_but_cannot_administer(client, admin, review_seed):
    from app.security import PASSWORDS

    with client.app.state.session_factory() as db:
        reviewer = User(
            email="reviewer@example.com",
            display_name="Reviewer",
            role="reviewer",
            password_hash=PASSWORDS.hash("reviewer test password long"),
        )
        db.add(reviewer)
        db.commit()
        reviewer_id = reviewer.id
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "reviewer@example.com", "password": "reviewer test password long"},
    )
    assert login.status_code == 200
    response = client.post(
        f"/api/v1/hits/{review_seed['hit_id']}/dispositions",
        json={"kind": "escalated", "note": "Require second investigation"},
        headers={"x-csrf-token": login.json()["csrf_token"]},
    )
    assert response.status_code == 201
    assert response.json()["disposition"]["actor_user_id"] == reviewer_id
    assert client.get("/api/v1/settings").status_code == 403


def test_federal_register_source_takes_precedence_and_audit_actor_is_frozen(
    client, admin, review_seed
):
    import json

    from app.models import CSLEntry

    with client.app.state.session_factory() as db:
        raw = db.scalar(select(CSLEntry)).raw
        refresh_csl(
            db,
            data=json.dumps(
                [
                    raw
                    | {
                        "federal_register_notice": "https://example.com/federal-notice",
                        "source_list_url": "https://example.com/list",
                    }
                ]
            ).encode(),
        )
        run_screening(db)
        # A metadata-only list update leaves the original immutable queue anchor;
        # a fresh detail still reads its historically captured original source URL.
        newest = db.scalar(select(Hit).order_by(Hit.created_at.desc()))
        newest_id = newest.id
        user = db.get(User, admin["user"]["id"])
        user.display_name = "Later Name"
        db.commit()
    detail = client.get(f"/api/v1/hits/{newest_id}").json()
    assert detail["source_url"] == "https://example.com/federal-notice"
    audit = client.get("/api/v1/audit-log", params={"action": "sources.create"}).json()
    # Fixture inserts are system events; attributed setup evidence uses recorded identity.
    setup = client.get(
        "/api/v1/audit-log", params={"action": "setup.disclaimer_acknowledged"}
    ).json()
    assert setup["items"][0]["actor"]["display_name"] == "Administrator"
    assert audit["total"] == 1


@pytest.mark.parametrize("kind", ["confirmed_match", "escalated"])
def test_identical_repeats_expose_completed_human_reference_without_new_decision(
    client, admin, review_seed, kind
):
    response = decide(client, admin, review_seed["hit_id"], kind, "Recorded human assessment")
    assert response.status_code == 201
    original_id = response.json()["disposition"]["id"]
    with client.app.state.session_factory() as db:
        repeated = run_screening(db)
        observation_id = db.scalar(select(Hit.id).where(Hit.run_id == repeated.id))
        assert len(list(db.scalars(select(Disposition)))) == 2
    detail = client.get(f"/api/v1/hits/{observation_id}").json()
    assert detail["completed_disposition"]["kind"] == kind
    # Repeated observations can reference any of the matching original per-observation
    # decisions, all of which retain the real human and note.
    assert detail["completed_disposition"]["actor_user_id"] == admin["user"]["id"]
    assert detail["completed_disposition"]["note"] == "Recorded human assessment"
    assert detail["dispositions"] == []
    assert original_id
    assert client.get("/api/v1/queue").json()["total"] == 0
