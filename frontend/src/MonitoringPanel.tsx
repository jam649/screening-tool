import { useId, useState, type FormEvent } from "react";
import type { UseQueryResult } from "@tanstack/react-query";
import type { Session } from "./api";
import { canAdminister, messageOf, useSettings } from "./adminApi";
import { Field } from "./SettingsControls";
import { LocalTime } from "./CSLPanel";
import {
  WEEKDAYS,
  changedMonitoring,
  isActive,
  isProblem,
  latestByOperation,
  needsAttention,
  useCycleAttempt,
  useMonitoringSettings,
  useMonitoringStatus,
  useSaveMonitoringSettings,
  useStartCycle,
  type MonitoringOperation,
  type MonitoringSettings,
  type MonitoringStatus,
  type Weekday,
} from "./monitoringApi";

const OPERATION_LABELS: Record<string, string> = {
  monitor_cycle: "Monitoring cycle",
  csl_fetch: "CSL download",
  source_poll: "Source polling",
  retention: "Retention",
};
const operationLabel = (operation: string) =>
  OPERATION_LABELS[operation] ??
  operation.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
const WEEKDAY_NAMES: Record<Weekday, string> = {
  sun: "Sunday",
  mon: "Monday",
  tue: "Tuesday",
  wed: "Wednesday",
  thu: "Thursday",
  fri: "Friday",
  sat: "Saturday",
};
const HH_MM = /^([01]\d|2[0-3]):[0-5]\d$/;

/** Primitive outcome facts only; nested records are never rendered. */
function outcomeFacts(outcome: unknown): string[] {
  if (!outcome || typeof outcome !== "object" || Array.isArray(outcome)) return [];
  return Object.entries(outcome)
    .filter(([, value]) => ["string", "number", "boolean"].includes(typeof value))
    .slice(0, 8)
    .map(([key, value]) => `${key.replace(/_/g, " ")}: ${String(value).slice(0, 160)}`);
}

function Details({ operation }: { operation: MonitoringOperation }) {
  const facts = [
    operation.error_code && `Error code: ${operation.error_code}`,
    ...outcomeFacts(operation.outcome),
  ].filter(Boolean);
  return facts.length ? <>{facts.join("; ")}</> : <span className="admin-muted">None reported</span>;
}

const When = ({ at, none }: { at: string | null; none: string }) =>
  at ? <LocalTime value={at} /> : <span className="admin-muted">{none}</span>;

function LoadError(props: { what: string; error: unknown; onRetry: () => void; busy: boolean }) {
  return (
    <div className="admin-warning" role="alert">
      {props.what}: {messageOf(props.error, "Unknown error.")}{" "}
      <button type="button" className="button secondary admin-compact" onClick={props.onRetry} disabled={props.busy}>
        {props.busy ? "Retrying…" : "Retry"}
      </button>
    </div>
  );
}

/** Compact Home notice: silent while loading or healthy. */
export function MonitoringSummary({ session }: { session: Session }) {
  const status = useMonitoringStatus(session);
  if (status.isPending) return null;
  if (status.isError)
    return (
      <section aria-label="Monitoring health">
        <LoadError what="Monitoring status could not be loaded" error={status.error} onRetry={() => void status.refetch()} busy={status.isFetching} />
      </section>
    );
  const { enabled, health, operations } = status.data;
  const problems = health.filter(isProblem);
  const failed = latestByOperation(operations).filter((op) => needsAttention(op.status));
  if (enabled && !problems.length && !failed.length) return null;
  return (
    <section className="admin-warning" aria-label="Monitoring health">
      <strong>Monitoring needs attention</strong>
      <ul>
        {!enabled && <li>Scheduled monitoring is not enabled.</li>}
        {problems.map((item, index) => (
          <li key={`${item.kind}-${index}`}>{item.message}</li>
        ))}
        {failed.map((op) => (
          <li key={op.id}>
            {operationLabel(op.operation)} last ended with status “{op.status}”
            {op.error_code && ` (${op.error_code})`}
            {op.finished_at && <> at <LocalTime value={op.finished_at} /> (your local time)</>}.
          </li>
        ))}
      </ul>
    </section>
  );
}

