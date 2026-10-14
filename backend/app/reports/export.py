"""Consistent, sanitized audit artifacts; originals remain authoritative in JSON.

Party artifacts project shared scopes; run exports and ZIP indexes retain full manifests.
Only the returned ZIP artifact owns a private temporary directory. Call close() after use.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import tempfile
import unicodedata
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from fontTools.ttLib import TTFont  # type: ignore[import-untyped]
from fpdf import FPDF
from fpdf.enums import WrapMode
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.parties import filter_party_rows, party_rows, source_payload
from app.api.review import historical_actor, safe_source_url
from app.audit import PUBLIC_SETTING_KEYS, SECRET_KEY_MARKERS, audit
from app.canonical import HASH_RULE_VERSION, canonical_hash
from app.config import DISCLAIMER, PRODUCT_NAME
from app.models import (
    AuditLog,
    CSLEntryRevision,
    CSLFetch,
    CSLVersion,
    Disposition,
    Hit,
    Party,
    RetentionAttestation,
    RetentionHold,
    RetentionHoldRelease,
    RetentionPurgeCertificate,
    RunPartyEvidence,
    ScreeningRun,
    Setting,
    Source,
    Suppression,
    utcnow,
)
from app.screening.normalize import party_record_hash

Format = Literal["pdf", "csv"]
SCHEMA = 1
FONT_DIR = Path(__file__).with_name("fonts")
FONT_FILES = {
    "ScreeningLatin": "NotoSans-Regular.ttf",
    "ScreeningArabic": "NotoSansArabic-Regular.ttf",
    "ScreeningHindi": "NotoSansDevanagari-Regular.ttf",
    "ScreeningCJK": "ScreeningCJK-Regular.ttf",
}


class ExportNotFound(ValueError):
    """The requested party or run does not exist."""


@dataclass
class ExportArtifact:
    filename: str
    media_type: str
    sha256: str
    size: int
    body: bytes | None = None
    path: Path | None = None
    _directory: tempfile.TemporaryDirectory | None = None

    def close(self) -> None:
        if self._directory is not None:
            self._directory.cleanup()
            self._directory = None


def _json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _safe(value: Any, *, explanation: bool = False) -> Any:
    """Redact private fields before any serializer sees the evidence."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            clean = "".join(c for c in key.casefold() if c.isalnum())
            trusted_tokens = explanation and key == "matched_tokens"
            legal_reference = key == "authorization_reference"
            # Genuine scorer name ratios use 0..100; ordinary weights use 0..1.
            public_weight = (
                key in {"token_set", "token_sort"}
                and isinstance(item, (int, float))
                and not isinstance(item, bool)
                and 0 <= item <= (100 if explanation else 1)
                and math.isfinite(item)
            )
            if (
                (
                    any(marker in clean for marker in SECRET_KEY_MARKERS)
                    and not trusted_tokens
                    and not legal_reference
                    and not public_weight
                )
                or (key in {"cursor", "last_error", "error", "error_detail"})
                or key.endswith("_enc")
            ):
                result[key] = "[redacted]" if item is not None else None
            elif (key.endswith("_url") or key in {"url", "source_url"}) and isinstance(item, str):
                result[key] = safe_source_url(item)
            else:
                result[key] = _safe(item, explanation=explanation)
        if (
            result.get("key") is not None
            and "value" in result
            and (result.get("encrypted") or result["key"] not in PUBLIC_SETTING_KEYS)
        ):
            result["value"] = "[redacted]"
        return result
    if isinstance(value, (list, tuple)):
        return [_safe(item, explanation=explanation) for item in value]
    return value


def _row(record: Any) -> dict[str, Any]:
    result = _safe({c.key: getattr(record, c.key) for c in record.__mapper__.column_attrs})
    if isinstance(record, Hit):
        result["explanation"] = _safe(record.explanation, explanation=True)
    elif isinstance(record, RetentionPurgeCertificate):
        # Certificate comparisons were captured from genuine immutable Hit records.
        for sanitized, original in zip(
            result["evidence"].get("hits", []), record.evidence.get("hits", []), strict=True
        ):
            sanitized["explanation"] = _safe(original.get("explanation", {}), explanation=True)
    return result


def _internal_id(value: str) -> str:
    # Never use party names or external IDs as path components, even for legacy IDs.
    try:
        return str(UUID(value))
    except ValueError:
        return hashlib.sha256(value.encode()).hexdigest()


def csv_cell(value: Any) -> str:
    if isinstance(value, (dict, list)):
        value = _json(value).decode()
    result = "" if value is None else str(value)
    candidate = result.lstrip()
    candidate = "".join(c for c in candidate if unicodedata.category(c).startswith("C") is False)
    candidate = candidate.lstrip()
    if candidate.startswith(("=", "+", "-", "@")):
        result = "'" + result
    return result


