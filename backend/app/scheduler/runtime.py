"""Persistent, reconciled schedules; stored job arguments contain fixed IDs only."""

from datetime import timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.models import Setting, Source, User, utcnow
from app.notifications.settings import public_settings
from app.scheduler.jobstore import JSONJobStore
from app.scheduler.service import dispatch_pending, run_operation

_RUNTIME = None
MONITORING_DEFAULTS = {
    "csl_fetch_time": "05:00",
    "source_poll_minutes": 15,
    "retention_weekday": "sun",
    "retention_time": "03:00",
}


def monitoring_settings(db, config):
    values = {**MONITORING_DEFAULTS, "timezone": config.default_timezone}
    for row in db.scalars(select(Setting).where(Setting.key.in_(values))):
        if not row.encrypted:
            values[row.key] = row.value
    return values


def scheduled_command(operation, source_id=None):
    runtime = _RUNTIME
    if runtime is None:
        return
    with runtime.factory() as db:
        if (
            db.scalar(select(User.id).where(User.active.is_(True), User.role == "admin").limit(1))
            is None
        ):
            return
    try:
        if operation == "dispatch_pending":
            dispatch_pending(runtime.factory, runtime.config)
        else:
            run_operation(runtime.factory, runtime.config, operation, source_id=source_id)
    except Exception:
        # Durable operation attempts provide the UI failure evidence. Do not print
        # traceback/input values from a scheduler worker.
        return


class SchedulerRuntime:
    def __init__(self, factory, config):
        global _RUNTIME
        _RUNTIME = self
        self.factory, self.config = factory, config
        self.scheduler = BackgroundScheduler(
            jobstores={"default": JSONJobStore(factory.kw["bind"])},
            timezone=config.default_timezone,
        )
        self.scheduler.start(paused=True)
        self.reconcile()
        self.scheduler.resume()

    def reconcile(self):
        with self.factory() as db:
            values = monitoring_settings(db, self.config)
            notifications = public_settings(db)
            sources = list(db.scalars(select(Source.id).where(Source.kind.in_(["qbo", "bc"]))))
        timezone = ZoneInfo(values["timezone"])
        definitions = {}
        for job_id, operation, when, day in [
            ("csl-daily", "csl_refresh", values["csl_fetch_time"], None),
            ("morning-daily", "morning_report", notifications["report_time"], None),
            (
                "retention-weekly",
                "retention_apply",
                values["retention_time"],
                values["retention_weekday"],
            ),
        ]:
            hour, minute = map(int, when.split(":"))
            trigger = CronTrigger(
                hour=hour, minute=minute, day_of_week=day or "*", timezone=timezone
            )
            definitions[job_id] = (operation, None, trigger)
        for source_id in sources:
            definitions[f"source-{source_id}"] = (
                "source_sync",
                source_id,
                IntervalTrigger(minutes=values["source_poll_minutes"], timezone=timezone),
            )
        for job_id, operation in [
            ("screen-recovery", "screen_changed"),
            ("outbox-recovery", "alert_dispatch"),
            ("operation-recovery", "dispatch_pending"),
        ]:
            definitions[job_id] = (operation, None, IntervalTrigger(seconds=30, timezone=timezone))
        self.scheduler.pause()
        try:
            for job in self.scheduler.get_jobs():
                if job.id not in definitions:
                    self.scheduler.remove_job(job.id)
            for job_id, (operation, source_id, trigger) in definitions.items():
                current = self.scheduler.get_job(job_id)
                # Preserve the durable next occurrence across restarts and ordinary
                # unrelated settings updates. Interval start_date is intentionally ignored.
                same_trigger = (
                    current is not None
                    and str(current.trigger) == str(trigger)
                    and str(current.trigger.timezone) == str(trigger.timezone)
                )
                if same_trigger and current.args == (operation, source_id):
                    continue
                self.scheduler.add_job(
                    scheduled_command,
                    trigger=trigger,
                    args=(operation, source_id),
                    id=job_id,
                    name=job_id,
                    replace_existing=True,
                    coalesce=True,
                    max_instances=1,
                    misfire_grace_time=86400,
                )
        finally:
            self.scheduler.resume()

    def wake(self):
        # Wake a persistent fixed recovery schedule instead of adding closures or
        # serializing session factories / credentials as scheduler arguments.
        for job_id in ("operation-recovery", "screen-recovery", "outbox-recovery"):
            job = self.scheduler.get_job(job_id)
            if job:
                self.scheduler.modify_job(
                    job.id, next_run_time=utcnow() + timedelta(milliseconds=100)
                )

    def shutdown(self):
        global _RUNTIME
        self.scheduler.pause()
        self.scheduler.shutdown(wait=True)
        if _RUNTIME is self:
            _RUNTIME = None

    def schedules(self):
        return [
            {"id": job.id, "operation": job.args[0], "next_run_at": job.next_run_time}
            for job in self.scheduler.get_jobs()
        ]


def start_scheduler(factory, config):
    global _RUNTIME
    if not config.scheduler_enabled:
        return None
    _RUNTIME = SchedulerRuntime(factory, config)
    _RUNTIME.wake()
    return _RUNTIME


def current_runtime():
    return _RUNTIME
