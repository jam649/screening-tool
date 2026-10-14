"""Original human decisions are labels; current and system records are not."""

import csv
import hashlib
import io
import json

import pytest
from sqlalchemy import select
from test_retention import eligible_bundle as eligible_bundle
from test_review_api import decide
from test_review_api import review_seed as review_seed

from app.config import DISCLAIMER
from app.models import AuditLog, Hit, Party, User
from app.reports.training import export_training_pairs
from app.retention.service import apply_retention
from app.screening.engine import run_screening
from app.screening.normalize import party_record_hash


def rows(artifact):
    return list(csv.DictReader(io.StringIO(artifact.body.decode("utf-8-sig"))))


@pytest.mark.parametrize("kind,label", [("false_positive", "0"), ("confirmed_match", "1")])
def test_training_human_fanout_original_snapshots_actor_and_repeat(
    client, admin, review_seed, kind, label
):
    decision = decide(client, admin, review_seed["hit_id"], kind, "Actual decision")
    assert decision.status_code == 201 and decision.json()["observations_dispositioned"] == 2
    with client.app.state.session_factory() as db:
        run_screening(db)
        party = db.get(Party, review_seed["party_id"])
        party.name = "Mutable current identity"
        party.record_hash = party_record_hash({"name": party.name})
        db.get(User, admin["user"]["id"]).display_name = "Changed current actor"
        db.commit()
        artifact = export_training_pairs(db, actor_user_id=admin["user"]["id"])
        document = rows(artifact)
        pairs = [row for row in document if row["row_type"] == "training_pair"]
        assert len(pairs) == 2
        assert {row["label"] for row in pairs} == {kind}
        assert {row["label_value"] for row in pairs} == {label}
        assert {row["queue_hit_id"] for row in pairs} == {review_seed["hit_id"]}
        assert len({row["disposition_id"] for row in pairs}) == 2
        assert all(
            json.loads(row["party_snapshot"])["name"] == "Ácme Manufacturing" for row in pairs
        )
        assert all(
            json.loads(row["actor_snapshot"])["display_name"] == "Administrator" for row in pairs
        )
        assert all(
            json.loads(row["entry_snapshot"])["name"] == "Ácme Manufacturing" for row in pairs
        )
        assert len(list(db.scalars(select(Hit)))) == 3
        assert document[-1]["row_type"] == "manifest" and document[-1]["exported_pairs"] == "2"
        assert document[-1]["disclaimer"] == DISCLAIMER and document[-1]["label"] == ""
        assert artifact.sha256 == hashlib.sha256(artifact.body).hexdigest()
        event = db.scalar(select(AuditLog).where(AuditLog.action == "export.created"))
        assert event.after["sha256"] == artifact.sha256
        assert event.after["selection"]["exported_pairs"] == 2


def test_training_empty_and_escalations_are_manifest_only(client, admin, review_seed):
    with client.app.state.session_factory() as db:
        document = rows(export_training_pairs(db))
        assert len(document) == 1 and document[0]["exported_pairs"] == "0"
    assert (
        decide(client, admin, review_seed["hit_id"], "escalated", "Investigate").status_code == 201
    )
    with client.app.state.session_factory() as db:
        assert rows(export_training_pairs(db))[0]["exported_pairs"] == "0"


def test_training_purged_labels_unavailable_not_reconstructed(client, admin, eligible_bundle):
    with client.app.state.session_factory() as db:
        assert apply_retention(db, now=eligible_bundle["future"])["hits"] == 2
        document = rows(export_training_pairs(db))
        assert len(document) == 1 and document[0]["row_type"] == "manifest"
        assert document[0]["exported_pairs"] == "0"
        assert document[0]["unavailable_purged_pairs"] == "2"
        assert "Ácme" not in document[0]["party_snapshot"]


def test_training_does_not_commit_pending_mutable_changes(client, admin, review_seed):
    with client.app.state.session_factory() as db:
        db.get(Party, review_seed["party_id"]).name = "Pending change"
        with pytest.raises(ValueError, match="Commit pending"):
            export_training_pairs(db)


def test_training_system_list_removal_is_not_a_human_label(client, admin, review_seed):
    from app.csl.service import refresh_csl

    with client.app.state.session_factory() as db:
        refresh_csl(
            db,
            data=json.dumps(
                [
                    {
                        "id": "unrelated-replacement",
                        "source": "Entity List (EL) - Bureau of Industry and Security",
                        "name": "Synthetic Unrelated Replacement",
                        "type": "Entity",
                        "alt_names": [],
                        "addresses": [],
                        "programs": [],
                    }
                ]
            ).encode(),
        )
        document = rows(export_training_pairs(db))
        assert len(document) == 1 and document[0]["exported_pairs"] == "0"
        assert document[0]["unavailable_actor_provenance"] == "0"


def test_training_missing_historical_actor_proof_is_explicitly_unavailable(
    client, admin, review_seed
):
    from app.models import Disposition

    with client.app.state.session_factory() as db:
        hit = db.get(Hit, review_seed["hit_id"])
        db.add(
            Disposition(
                hit_id=hit.id,
                kind="confirmed_match",
                actor_user_id=admin["user"]["id"],
                note="Legacy human decision without actor snapshot",
            )
        )
        db.flush()
        hit.status = "dispositioned"
        db.commit()
        document = rows(export_training_pairs(db))
        assert len(document) == 1 and document[0]["exported_pairs"] == "0"
        assert document[0]["unavailable_actor_provenance"] == "1"
