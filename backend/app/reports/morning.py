"""Freeze report windows and render current queue facts from immutable observations."""

from html import escape

from sqlalchemy import select

from app.config import DISCLAIMER
from app.models import AuditLog, CSLVersion, ScreeningRun, utcnow
from app.models.monitoring import NotificationBatch
from app.notifications.settings import (
    NotificationConfigurationError,
    load_smtp_settings,
    public_settings,
)
from app.reports.metrics import dashboard_metrics, report_boundary
from app.screening.engine import queue_hits


def enqueue_morning_report(db, config):
    from app.notifications.service import create_batch, product_name

    pending = db.scalar(
        select(NotificationBatch)
        .where(
            NotificationBatch.kind == "morning",
            NotificationBatch.status.notin_(["sent", "cancelled"]),
        )
        .order_by(NotificationBatch.created_at)
        .limit(1)
    )
    if pending:
        return pending
    values = public_settings(db)
    if not load_smtp_settings(db, config).configured or not values["report_recipients"]:
        raise NotificationConfigurationError("Configure SMTP and report recipients first")
    if not values["public_base_url"]:
        raise NotificationConfigurationError(
            "Configure the public application URL for report links"
        )
    end, start = utcnow(), report_boundary(db)
    metrics = dashboard_metrics(db, now=end)
    queue = queue_hits(db)
    runs = select(ScreeningRun).where(
        ScreeningRun.finished_at.is_not(None), ScreeningRun.finished_at <= end
    )
    changes = (
        select(CSLVersion)
        .join(AuditLog, AuditLog.target_id == CSLVersion.id)
        .where(AuditLog.action == "csl.version_created", AuditLog.created_at <= end)
    )
    if start:
        runs = runs.where(ScreeningRun.finished_at > start)
        changes = changes.where(AuditLog.created_at > start)
    new_hits = sum(run.hits_new for run in db.scalars(runs))
    changed_versions = list(db.scalars(changes))
    list_changes = {
        key: sum(getattr(version, key) for version in changed_versions)
        for key in ("added", "changed", "removed")
    }
    latest = metrics["csl_version"]
    lines = [f"As of {end.isoformat()}"]
    if latest:
        lines.append(f"CSL version {latest['id']}; fetched {latest['last_fetched_at'].isoformat()}")
    else:
        lines.append("CSL has not been fetched; screening coverage is unavailable.")
    base = values["public_base_url"]
    lines.extend(
        [
            f"Open items: {metrics['open_items']}",
            f"Queue: {base}/queue",
            f"Likely matches awaiting review: {metrics['likely_matches']}",
        ]
    )
    for hit in queue:
        if hit.bucket == "likely":
            lines.append(
                f"{hit.party_snapshot.get('name', '')} | "
                f"{hit.entry_snapshot.get('source_list', '')} | "
                f"confidence {hit.final_confidence:g}"
            )
    lines.extend(
        [
            f"New hits since the last report: {new_hits}",
            f"Auto-clears since the last report: {metrics['auto_cleared_since_report']}",
            f"Auto-cleared parties: {base}/parties?auto_cleared=true",
            f"Removed-list closures: {metrics['removed_entry_closures_since_report']}",
            f"List changes: {list_changes['added']} added, "
            f"{list_changes['changed']} changed, {list_changes['removed']} removed",
            "Sync health:",
        ]
    )
    unhealthy = [
        source for source in metrics["sync_health"] if source["stale"] or source["last_error"]
    ]
    for source in unhealthy:
        lines.append(
            f"{source['label']}: "
            f"{'sync failed' if source['last_error'] else 'not synced within 24 hours'}"
        )
    if not unhealthy:
        lines.append("All configured sources have current sync health.")
    if not new_hits and not metrics["auto_cleared_since_report"] and not any(list_changes.values()):
        lines.append("Nothing new to report.")
    lines.append(DISCLAIMER)
    html = "<html><body>" + "".join(f"<p>{escape(line)}</p>" for line in lines[:-1])
    html += (
        f'<p><a href="{escape(base + "/queue", quote=True)}">Open the review queue</a></p>'
        f'<p><a href="{escape(base + "/parties?auto_cleared=true", quote=True)}">'
        f"View auto-cleared parties</a></p><footer>{escape(DISCLAIMER)}</footer></body></html>"
    )
    return create_batch(
        db,
        config,
        kind="morning",
        dedupe_key=f"morning:{start.isoformat() if start else 'initial'}:{end.isoformat()}",
        window_start=start,
        window_end=end,
        recipients=values["report_recipients"],
        payload={
            "sender": values["smtp_from"],
            "subject": f"{product_name(db)}: {metrics['open_items']} open items, "
            f"{metrics['likely_matches']} likely matches",
            "text": "\n\n".join(lines),
            "html": html,
            "facts": {
                "open_items": metrics["open_items"],
                "likely_matches": metrics["likely_matches"],
                "new_hits": new_hits,
                "auto_cleared": metrics["auto_cleared_since_report"],
                "removed_entry_closures": metrics["removed_entry_closures_since_report"],
                "list_changes": list_changes,
                "queue_hit_ids": [hit.id for hit in queue],
            },
        },
    )