function StatusDetails({ status }: { status: UseQueryResult<MonitoringStatus> }) {
  const schedulesId = useId();
  const operationsId = useId();
  if (status.isPending) return <p className="admin-muted" role="status">Loading monitoring status…</p>;
  if (status.isError)
    return <LoadError what="Monitoring status could not be loaded" error={status.error} onRetry={() => void status.refetch()} busy={status.isFetching} />;
  const { enabled, schedules, operations, health } = status.data;
  const problems = health.filter(isProblem);
  return (
    <>
      <p className="admin-status">
        Scheduled monitoring: <strong>{enabled ? "Enabled" : "Not enabled"}</strong>
      </p>
      {problems.length ? (
        <ul className="admin-warning" aria-label="Health problems">
          {problems.map((item, index) => (
            <li key={`${item.kind}-${index}`}>{item.message}</li>
          ))}
        </ul>
      ) : (
        <p className="admin-muted">No health problems reported.</p>
      )}
      <div className="admin-subsection">
        <h3 id={schedulesId}>Upcoming runs</h3>
        {schedules.length ? (
          <div className="rv-table-wrap" tabIndex={0}><table className="rv-table" aria-labelledby={schedulesId}>
            <thead>
              <tr><th scope="col">Schedule</th><th scope="col">Next run (your local time)</th></tr>
            </thead>
            <tbody>
              {schedules.map((schedule) => (
                <tr key={schedule.id}>
                  <td>{operationLabel(schedule.operation)}</td>
                  <td><When at={schedule.next_run_at} none="Not scheduled" /></td>
                </tr>
              ))}
            </tbody>
          </table></div>
        ) : (
          <p className="admin-muted">No schedules reported.</p>
        )}
      </div>
      <div className="admin-subsection">
        <h3 id={operationsId}>Recent operations</h3>
        {operations.length ? (
          <div className="rv-table-wrap" tabIndex={0}><table className="rv-table" aria-labelledby={operationsId}>
            <thead>
              <tr>
                <th scope="col">Operation</th><th scope="col">Status</th>
                <th scope="col">Started (your local time)</th><th scope="col">Finished (your local time)</th>
                <th scope="col">Details</th>
              </tr>
            </thead>
            <tbody>
              {operations.map((op) => (
                <tr key={op.id}>
                  <td>{operationLabel(op.operation)}</td>
                  <td>{op.status}</td>
                  <td><When at={op.started_at} none="Not started" /></td>
                  <td><When at={op.finished_at} none="Not finished" /></td>
                  <td><Details operation={op} /></td>
                </tr>
              ))}
            </tbody>
          </table></div>
        ) : (
          <p className="admin-muted">No operations reported.</p>
        )}
      </div>
    </>
  );
}

function AttemptMessage({ id, operation }: { id: string; operation?: MonitoringOperation }) {
  if (!operation || operation.status === "queued")
    return <p className="admin-status">Monitoring cycle queued (attempt {id}); it has not started yet.</p>;
  if (operation.status === "running")
    return (
      <p className="admin-status">
        Monitoring cycle running since <When at={operation.started_at} none="an unreported time" /> (your local time)…
      </p>
    );
  const success = operation.status === "success";
  return (
    <div className={success ? "admin-status" : "admin-warning"}>
      {success ? "Monitoring cycle finished successfully" : `Monitoring cycle ended with status “${operation.status}”`}
      {operation.finished_at && <> at <LocalTime value={operation.finished_at} /> (your local time)</>}.{" "}
      Attempt {id}. Details: <Details operation={operation} />
    </div>
  );
}

function CycleControl() {
  const [attemptId, setAttemptId] = useState<string | null>(null);
  const start = useStartCycle();
  const attempt = useCycleAttempt(attemptId);
  const op = attempt.data;
  const busy = start.isPending || (attemptId !== null && !attempt.isError && (!op || isActive(op.status)));
  return (
    <div className="admin-subsection">
      <h3>Manual monitoring cycle</h3>
      <p className="admin-intro">
        Runs the monitoring operations and source deltas now. It does not guarantee a full rescreen of every party.
      </p>
      <div className="admin-actions">
        <button
          type="button"
          className="button primary"
          disabled={busy}
          aria-busy={busy || undefined}
          onClick={() => start.mutate(undefined, { onSuccess: (queued) => setAttemptId(queued.attempt_id) })}
        >
          {start.isPending ? "Starting…" : "Run monitoring cycle"}
        </button>
      </div>
      <div role="status" aria-label="Cycle status">
        {attemptId && (op || !attempt.isError) && <AttemptMessage id={attemptId} operation={op} />}
      </div>
      {start.isError && (
        <p className="admin-warning" role="alert">The cycle could not be started: {messageOf(start.error, "Unknown error.")}</p>
      )}
      {attempt.isError && (
        <LoadError what="Could not check the monitoring cycle; it may still be running" error={attempt.error} onRetry={() => void attempt.refetch()} busy={attempt.isFetching} />
      )}
    </div>
  );
}

type Draft = Omit<MonitoringSettings, "source_poll_minutes"> & { source_poll_minutes: string };
type Errors = Partial<Record<keyof Draft, string>>;
const toDraft = (saved: MonitoringSettings): Draft => ({ ...saved, source_poll_minutes: String(saved.source_poll_minutes) });

