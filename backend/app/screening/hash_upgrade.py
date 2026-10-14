"""One-time cache transition to the explicitly approved type-aware hash policy.

Current cache hashes can change; historical revisions, versions, hits, decisions,
and suppressions retain their exact original evidence. A full durable task screens
the current records under v2 without pretending old revisions used the new rule.
"""

from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.audit import audit
from app.csl.parser import HASH_RULE_VERSION, csl_entry_hash
from app.csl.service import current_version
from app.models import CSLEntry, Party, ScreeningTask, Setting
from app.screening.normalize import party_record_hash

HASH_RULE_SETTING = "matching_hash_rule_version"


@dataclass(frozen=True)
class HashUpgradeResult:
    parties_rehashed: int = 0
    entries_rehashed: int = 0
    task_id: str | None = None


def upgrade_hash_rules(db: Session) -> HashUpgradeResult:
    """Commit an idempotent upgrade before reads/screening; never discard pending edits."""
    if db.new or db.dirty or db.deleted:
        raise ValueError("Commit pending changes before upgrading matching hashes")
    db.commit()
    db.execute(text("BEGIN IMMEDIATE"))
    actor = db.info.pop("actor_user_id", None)
    try:
        marker = db.scalar(select(Setting).where(Setting.key == HASH_RULE_SETTING))
        if marker is not None and marker.value == HASH_RULE_VERSION:
            db.commit()
            return HashUpgradeResult()
        previous_rule = marker.value if marker else 1
        if previous_rule not in {1, HASH_RULE_VERSION}:
            raise ValueError("Unsupported cached matching hash policy; refusing to downgrade")
        party_count = entry_count = 0
        for party in db.scalars(select(Party)):
            fields = {
                key: getattr(party, key)
                for key in (
                    "party_type",
                    "name",
                    "alt_names",
                    "address_lines",
                    "city",
                    "region",
                    "postal_code",
                    "country_iso2",
                )
            }
            new_hash = party_record_hash(fields)
            if new_hash != party.record_hash:
                old_hash = party.record_hash
                party.record_hash = new_hash
                party_count += 1
                audit(
                    db,
                    "matching.party_hash_upgraded",
                    "parties",
                    party.id,
                    before={"hash_rule_version": previous_rule, "record_hash": old_hash},
                    after={"hash_rule_version": HASH_RULE_VERSION, "record_hash": new_hash},
                )
        for entry in db.scalars(select(CSLEntry)):
            fields = {
                key: getattr(entry, key)
                for key in (
                    "name",
                    "alt_names",
                    "addresses",
                    "countries",
                    "programs",
                    "entity_type",
                )
            }
            new_hash = csl_entry_hash(fields)
            if new_hash != entry.entry_hash:
                old_hash = entry.entry_hash
                entry.entry_hash = new_hash
                entry_count += 1
                audit(
                    db,
                    "matching.entry_hash_upgraded",
                    "csl_entries",
                    entry.id,
                    before={"hash_rule_version": previous_rule, "entry_hash": old_hash},
                    after={"hash_rule_version": HASH_RULE_VERSION, "entry_hash": new_hash},
                )
        version = current_version(db)
        task = None
        if version is not None and (party_count or entry_count):
            task = ScreeningTask(
                trigger="manual", csl_version_id=version.id, party_ids=[], entry_ids=[]
            )
            db.add(task)
            db.flush()
            audit(
                db,
                "screening.enqueued",
                "screening_tasks",
                task.id,
                after={
                    "trigger": "manual",
                    "reason": "matching hash policy upgrade",
                    "hash_rule_version": HASH_RULE_VERSION,
                    "csl_version_id": version.id,
                },
            )
        if marker is None:
            marker = Setting(key=HASH_RULE_SETTING, value=HASH_RULE_VERSION)
            db.add(marker)
        else:
            marker.value = HASH_RULE_VERSION
        audit(
            db,
            "matching.hash_policy_upgraded",
            "settings",
            before={"hash_rule_version": previous_rule},
            after={
                "hash_rule_version": HASH_RULE_VERSION,
                "parties_rehashed": party_count,
                "entries_rehashed": entry_count,
                "full_screening_task_id": task.id if task else None,
            },
        )
        db.commit()
        return HashUpgradeResult(party_count, entry_count, task.id if task else None)
    except Exception:
        db.rollback()
        raise
    finally:
        if actor is not None:
            db.info["actor_user_id"] = actor
