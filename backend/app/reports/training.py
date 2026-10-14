"""Human-only training labels from retained original comparison evidence."""

import csv
import hashlib
import io
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.audit import audit
from app.config import DISCLAIMER
from app.models import AuditLog, Disposition, Hit, RetentionPurgeCertificate, ScreeningRun, utcnow
from app.reports.export import ExportArtifact, _safe, csv_cell

COLUMNS = [
    "row_type",
    "schema_version",
    "label",
    "label_value",
    "disposition_id",
    "hit_id",
    "queue_hit_id",
    "run_id",
    "party_id",
    "csl_entry_id",
    "party_hash",
    "entry_hash",
    "screened_at",
    "decided_at",
    "actor_snapshot",
    "party_snapshot",
    "entry_snapshot",
    "exported_pairs",
    "unavailable_purged_pairs",
    "unavailable_actor_provenance",
    "disclaimer",
]


def export_training_pairs(db: Session, *, actor_user_id: str | None = None) -> ExportArtifact:
    if db.new or db.dirty or db.deleted:
        raise ValueError("Commit pending changes before exporting training evidence")
    db.rollback()
    # One consistent read view; the audit write begins only after serialization.
    db.execute(text("BEGIN"))
    as_of = utcnow().isoformat()
    events = {
        row.target_id: row
        for row in db.scalars(select(AuditLog).where(AuditLog.action == "review.disposition"))
    }
    records: list[dict[str, Any]] = []
    missing_actor = 0
    for decision, hit, run in db.execute(
        select(Disposition, Hit, ScreeningRun)
        .join(Hit, Disposition.hit_id == Hit.id)
        .join(ScreeningRun, Hit.run_id == ScreeningRun.id)
        .where(
            Disposition.kind.in_(["false_positive", "confirmed_match"]),
            Disposition.actor_user_id.is_not(None),
        )
        .order_by(Disposition.created_at, Disposition.id)
    ):
        event = events.get(decision.id)
        proof = event.after or {} if event is not None else {}
        actor = proof.get("actor")
        if (
            event is None
            or event.actor_user_id != decision.actor_user_id
            or not isinstance(actor, dict)
            or actor.get("id") != decision.actor_user_id
            or proof.get("hit_id") != hit.id
            or proof.get("kind") != decision.kind
            or not isinstance(proof.get("queue_hit_id"), str)
        ):
            missing_actor += 1
            continue
        records.append(
            _safe(
                {
                    "row_type": "training_pair",
                    "schema_version": 1,
                    "label": decision.kind,
                    "label_value": int(decision.kind == "confirmed_match"),
                    "disposition_id": decision.id,
                    "hit_id": hit.id,
                    "queue_hit_id": proof["queue_hit_id"],
                    "run_id": run.id,
                    "party_id": hit.party_id,
                    "csl_entry_id": hit.csl_entry_id,
                    "party_hash": hit.party_hash,
                    "entry_hash": hit.entry_hash,
                    "screened_at": run.started_at,
                    "decided_at": decision.created_at,
                    "actor_snapshot": actor,
                    "party_snapshot": hit.party_snapshot,
                    "entry_snapshot": hit.entry_snapshot,
                }
            )
        )
    purged_ids: set[str] = set()
    for certificate in db.scalars(select(RetentionPurgeCertificate)):
        for decision in certificate.evidence.get("dispositions", []):
            if (
                decision.get("kind") in {"false_positive", "confirmed_match"}
                and decision.get("actor_user_id") is not None
                and isinstance(decision.get("id"), str)
            ):
                purged_ids.add(decision["id"])
    counts = {
        "exported_pairs": len(records),
        "unavailable_purged_pairs": len(purged_ids),
        "unavailable_actor_provenance": missing_actor,
    }
    records.append(
        {"row_type": "manifest", "schema_version": 1, **counts, "disclaimer": DISCLAIMER}
    )
    output = io.StringIO(newline="")
    writer = csv.writer(output, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(COLUMNS)
    for record in records:
        writer.writerow([csv_cell(record.get(column)) for column in COLUMNS])
    body = output.getvalue().encode("utf-8-sig")
    artifact = ExportArtifact(
        "training-pairs.csv",
        "text/csv; charset=utf-8",
        hashlib.sha256(body).hexdigest(),
        len(body),
        body=body,
    )
    db.rollback()
    audit(
        db,
        "export.created",
        "exports",
        after={
            "selection": {"kind": "human_training_pairs", **counts},
            "format": artifact.media_type,
            "filename": artifact.filename,
            "sha256": artifact.sha256,
            "bytes": artifact.size,
            "schema_version": 1,
            "evidence_as_of": as_of,
        },
        actor_user_id=actor_user_id,
    )
    db.commit()
    return artifact