function validate(draft: Draft): Errors {
  const errors: Errors = {};
  if (!HH_MM.test(draft.csl_fetch_time)) errors.csl_fetch_time = "Enter a time as HH:MM.";
  if (!HH_MM.test(draft.retention_time)) errors.retention_time = "Enter a time as HH:MM.";
  if (!/^[1-9]\d*$/.test(draft.source_poll_minutes.trim()))
    errors.source_poll_minutes = "Enter a whole number of minutes greater than zero.";
  return errors;
}

function ScheduleForm({ session }: { session: Session }) {
  const settings = useMonitoringSettings(session);
  const workspace = useSettings(session);
  const save = useSaveMonitoringSettings();
  const [draft, setDraft] = useState<Draft | null>(null);
  const [errors, setErrors] = useState<Errors>({});
  const [notice, setNotice] = useState("");

  if (settings.isPending) return <p className="admin-muted" role="status">Loading schedule settings…</p>;
  if (settings.isError)
    return <LoadError what="Schedule settings could not be loaded" error={settings.error} onRetry={() => void settings.refetch()} busy={settings.isFetching} />;
  const saved = settings.data;
  const values = draft ?? toDraft(saved);
  const edit = (changes: Partial<Draft>) => {
    setDraft({ ...values, ...changes });
    setNotice("");
  };
  const zone = workspace.data?.timezone;

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setNotice("");
    const found = validate(values);
    setErrors(found);
    if (Object.keys(found).length) return;
    const changes = changedMonitoring(saved, { ...values, source_poll_minutes: Number(values.source_poll_minutes.trim()) });
    if (!Object.keys(changes).length) {
      setNotice("No changes to save.");
      return;
    }
    // Edits stay in the draft on failure (including 422) so nothing is lost.
    save.mutate(changes, {
      onSuccess: () => {
        setDraft(null);
        setNotice("Schedule saved.");
      },
    });
  }

  return (
    <form className="admin-form" onSubmit={submit} noValidate>
      <p className="admin-intro">
        {zone ? <>Schedule times are in the workspace time zone, <strong>{zone}</strong>.</>
          : workspace.isError ? "The workspace time zone could not be loaded; schedule times use the time zone in general settings."
          : "Loading the workspace time zone…"}
      </p>
      <fieldset className="admin-group" disabled={save.isPending}>
        <legend>Schedule settings</legend>
        <div className="admin-grid">
          <Field label="CSL download time" error={errors.csl_fetch_time}>
            {(props) => <input {...props} type="time" required value={values.csl_fetch_time} onChange={(e) => edit({ csl_fetch_time: e.target.value })} />}
          </Field>
          <Field label="Source poll interval (minutes)" help="Polls configured API connectors only. CSV sources update by manual upload." error={errors.source_poll_minutes}>
            {(props) => <input {...props} type="number" inputMode="numeric" min={1} step={1} required value={values.source_poll_minutes} onChange={(e) => edit({ source_poll_minutes: e.target.value })} />}
          </Field>
          <Field label="Retention day">
            {(props) => (
              <select {...props} value={values.retention_weekday} onChange={(e) => edit({ retention_weekday: e.target.value as Weekday })}>
                {WEEKDAYS.map((day) => <option key={day} value={day}>{WEEKDAY_NAMES[day]}</option>)}
              </select>
            )}
          </Field>
          <Field label="Retention time" error={errors.retention_time}>
            {(props) => <input {...props} type="time" required value={values.retention_time} onChange={(e) => edit({ retention_time: e.target.value })} />}
          </Field>
        </div>
      </fieldset>
      {save.isError && (
        <p className="admin-warning" role="alert">The schedule was not saved: {messageOf(save.error, "Unknown error.")}</p>
      )}
      <div className="admin-actions">
        <button type="submit" className="button primary" disabled={save.isPending} aria-busy={save.isPending || undefined}>
          {save.isPending ? "Saving…" : "Save schedule"}
        </button>
      </div>
      <p className="admin-status" role="status">{notice}</p>
    </form>
  );
}

/** Full settings panel: read-only status for everyone, controls for Admins only. */
export function MonitoringPanel({ session }: { session: Session }) {
  const headingId = useId();
  const status = useMonitoringStatus(session);
  const admin = canAdminister(session);
  return (
    <section className="panel settings-panel" aria-labelledby={headingId}>
      <h2 id={headingId}>Monitoring and schedules</h2>
      <p className="admin-intro">
        Scheduled CSL downloads, polling of configured API source connectors and retention. CSV sources change only by manual upload.
      </p>
      <StatusDetails status={status} />
      {admin && <CycleControl />}
      {admin && (
        <div className="admin-subsection">
          <h3>Schedule</h3>
          <ScheduleForm session={session} />
        </div>
      )}
    </section>
  );
}
