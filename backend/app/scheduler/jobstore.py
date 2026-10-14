"""APScheduler job store with an allowlisted JSON schema instead of executable pickle."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.job import Job
from apscheduler.jobstores.base import BaseJobStore, ConflictingIdError, JobLookupError
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import Column, Float, MetaData, String, Table, Text, delete, insert, select, update
from sqlalchemy.exc import IntegrityError

CALLBACK = "app.scheduler.runtime:scheduled_command"


def encode(job):
    state = job.__getstate__()
    if state["func"] != CALLBACK or state["kwargs"]:
        raise ValueError("Only fixed scheduler commands may be persisted")
    trigger = job.trigger
    if isinstance(trigger, CronTrigger):
        definition = {
            "kind": "cron",
            "timezone": str(trigger.timezone),
            "fields": {field.name: str(field) for field in trigger.fields},
        }
    elif isinstance(trigger, IntervalTrigger):
        definition = {
            "kind": "interval",
            "timezone": str(trigger.timezone),
            "seconds": trigger.interval.total_seconds(),
            "start_date": trigger.start_date.isoformat(),
        }
    else:
        raise ValueError("Unsupported persisted schedule")
    return json.dumps(
        {
            "version": 1,
            "id": job.id,
            "name": job.name,
            "args": list(job.args),
            "trigger": definition,
            "executor": job.executor,
            "misfire_grace_time": job.misfire_grace_time,
            "coalesce": job.coalesce,
            "max_instances": job.max_instances,
            "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
        }
    )


class JSONJobStore(BaseJobStore):
    def __init__(self, engine):
        super().__init__()
        self.engine = engine
        self.table = Table(
            "apscheduler_jobs",
            MetaData(),
            Column("id", String(191), primary_key=True),
            Column("next_run_time", Float, index=True),
            Column("job_state", Text, nullable=False),
        )

    def start(self, scheduler, alias):
        super().start(scheduler, alias)
        self.table.create(self.engine, checkfirst=True)

    def _restore(self, raw):
        value = json.loads(raw)
        if value.get("version") != 1:
            raise ValueError("Unsupported schedule version")
        definition = value.pop("trigger")
        timezone = ZoneInfo(definition["timezone"])
        if definition["kind"] == "cron":
            trigger = CronTrigger(timezone=timezone, **definition["fields"])
        elif definition["kind"] == "interval":
            trigger = IntervalTrigger(
                seconds=definition["seconds"],
                timezone=timezone,
                start_date=datetime.fromisoformat(definition["start_date"]),
            )
        else:
            raise ValueError("Unsupported schedule")
        value["trigger"] = trigger
        value["func"], value["kwargs"] = CALLBACK, {}
        value["args"] = tuple(value["args"])
        value["next_run_time"] = (
            datetime.fromisoformat(value["next_run_time"]) if value["next_run_time"] else None
        )
        job = Job.__new__(Job)
        job.__setstate__(value)
        job._scheduler, job._jobstore_alias = self._scheduler, self._alias
        return job

    def lookup_job(self, job_id):
        with self.engine.connect() as connection:
            value = connection.scalar(
                select(self.table.c.job_state).where(self.table.c.id == job_id)
            )
        return self._restore(value) if value is not None else None

    def _jobs(self, condition=None):
        query = select(self.table.c.job_state).order_by(self.table.c.next_run_time)
        if condition is not None:
            query = query.where(condition)
        with self.engine.connect() as connection:
            values = list(connection.scalars(query))
        return [self._restore(value) for value in values]

    def get_due_jobs(self, now):
        return self._jobs(self.table.c.next_run_time <= now.timestamp())

    def get_next_run_time(self):
        with self.engine.connect() as connection:
            value = connection.scalar(
                select(self.table.c.next_run_time)
                .where(self.table.c.next_run_time.is_not(None))
                .order_by(self.table.c.next_run_time)
                .limit(1)
            )
        return datetime.fromtimestamp(value, ZoneInfo("UTC")) if value is not None else None

    def get_all_jobs(self):
        jobs = self._jobs()
        self._fix_paused_jobs_sorting(jobs)
        return jobs

    def add_job(self, job):
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    insert(self.table).values(
                        id=job.id,
                        next_run_time=job.next_run_time.timestamp() if job.next_run_time else None,
                        job_state=encode(job),
                    )
                )
        except IntegrityError:
            raise ConflictingIdError(job.id) from None

    def update_job(self, job):
        with self.engine.begin() as connection:
            result = connection.execute(
                update(self.table)
                .where(self.table.c.id == job.id)
                .values(
                    next_run_time=job.next_run_time.timestamp() if job.next_run_time else None,
                    job_state=encode(job),
                )
            )
            if result.rowcount == 0:
                raise JobLookupError(job.id)

    def remove_job(self, job_id):
        with self.engine.begin() as connection:
            result = connection.execute(delete(self.table).where(self.table.c.id == job_id))
            if result.rowcount == 0:
                raise JobLookupError(job_id)

    def remove_all_jobs(self):
        with self.engine.begin() as connection:
            connection.execute(delete(self.table))

    def shutdown(self):
        # Root owns the shared database engine lifecycle.
        pass