def _csv(records: Any, columns: list[str]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(columns)
    for record in records:
        writer.writerow([csv_cell(record.get(column)) for column in columns])
    writer.writerow(["ITA disclaimer", DISCLAIMER] + [""] * max(0, len(columns) - 2))
    return output.getvalue().encode("utf-8-sig")


class _Evidence:
    """One transaction's indexes, reused across all files in a full export."""

    def __init__(self, db: Session):
        self.db = db
        self.as_of = utcnow().isoformat()
        db.info["export_as_of"] = self.as_of
        self.parties = {p.id: p for p in db.scalars(select(Party))}
        self.sources = {s.id: s for s in db.scalars(select(Source))}
        self.runs = {
            r.id: r
            for r in db.scalars(select(ScreeningRun).order_by(ScreeningRun.started_at.desc()))
        }
        self.scope_hash_cache: dict[int, str] = {}
        self.membership_cache: dict[int, frozenset[str]] = {}
        self.scope_digests = {
            r.id: canonical_hash(r.settings_snapshot.get("scope") or {}) for r in self.runs.values()
        }
        self.versions = {v.id: _row(v) for v in db.scalars(select(CSLVersion))}
        self.fetches: dict[str, list[dict]] = defaultdict(list)
        for fetch in db.scalars(select(CSLFetch)):
            self.fetches[fetch.version_id or ""].append(_row(fetch))
        self.hits: dict[str, list[Hit]] = defaultdict(list)
        self.run_members: dict[str, set[str]] = defaultdict(set)
        for hit in db.scalars(select(Hit).order_by(Hit.created_at, Hit.id)):
            self.hits[hit.party_id].append(hit)
            self.run_members[hit.run_id].add(hit.party_id)
        self.participation: dict[tuple[str, str], RunPartyEvidence] = {}
        for evidence in db.scalars(select(RunPartyEvidence)):
            self.participation[(evidence.run_id, evidence.party_id)] = evidence
            self.run_members[evidence.run_id].add(evidence.party_id)
        for run in self.runs.values():
            scope = run.settings_snapshot.get("scope") or {}
            self.run_members[run.id].update(scope.get("party_ids") or [])
        self.party_runs: dict[str, list[str]] = defaultdict(list)
        self.dispositions: dict[str, list[dict]] = defaultdict(list)
        self.decisions: dict[str, dict] = {}
        self.audit_by_target: dict[str, list[AuditLog]] = defaultdict(list)
        self.settings_audit: list[AuditLog] = []
        self.task_run_targets: dict[str, set[str]] = defaultdict(set)
        self.task_party_targets: dict[str, set[str]] = defaultdict(set)
        self.review_actors: dict[str, Any] = {}
        # Audit rows are indexed once, not scanned for each of 20,000 party files.
        for event in db.scalars(select(AuditLog).order_by(AuditLog.created_at, AuditLog.id)):
            if event.target_id:
                self.audit_by_target[event.target_id].append(event)
            if event.target_table == "settings":
                self.settings_audit.append(event)
            if event.action == "review.disposition":
                self.review_actors[event.target_id or ""] = (event.after or {}).get("actor")
            if event.target_table == "screening_tasks":
                for state in (event.before, event.after):
                    if not state:
                        continue
                    if state.get("run_id"):
                        self.task_run_targets[state["run_id"]].add(event.target_id or "")
                    for party_id in state.get("party_ids") or []:
                        self.task_party_targets[party_id].add(event.target_id or "")
        for disposition in db.scalars(select(Disposition).order_by(Disposition.created_at)):
            payload = _row(disposition)
            payload["actor"] = _safe(
                self.review_actors.get(disposition.id)
                or historical_actor(db, disposition.actor_user_id, disposition.created_at)
            )
            self.dispositions[disposition.hit_id].append(payload)
            self.decisions[disposition.id] = payload
        self.suppressions: dict[str, list[dict]] = defaultdict(list)
        for suppression in db.scalars(select(Suppression)):
            payload = _row(suppression)
            payload["original_decision"] = self.decisions.get(suppression.disposition_id)
            self.suppressions[suppression.party_id].append(payload)
        self.purged: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for certificate in db.scalars(select(RetentionPurgeCertificate)):
            # Same actual-certificate contract as retention.service.purged_run_evidence.
            proof = _row(certificate)["evidence"]
            for hit in proof.get("hits", []):
                self.purged[(hit["run_id"], certificate.party_id)].append(
                    {
                        "certificate_id": certificate.id,
                        "certificate_sha256": certificate.sha256,
                        "hit": hit,
                        "dispositions": [
                            d for d in proof.get("dispositions", []) if d["hit_id"] == hit["id"]
                        ],
                    }
                )
                self.run_members[hit["run_id"]].add(certificate.party_id)
        self.retention: dict[str, list[dict]] = defaultdict(list)
        holds = {row.id: row for row in db.scalars(select(RetentionHold))}
        for row in db.scalars(select(RetentionAttestation)):
            self.retention[row.party_id].append({"table": row.__tablename__, **_row(row)})
        for hold_row in holds.values():
            self.retention[hold_row.party_id or ""].append(
                {"table": hold_row.__tablename__, **_row(hold_row)}
            )
        for release in db.scalars(select(RetentionHoldRelease)):
            hold = holds.get(release.hold_id)
            if hold:
                self.retention[hold.party_id or ""].append(
                    {"table": release.__tablename__, **_row(release)}
                )
        for certificate in db.scalars(select(RetentionPurgeCertificate)):
            self.retention[certificate.party_id].append(
                {"table": certificate.__tablename__, **_row(certificate)}
            )
        for run_id in self.runs:
            for party_id in self.run_members[run_id]:
                self.party_runs[party_id].append(run_id)
        display = db.scalar(select(Setting).where(Setting.key == "display_name"))
        self.display_name = (
            display.value if display and isinstance(display.value, str) else PRODUCT_NAME
        )

    def base(self, kind: str, selection: dict) -> dict:
        return {
            "schema_version": SCHEMA,
            "hash_rule_version_current": HASH_RULE_VERSION,
            "exported_at": self.as_of,
            "display_name": self.display_name,
            "kind": kind,
            "selection": selection,
            "disclaimer": DISCLAIMER,
        }

    def scope_hash(self, scope: dict) -> str:
        if id(scope) not in self.scope_hash_cache:
            self.scope_hash_cache[id(scope)] = canonical_hash(scope)
        return self.scope_hash_cache[id(scope)]

    def project(self, value: Any, party_id: str | None, run_id: str | None = None) -> Any:
        if party_id is None:
            return _safe(value)
        if isinstance(value, list):
            return [self.project(x, party_id, run_id) for x in value]
        if not isinstance(value, dict):
            return _safe(value)
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key == "scope" and isinstance(item, dict):
                result[key] = {
                    "party_count": len(item["party_ids"])
                    if isinstance(item.get("party_ids"), list)
                    else None,
                    "entry_count": len(item["entry_ids"])
                    if isinstance(item.get("entry_ids"), list)
                    else None,
                    "party_id": party_id,
                    "party_hash": (item.get("party_hashes") or {}).get(party_id),
                    "scope_sha256": self.scope_digests.get(run_id or "") or self.scope_hash(item),
                    "full_manifest": f"runs/{_internal_id(run_id)}.json" if run_id else None,
                }
            elif key in {"party_ids", "entry_ids", "hit_ids", "disposition_ids"} and isinstance(
                item, list
            ):
                result[key + "_count"] = len(item)
                if key == "party_ids":
                    members = self.membership_cache.get(id(item))
                    if members is None:
                        members = self.membership_cache[id(item)] = frozenset(item)
                    result[key] = [party_id] if party_id in members else []
            elif key in {"party_hashes", "entry_hashes"} and isinstance(item, dict):
                result[key + "_count"] = len(item)
                if key == "party_hashes":
                    result[key] = {party_id: item[party_id]} if party_id in item else {}
            else:
                result[key] = self.project(item, party_id, run_id)
        return _safe(result)

    def audit_payload(self, event: AuditLog, party_id: str | None = None) -> dict:
        # Do not copy full run/task scopes before projecting a party's audit rows.
        result = _safe(
            {
                c.key: getattr(event, c.key)
                for c in event.__mapper__.column_attrs
                if c.key not in {"before", "after"}
            }
        )
        result["actor"] = _safe(
            (event.after or {}).get("actor")
            or historical_actor(self.db, event.actor_user_id, event.created_at)
        )
        run_id = event.target_id if event.target_table == "screening_runs" else None
        if event.target_table == "screening_tasks":
            run_id = (event.after or {}).get("run_id")
        result["before"] = self.project(event.before, party_id, run_id)
        result["after"] = self.project(event.after, party_id, run_id)
        if event.target_table == "hits":
            for key in ("before", "after"):
                original = getattr(event, key)
                if isinstance(original, dict) and isinstance(original.get("explanation"), dict):
                    result[key]["explanation"] = _safe(original["explanation"], explanation=True)
        return result

    def run(self, run_id: str, party_id: str | None = None) -> dict:
        run = self.runs[run_id]
        # Project the original scope directly; no per-party copy of the full book.
        result = _safe(
            {
                c.key: getattr(run, c.key)
                for c in run.__mapper__.column_attrs
                if c.key != "settings_snapshot"
            }
        )
        result["settings_snapshot"] = self.project(run.settings_snapshot, party_id, run_id)
        result["scope_sha256"] = self.scope_digests[run_id]
        result["list_version"] = self.versions.get(run.csl_version_id)
        result["fetches"] = self.fetches[run.csl_version_id]
        result["disclaimer"] = DISCLAIMER
        return result

    def historical_party(self, run: ScreeningRun, party_id: str, hits: list[dict]) -> dict:
        row = self.participation.get((run.id, party_id))
        if row:
            return {"origin": "run_party_evidence", **_row(row)}
        if hits:
            return {"origin": "hit_snapshot", "party_snapshot": hits[0]["party_snapshot"]}
        expected = (run.settings_snapshot.get("scope") or {}).get("party_hashes", {}).get(party_id)
        for event in reversed(self.audit_by_target.get(party_id, [])):
            if event.target_table != "parties" or event.action not in {
                "parties.create",
                "parties.update",
            }:
                continue
            if event.created_at > run.started_at or not event.after:
                continue
            snapshot = event.after
            rule = run.settings_snapshot.get("hash_rule_version", 1)
            if (
                expected
                and snapshot.get("record_hash") == expected
                and (rule != HASH_RULE_VERSION or party_record_hash(snapshot) == expected)
            ):
                return {
                    "origin": "matching_prior_party_audit",
                    "audit_id": event.id,
                    "party_hash": expected,
                    "party_snapshot": _safe(snapshot),
                }
            # A later prior update is authoritative even if it no longer matches.
            break
        return {
            "origin": "unavailable",
            "party_hash": expected,
            "historical_fields_unavailable": True,
            "notice": (
                "Historical party fields unavailable; recorded membership and hashes retained."
            ),
        }

    def comparisons(self, party_id: str, run_id: str) -> list[dict]:
        result = []
        for hit in self.hits[party_id]:
            if hit.run_id != run_id:
                continue
            payload = _row(hit)
            payload["source_url"] = safe_source_url(hit.entry_snapshot.get("fr_notice_url")) or (
                safe_source_url(hit.entry_snapshot.get("source_list_url"))
            )
            payload["dispositions"] = self.dispositions[hit.id]
            for key in (
                "prior_disposition_id",
                "suppression_disposition_id",
                "completed_disposition_id",
            ):
                payload[key.removesuffix("_id")] = self.decisions.get(
                    hit.explanation.get(key) or ""
                )
            result.append(payload)
        return result

    def party(self, party_id: str) -> dict:
        party = self.parties[party_id]
        result = self.base("party", {"party_id": party_id})
        result["current_record"] = {
            "label": "Current record as of export",
            "party": _row(party),
            "source": _safe(source_payload(self.sources.get(party.source_id))),
        }
        events, targets = [], {party_id, party.source_id}
        for run_id in self.party_runs[party_id]:
            run = self.runs[run_id]
            comparisons = self.comparisons(party_id, run_id)
            purged = self.purged[(run_id, party_id)]
            historical_hits = comparisons + [row["hit"] for row in purged]
            events.append(
                {
                    "run": self.run(run_id, party_id),
                    "participation": self.historical_party(run, party_id, historical_hits),
                    "comparisons": comparisons,
                    "purged_comparisons": purged,
                    "zero_hits": run.finished_at is not None and not comparisons and not purged,
                }
            )
            targets.update({run_id, run.csl_version_id})
            targets.update(self.task_run_targets[run_id])
            evidence = self.participation.get((run_id, party_id))
            if evidence:
                targets.add(evidence.id)
                if evidence.source_snapshot.get("id"):
                    targets.add(evidence.source_snapshot["id"])
            targets.update(f["id"] for f in self.fetches[run.csl_version_id])
            for comparison in historical_hits:
                targets.update({comparison["id"], comparison["csl_entry_id"]})
                if comparison.get("party_snapshot", {}).get("source_id"):
                    targets.add(comparison["party_snapshot"]["source_id"])
                targets.update(d["id"] for d in self.dispositions[comparison["id"]])
                for key in (
                    "prior_disposition_id",
                    "suppression_disposition_id",
                    "completed_disposition_id",
                ):
                    reference = comparison.get("explanation", {}).get(key)
                    if reference:
                        targets.add(reference)
            for proof in purged:
                targets.add(proof["certificate_id"])
                targets.update(d["id"] for d in proof["dispositions"])
        result["events"] = events
        result["suppressions"] = self.suppressions[party_id]
        result["retention_evidence"] = self.retention[party_id] + self.retention[""]
        targets.update(row["id"] for row in result["retention_evidence"])
        targets.update(s["id"] for s in self.suppressions[party_id])
        for suppression in self.suppressions[party_id]:
            targets.add(suppression["disposition_id"])
        targets.update(self.task_party_targets[party_id])
        audit_rows = {row.id: row for row in self.settings_audit}
        for target in targets:
            audit_rows.update((row.id, row) for row in self.audit_by_target.get(target, []))
        result["audit_log"] = [
            self.audit_payload(row, party_id)
            for row in sorted(audit_rows.values(), key=lambda row: (row.created_at, row.id))
        ]
        return result

    def run_document(self, run_id: str) -> dict:
        result = self.base("run", {"run_id": run_id})
        result["run"] = self.run(run_id)
        result["parties"] = []
        targets = {run_id, self.runs[run_id].csl_version_id}
        targets.update(self.task_run_targets[run_id])
        targets.update(f["id"] for f in self.fetches[self.runs[run_id].csl_version_id])
        for party_id in sorted(self.run_members[run_id]):
            comparisons = self.comparisons(party_id, run_id)
            purged = self.purged[(run_id, party_id)]
            result["parties"].append(
                {
                    "party_id": party_id,
                    "participation": self.historical_party(
                        self.runs[run_id], party_id, comparisons + [row["hit"] for row in purged]
                    ),
                    "comparisons": comparisons,
                    "purged_comparisons": purged,
                    "zero_hits": self.runs[run_id].finished_at is not None
                    and not comparisons
                    and not purged,
                }
            )
            targets.add(party_id)
            if party_id in self.parties:
                targets.add(self.parties[party_id].source_id)
            targets.update(row["id"] for row in self.retention[party_id] + self.retention[""])
            for suppression in self.suppressions[party_id]:
                targets.update({suppression["id"], suppression["disposition_id"]})
            if (run_id, party_id) in self.participation:
                participation = self.participation[(run_id, party_id)]
                targets.add(participation.id)
                if participation.source_snapshot.get("id"):
                    targets.add(participation.source_snapshot["id"])
            for hit in comparisons:
                targets.update({hit["id"], hit["csl_entry_id"]})
                targets.update(d["id"] for d in hit["dispositions"])
                if hit.get("party_snapshot", {}).get("source_id"):
                    targets.add(hit["party_snapshot"]["source_id"])
                for key in (
                    "prior_disposition_id",
                    "suppression_disposition_id",
                    "completed_disposition_id",
                ):
                    if hit.get("explanation", {}).get(key):
                        targets.add(hit["explanation"][key])
            for proof in purged:
                targets.add(proof["certificate_id"])
                targets.update({proof["hit"]["id"], proof["hit"]["csl_entry_id"]})
                targets.update(d["id"] for d in proof["dispositions"])
        rows = {row.id: row for row in self.settings_audit}
        for target in targets:
            rows.update((row.id, row) for row in self.audit_by_target.get(target, []))
        result["audit_log"] = [
            self.audit_payload(row)
            for row in sorted(rows.values(), key=lambda row: (row.created_at, row.id))
        ]
        return result


def _begin_snapshot(db: Session) -> None:
    # sqlite3 legacy transaction mode does not begin for SELECT. Explicit BEGIN pins
    # the WAL snapshot across the entire artifact without acquiring a writer lock.
    if db.new or db.dirty or db.deleted:
        raise ValueError("Commit pending changes before exporting")
    db.rollback()
    db.connection().exec_driver_sql("BEGIN")
    db.info["export_as_of"] = utcnow().isoformat()


def _start(db: Session) -> _Evidence:
    _begin_snapshot(db)
    return _Evidence(db)


def _record(
    db: Session, artifact: ExportArtifact, selection: dict, actor_user_id: str | None
) -> None:
    # End the long read snapshot before the short audit write (no WAL upgrade race).
    db.rollback()
    audit(
        db,
        "export.created",
        "exports",
        after={
            "selection": selection,
            "format": artifact.media_type,
            "filename": artifact.filename,
            "sha256": artifact.sha256,
            "bytes": artifact.size,
            "schema_version": SCHEMA,
            "evidence_as_of": db.info.get("export_as_of"),
        },
        actor_user_id=actor_user_id,
    )
    db.commit()


def _artifact(body: bytes, filename: str, media_type: str) -> ExportArtifact:
    return ExportArtifact(
        filename, media_type, hashlib.sha256(body).hexdigest(), len(body), body=body
    )


def _evidence_csv(document: dict) -> bytes:
    records: list[dict[str, Any]] = []
    # Include every field in structured rows; JSON is exact except spreadsheet prefix defense.
    for key, value in document.items():
        if key == "disclaimer":
            continue
        if isinstance(value, list):
            records.extend(
                {"section": key, "index": i, "evidence": item} for i, item in enumerate(value)
            )
        else:
            records.append({"section": key, "index": "", "evidence": value})
    return _csv(records, ["section", "index", "evidence"])


def _pdf(document: dict) -> bytes:
    coverage: set[int] = set()
    for filename in FONT_FILES.values():
        font = TTFont(FONT_DIR / filename, lazy=True)
        coverage.update(font.getBestCmap() or {})
        font.close()

    def visible(value: str) -> str:
        # Explicitly render controls too; joiners needed for shaping are preserved.
        return "".join(
            c if ord(c) in coverage or c in {"\n", "\u200c", "\u200d"} else f"[U+{ord(c):04X}]"
            for c in value
        )

    class AuditPDF(FPDF):
        def footer(self):
            self.set_y(-42)
            self.set_font("ScreeningLatin", size=7)
            self.multi_cell(w=0, h=3.1, text=DISCLAIMER, new_x="LMARGIN", new_y="NEXT")
            self.cell(w=0, h=4, text=f"Page {self.page_no()}")

    pdf = AuditPDF()
    pdf.set_margins(14, 14, 14)
    pdf.set_auto_page_break(auto=True, margin=47)
    for family, filename in FONT_FILES.items():
        pdf.add_font(family, fname=str(FONT_DIR / filename))
    pdf.set_font("ScreeningLatin", size=10)
    pdf.set_fallback_fonts(["ScreeningArabic", "ScreeningHindi", "ScreeningCJK"])
    pdf.set_text_shaping(True, features={"liga": False, "rlig": False, "dlig": False})
    pdf.add_page()
    pdf.set_title(visible(f"{document['display_name']}: {document['kind']} audit export"))
    pdf.multi_cell(
        w=0,
        h=7,
        text=visible(f"{document['display_name']}: {document['kind']} audit export"),
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.multi_cell(
        w=0,
        h=5,
        text=(
            "Exact sanitized original evidence is attached as evidence.json. "
            "Unsupported glyphs appear as [U+codepoint]."
        ),
        new_x="LMARGIN",
        new_y="NEXT",
    )

    def line(text: str, *, size: int = 10, link: str = "") -> None:
        pdf.set_font("ScreeningLatin", size=size)
        pdf.multi_cell(
            w=0,
            h=5.2 if size <= 10 else 7,
            text=visible(text),
            new_x="LMARGIN",
            new_y="NEXT",
            wrapmode=WrapMode.WORD,
            align="L",
            link=link,
        )

    def heading(text: str) -> None:
        if pdf.will_page_break(21):
            pdf.add_page()
        pdf.ln(3)
        line(text, size=13)

    def fields(record: dict, *, label: str) -> None:
        line(label, size=11)
        for key in (
            "name",
            "alt_names",
            "party_type",
            "entity_type",
            "role",
            "external_id",
            "address_lines",
            "addresses",
            "city",
            "region",
            "postal_code",
            "country_iso2",
            "countries",
            "source_list",
            "csl_id",
            "programs",
            "remarks",
        ):
            value = record.get(key)
            if value not in (None, "", []):
                if isinstance(value, (dict, list)):
                    value = _json(value).decode()
                line(f"{key.replace('_', ' ').capitalize()}: {value}")

    def decision(record: dict | None, label: str) -> None:
        if not record:
            return
        actor = record.get("actor") or {}
        identity = actor.get("display_name") or (
            "System" if record.get("actor_user_id") is None else "Historical identity unavailable"
        )
        line(f"{label}: {record.get('kind')} | {record.get('created_at')} | {identity}")
        line(f"Decision ID: {record.get('id')} | Actor ID: {record.get('actor_user_id')}")
        if record.get("note"):
            line(f"Note: {record['note']}")
        if record.get("slider_value_at_time") is not None:
            line(f"Auto-clear threshold at time: {record['slider_value_at_time']}")

    def comparison(hit: dict, *, purged: dict | None = None) -> None:
        heading(
            "Purged comparison — original certificate evidence"
            if purged
            else (
                "Previously cleared pairing" if hit.get("status") == "suppressed" else "Comparison"
            )
        )
        line(f"Hit: {hit['id']} | Recorded: {hit.get('created_at')} | Status: {hit.get('status')}")
        fields(hit.get("party_snapshot", {}), label="Historical party input")
        fields(hit.get("entry_snapshot", {}), label="Historical list input")
        line(f"Party hash: {hit.get('party_hash')} | Entry hash: {hit.get('entry_hash')}")
        line(
            f"Deterministic score: {hit.get('det_score')} | Model probability: "
            f"{hit.get('model_prob')} | Confidence: {hit.get('final_confidence')} | "
            f"Bucket: {hit.get('bucket')}"
        )
        if hit.get("source_url"):
            line(f"Source: {hit['source_url']}", link=hit["source_url"])
        explanation = hit.get("explanation") or {}
        if explanation:
            line("Matching explanation: " + _json(explanation).decode())
        for row in purged.get("dispositions", []) if purged else hit.get("dispositions", []):
            decision(row, "Original disposition")
        for key, label in (
            ("suppression_disposition", "Original false-positive decision"),
            ("prior_disposition", "Prior decision before changed inputs"),
            ("completed_disposition", "Original governing decision"),
        ):
            decision(hit.get(key), label)
        if purged:
            line(
                f"Purge certificate: {purged['certificate_id']} | "
                f"SHA-256: {purged['certificate_sha256']}"
            )

    def run_summary(run: dict) -> None:
        heading(f"Screening run {run['id']}")
        line(
            f"Trigger: {run['trigger']} | Started: {run['started_at']} | "
            f"Finished: {run.get('finished_at') or 'unfinished'}"
        )
        line(
            f"Parties screened: {run['parties_screened']} | New observations: {run['hits_new']} | "
            f"Suppressed: {run['hits_suppressed']} | Auto-cleared: {run['auto_cleared']}"
        )
        settings = run.get("settings_snapshot") or {}
        scope = settings.get("scope") or {}
        party_count = scope.get(
            "party_count",
            len(scope["party_ids"]) if isinstance(scope.get("party_ids"), list) else "unavailable",
        )
        entry_count = scope.get(
            "entry_count",
            len(scope["entry_ids"]) if isinstance(scope.get("entry_ids"), list) else "unavailable",
        )
        line(f"Recorded scope: {party_count} parties; {entry_count} list entries")
        line(f"Scope SHA-256: {run.get('scope_sha256')}")
        line(
            "Exact membership and hash manifests are retained in "
            "the evidence attachment/shared run index."
        )
        line(
            f"Hash rule: {settings.get('hash_rule_version', 1)} | "
            f"Model: {run.get('model_name') or 'deterministic-only'}"
        )
        line(
            f"Frozen scoring policy: candidate {settings.get('candidate_min')}; "
            f"review {settings.get('review_threshold')}; "
            f"likely {settings.get('likely_threshold')}; "
            f"deterministic weight {settings.get('w_det', 'unavailable')}; "
            f"model weight {settings.get('w_model', 'unavailable')}"
        )
        line("Frozen score weights:")
        for name, weight in sorted((settings.get("score_weights") or {}).items()):
            line(f"  {name.replace('_', ' ').capitalize()}: {weight}")
        line(
            f"Auto-clear: {settings.get('auto_clear_enabled', False)}; "
            f"threshold {settings.get('auto_clear_threshold')}. "
            "Exact normalization rules and policy context are in evidence.json."
        )
        version = run.get("list_version") or {}
        line(f"List version: {run.get('csl_version_id')} | Fetched: {version.get('fetched_at')}")
        line(
            f"List file SHA-256: {version.get('file_sha256')} | "
            f"Semantic SHA-256: {version.get('semantic_sha256')}"
        )
        for fetch in run.get("fetches", []):
            line(
                f"Fetch: {fetch['id']} | {fetch.get('status')} | "
                f"{fetch.get('completed_at')} | {fetch.get('source')}"
            )

    line(f"As of: {document['exported_at']}")
    line("Selection: " + _json(document["selection"]).decode())
    if document["kind"] == "party":
        current = document["current_record"]
        heading(current["label"])
        fields(current["party"], label="Current party")
        source = current.get("source") or {}
        line(f"Current source: {source.get('label')} ({source.get('kind')}); ID {source.get('id')}")
        for event in document["events"]:
            run_summary(event["run"])
            participation = event["participation"]
            if participation.get("historical_fields_unavailable"):
                line(participation["notice"])
            elif participation.get("party_snapshot"):
                fields(participation["party_snapshot"], label="Recorded historical participation")
                if participation.get("source_snapshot"):
                    source = participation["source_snapshot"]
                    line(
                        f"Historical source: {source.get('label')} ({source.get('kind')}); "
                        f"ID {source.get('id')}"
                    )
            if event["zero_hits"]:
                line("No recorded hits for this party in this completed run.")
            for hit in event["comparisons"]:
                comparison(hit)
            for proof in event["purged_comparisons"]:
                comparison(proof["hit"], purged=proof)
        if document.get("suppressions"):
            heading("Remembered false-positive pairings")
            for suppression in document["suppressions"]:
                line(f"Pairing: {suppression['id']} | Entry: {suppression['csl_entry_id']}")
                decision(suppression.get("original_decision"), "Original human decision")
    else:
        run_summary(document["run"])
        zero_count = sum(p["zero_hits"] for p in document["parties"])
        line(
            f"Zero-hit participants: {zero_count}. "
            "Their recorded snapshots, hashes and membership are in evidence.json."
        )
        for party in document["parties"]:
            if party["zero_hits"]:
                continue
            heading(f"Historical party {party['party_id']}")
            for hit in party["comparisons"]:
                comparison(hit)
            for proof in party["purged_comparisons"]:
                comparison(proof["hit"], purged=proof)
    heading("Relevant append-only audit events")
    line(
        "Complete sanitized before/after records and "
        "original actor identities are in evidence.json."
    )
    for event in document["audit_log"]:
        actor = event.get("actor") or {}
        line(
            f"{event['created_at']} | {event['action']} | {event['target_table']} "
            f"{event.get('target_id')} | "
            f"{actor.get('display_name') or event.get('actor_user_id') or 'System'}"
        )

    pdf.embed_file(
        bytes=_json(document),
        basename="evidence.json",
        mime_type="application/json",
        compress=True,
        desc="Exact sanitized audit evidence",
    )
    return bytes(pdf.output())


def export_party(
    db: Session, party_id: str, format: Format = "pdf", *, actor_user_id: str | None = None
) -> ExportArtifact:
    evidence = _start(db)
    if party_id not in evidence.parties:
        db.rollback()
        raise ExportNotFound("Party not found")
    document = evidence.party(party_id)
    artifact = _render(document, f"party-{_internal_id(party_id)}", format)
    _record(db, artifact, document["selection"], actor_user_id)
    return artifact


def export_run(
    db: Session, run_id: str, format: Format = "pdf", *, actor_user_id: str | None = None
) -> ExportArtifact:
    evidence = _start(db)
    if run_id not in evidence.runs:
        db.rollback()
        raise ExportNotFound("Run not found")
    document = evidence.run_document(run_id)
    artifact = _render(document, f"run-{_internal_id(run_id)}", format)
    _record(db, artifact, document["selection"], actor_user_id)
    return artifact


def _render(document: dict, filename: str, format: Format) -> ExportArtifact:
    if format == "pdf":
        return _artifact(_pdf(document), filename + ".pdf", "application/pdf")
    if format == "csv":
        return _artifact(_evidence_csv(document), filename + ".csv", "text/csv")
    raise ValueError("Export format must be pdf or csv")


def export_parties_csv(
    db: Session, *, filters: dict | None = None, actor_user_id: str | None = None
) -> ExportArtifact:
    _begin_snapshot(db)
    filters = filters or {}
    rows = filter_party_rows(party_rows(db), **filters)
    columns = [
        "id",
        "name",
        "role",
        "source_id",
        "country_iso2",
        "status",
        "last_screened_at",
        "stale",
        "auto_cleared",
        "deleted_at",
        "record_hash",
        "external_id",
        "alt_names",
        "address_lines",
        "city",
        "region",
        "postal_code",
    ]
    artifact = _artifact(_csv((_safe(row) for row in rows), columns), "parties.csv", "text/csv")
    _record(
        db,
        artifact,
        {"kind": "current_parties", "filters": filters, "count": len(rows)},
        actor_user_id,
    )
    return artifact


def export_full_zip(
    db: Session, *, spool_dir: Path | None = None, actor_user_id: str | None = None
) -> ExportArtifact:
    evidence = _start(db)
    if spool_dir is None:
        database = db.get_bind().engine.url.database
        spool_dir = (
            Path(database).resolve().parent / "exports"
            if database and database != ":memory:"
            else Path(__file__).resolve().parents[2] / "data" / "exports"
        )
    spool_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = tempfile.TemporaryDirectory(prefix="audit-", dir=spool_dir)
    path = Path(directory.name) / "full-audit.zip"
    checksums = {}
    try:
        with zipfile.ZipFile(
            path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
        ) as archive:

            def add(name: str, body: bytes) -> None:
                archive.writestr(name, body)
                checksums[name] = {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}

            for party_id in sorted(evidence.parties):
                document = evidence.party(party_id)
                add(f"parties/{_internal_id(party_id)}.json", _json(document))
                add(f"parties/{_internal_id(party_id)}.csv", _evidence_csv(document))
            # Full manifests live once, not in every party's file.
            for run_id in evidence.runs:
                add(f"runs/{_internal_id(run_id)}.json", _json(evidence.run(run_id)))
            add(
                "runs.csv",
                _csv(
                    (evidence.run(r) for r in evidence.runs),
                    [
                        "id",
                        "trigger",
                        "started_at",
                        "finished_at",
                        "parties_screened",
                        "hits_new",
                        "hits_suppressed",
                        "auto_cleared",
                        "model_name",
                        "csl_version_id",
                        "scope_sha256",
                    ],
                ),
            )
            with archive.open("audit.jsonl", "w", force_zip64=True) as audit_file:
                digest, size = hashlib.sha256(), 0
                for event in db.scalars(
                    select(AuditLog).order_by(AuditLog.created_at, AuditLog.id)
                ):
                    body = _json(evidence.audit_payload(event)) + b"\n"
                    audit_file.write(body)
                    digest.update(body)
                    size += len(body)
                # JSONL footer is a typed record, preserving machine readability.
                body = _json({"kind": "footer", "disclaimer": DISCLAIMER}) + b"\n"
                audit_file.write(body)
                digest.update(body)
                size += len(body)
                checksums["audit.jsonl"] = {"sha256": digest.hexdigest(), "bytes": size}

            def add_rows(name: str, records: Any) -> None:
                digest, size = hashlib.sha256(), 0
                with archive.open(name, "w", force_zip64=True) as stream:
                    for record in records:
                        body = _json(record) + b"\n"
                        stream.write(body)
                        digest.update(body)
                        size += len(body)
                    footer = _json({"kind": "footer", "disclaimer": DISCLAIMER}) + b"\n"
                    stream.write(footer)
                    digest.update(footer)
                    size += len(footer)
                checksums[name] = {"sha256": digest.hexdigest(), "bytes": size}

            add_rows(
                "list-entry-revisions.jsonl",
                (_row(row) for row in db.scalars(select(CSLEntryRevision))),
            )
            add_rows("list-versions.jsonl", evidence.versions.values())
            add_rows(
                "list-fetches.jsonl", (row for rows in evidence.fetches.values() for row in rows)
            )
            add_rows(
                "retention.jsonl", (row for rows in evidence.retention.values() for row in rows)
            )
            add_rows(
                "sources.jsonl",
                (
                    {
                        k: v
                        for k, v in _row(source).items()
                        if k not in {"credentials_enc", "cursor", "last_error"}
                    }
                    for source in evidence.sources.values()
                ),
            )
            add("NOTICE.txt", (DISCLAIMER + "\n").encode())
            manifest = evidence.base(
                "full_audit",
                {
                    "include_deleted": True,
                    "party_count": len(evidence.parties),
                    "run_count": len(evidence.runs),
                },
            )
            manifest["files"] = checksums
            manifest["party_file_formats"] = ["csv", "json"]
            add("manifest.json", _json(manifest))
        path.chmod(0o600)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        artifact = ExportArtifact(
            "full-audit.zip",
            "application/zip",
            digest.hexdigest(),
            path.stat().st_size,
            path=path,
            _directory=directory,
        )
        _record(db, artifact, manifest["selection"], actor_user_id)
        return artifact
    except BaseException:
        directory.cleanup()
        db.rollback()
        raise
