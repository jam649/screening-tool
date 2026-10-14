import { useEffect, useId, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import type { Session } from "./api";
import { canAdminister, messageOf, parseList, parseNumber, useSettings } from "./adminApi";
import { LocalTime } from "./CSLPanel";
import { Field } from "./SettingsControls";
import {
  baseUrlError,
  changedNotificationConfig,
  isActive,
  isLoopback,
  TLS_MODES,
  useNotificationConfig,
  useNotificationStatus,
  useReportStatus,
  useRetryBatch,
  useSaveNotificationConfig,
  useSendMorningReport,
  useSendTestEmail,
  validEmail,
  validReportTime,
  type Batch,
  type NotificationChanges,
  type NotificationConfig,
  type PublicNotificationConfig,
  type SmtpTls,
} from "./notificationsApi";

const STATUS_TEXT: Record<string, string> = {
  pending: "Queued, not yet delivered",
  sending: "Sending, not yet delivered",
  sent: "Accepted by the SMTP server",
  failed: "Failed",
  uncertain: "Uncertain: the SMTP server may have accepted it",
  cancelled: "Cancelled",
};
const statusText = (status: string) => STATUS_TEXT[status] ?? status;
const TLS_LABELS: Record<SmtpTls, string> = {
  starttls: "STARTTLS",
  ssl: "SSL/TLS",
  none: "None (anonymous local relay only)",
};
const BANNER_KINDS = new Set(["delivery_failed", "delivery_uncertain"]);

type Draft = {
  smtp_host: string;
  smtp_port: string;
  smtp_tls: SmtpTls;
  smtp_from: string;
  public_base_url: string;
  report_recipients: string;
  report_time: string;
  likely_alerts_enabled: boolean;
};
type DraftErrors = Partial<Record<keyof Draft, string>>;

const toDraft = (config: NotificationConfig): Draft => ({
  smtp_host: config.smtp_host,
  smtp_port: String(config.smtp_port),
  smtp_tls: config.smtp_tls,
  smtp_from: config.smtp_from,
  public_base_url: config.public_base_url,
  report_recipients: config.report_recipients.join("\n"),
  report_time: config.report_time,
  likely_alerts_enabled: config.likely_alerts_enabled,
});

function checkDraft(draft: Draft, credentialsRemain: boolean) {
  const errors: DraftErrors = {};
  const host = draft.smtp_host.trim();
  const port = parseNumber(draft.smtp_port);
  if (port === null || !Number.isInteger(port) || port < 1 || port > 65535)
    errors.smtp_port = "Enter a TCP port from 1 to 65535.";
  if (draft.smtp_tls === "none") {
    if (host && !isLoopback(host))
      errors.smtp_tls = "Unencrypted SMTP is allowed only for a relay on localhost, 127.0.0.1 or ::1.";
    else if (credentialsRemain)
      errors.smtp_tls = "Credentials are never sent unencrypted. Clear them or choose STARTTLS or SSL/TLS.";
  }
  const from = draft.smtp_from.trim();
  if (from && !validEmail(from)) errors.smtp_from = "Enter a single email address.";
  const recipients = parseList(draft.report_recipients, /[\n,]/);
  const invalid = recipients.filter((item) => !validEmail(item));
  if (invalid.length) errors.report_recipients = `Not a valid address: ${invalid.join(", ")}`;
  if (!validReportTime(draft.report_time)) errors.report_time = "Enter a 24-hour time as HH:MM.";
  const baseUrl = draft.public_base_url.trim();
  const baseError = baseUrlError(baseUrl);
  if (baseError) errors.public_base_url = baseError;
  const values: PublicNotificationConfig = {
    smtp_host: host,
    smtp_port: port ?? 0,
    smtp_tls: draft.smtp_tls,
    smtp_from: from,
    public_base_url: baseUrl.replace(/\/$/, ""),
    report_recipients: recipients,
    report_time: draft.report_time,
    likely_alerts_enabled: draft.likely_alerts_enabled,
  };
  return { errors, values };
}

function Unavailable({ what, error, onRetry }: { what: string; error: unknown; onRetry: () => void }) {
  return (
    <div className="admin-warning" role="alert">
      {what} is unavailable, so its current state is unknown. {messageOf(error, "")}
      <div className="admin-actions">
        <button type="button" className="button secondary admin-compact" onClick={onRetry}>
          Retry
        </button>
      </div>
    </div>
  );
}

function SmtpForm({ config }: { config: NotificationConfig }) {
  const save = useSaveNotificationConfig();
  const [draft, setDraft] = useState(() => toDraft(config));
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [clear, setClear] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const { reset } = save;
  useEffect(() => () => reset(), [reset]);

  const configured = config.smtp_username_configured || config.smtp_password_configured;
  const replacing = !clear && Boolean(username.trim() || password);
  const { errors, values } = checkDraft(draft, (configured && !clear) || replacing);
  const shown: DraftErrors = submitted ? errors : {};
  const edit = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((prev) => ({ ...prev, [key]: value }));

  async function submit(event: FormEvent) {
    event.preventDefault();
    setSubmitted(true);
    setResult(null);
    if (Object.keys(errors).length) return;
    const changes: NotificationChanges = changedNotificationConfig(config, values);
    if (clear) changes.clear_credentials = true;
    if (!clear && username.trim()) changes.smtp_username = username.trim();
    if (!clear && password) changes.smtp_password = password;
    if (!Object.keys(changes).length) {
      setResult({ ok: true, text: "No changes to save." });
      return;
    }
    try {
      const saved = await save.mutateAsync(changes);
      setDraft(toDraft(saved));
      setClear(false);
      setSubmitted(false);
      setResult({ ok: true, text: "Notification settings saved." });
    } catch (error) {
      const reenter = replacing ? " Re-enter the new SMTP username or password to replace it." : "";
      setResult({ ok: false, text: messageOf(error, "The settings could not be saved.") + reenter });
    } finally {
      // Secrets leave component state and mutation variables whatever the outcome.
      setUsername("");
      setPassword("");
      reset();
    }
  }

  return (
    <form className="admin-form" noValidate onSubmit={submit}>
      <fieldset className="admin-fieldset" disabled={save.isPending}>
        <div className="admin-grid">
          <Field label="SMTP host" help="Leave blank until an SMTP server is available.">
            {(props) => (
              <input {...props} type="text" autoComplete="off" spellCheck={false} value={draft.smtp_host} onChange={(e) => edit("smtp_host", e.target.value)} />
            )}
          </Field>
          <Field label="SMTP port" error={shown.smtp_port}>
            {(props) => (
              <input {...props} type="number" inputMode="numeric" min={1} max={65535} step={1} value={draft.smtp_port} onChange={(e) => edit("smtp_port", e.target.value)} />
            )}
          </Field>
          <Field label="Connection security" error={shown.smtp_tls}>
            {(props) => (
              <select {...props} value={draft.smtp_tls} onChange={(e) => edit("smtp_tls", e.target.value as SmtpTls)}>
                {TLS_MODES.map((mode) => (
                  <option key={mode} value={mode}>{TLS_LABELS[mode]}</option>
                ))}
              </select>
            )}
          </Field>
          <Field label="Sender address" error={shown.smtp_from}>
            {(props) => (
              <input {...props} type="email" autoComplete="off" value={draft.smtp_from} onChange={(e) => edit("smtp_from", e.target.value)} />
            )}
          </Field>
          <Field
            label="Public base URL"
            help="Used to build links back to this workspace in emails. An HTTPS origin, or HTTP on localhost for local testing. Blank means not configured."
            error={shown.public_base_url}
          >
            {(props) => (
              <input {...props} type="url" autoComplete="off" spellCheck={false} value={draft.public_base_url} onChange={(e) => edit("public_base_url", e.target.value)} />
            )}
          </Field>
          <Field label="Report time" help="24-hour HH:MM." error={shown.report_time}>
            {(props) => (
              <input {...props} type="time" value={draft.report_time} onChange={(e) => edit("report_time", e.target.value)} />
            )}
          </Field>
        </div>
        <div className="admin-grid">
          <Field label="Report recipients" help="One address per line or separated by commas." error={shown.report_recipients}>
            {(props) => (
              <textarea {...props} rows={4} spellCheck={false} value={draft.report_recipients} onChange={(e) => edit("report_recipients", e.target.value)} />
            )}
          </Field>
        </div>
        <fieldset className="admin-group">
          <legend>Immediate alerts</legend>
          <label className="checkbox-label">
            <input type="checkbox" checked={draft.likely_alerts_enabled} onChange={(e) => edit("likely_alerts_enabled", e.target.checked)} />
            Send immediate likely-match alerts
          </label>
          <span className="field-help">
            Off by default. When on, each screening run emails every likely observation, including repeated ones and labels previously cleared.
          </span>
        </fieldset>
        <fieldset className="admin-group">
          <legend>SMTP credentials</legend>
          <p className="admin-muted">
            The server reports the username is {config.smtp_username_configured ? "saved" : "not saved"} and the password is{" "}
            {config.smtp_password_configured ? "saved" : "not saved"}. Saved values are encrypted and never shown. Leave these fields blank to keep them.
          </p>
          <div className="admin-grid">
            <Field label="New SMTP username" help="Optional. Replaces the saved username.">
              {(props) => (
                <input {...props} type="text" autoComplete="new-password" spellCheck={false} disabled={clear} value={username} onChange={(e) => setUsername(e.target.value)} />
              )}
            </Field>
            <Field label="New SMTP password" help="Optional. Replaces the saved password.">
              {(props) => (
                <input {...props} type="password" autoComplete="new-password" disabled={clear} value={password} onChange={(e) => setPassword(e.target.value)} />
              )}
            </Field>
          </div>
          {configured && (
            <label className="checkbox-label">
              <input type="checkbox" checked={clear} disabled={Boolean(username || password)} onChange={(e) => setClear(e.target.checked)} />
              Clear saved SMTP username and password
            </label>
          )}
        </fieldset>
        <div className="admin-actions">
          <button type="submit" className="button primary">
            {save.isPending ? "Saving…" : "Save notification settings"}
          </button>
        </div>
      </fieldset>
      {result && !result.ok ? (
        <p className="admin-field-error admin-group-error" role="alert">{result.text}</p>
      ) : (
        <p className="admin-status" role="status">{result?.text}</p>
      )}
    </form>
  );
}

function BatchDetails({ batch }: { batch: Batch }) {
  const retry = useRetryBatch();
  const failed = batch.deliveries.some((item) => item.status === "failed");
  const uncertain = batch.deliveries.some((item) => item.status === "uncertain");
  const busy = isActive(batch) || retry.isPending;
  return (
    <div className="admin-details">
      <p className="admin-muted">Batch {batch.batch_id}: {statusText(batch.status)}</p>
      <ul className="admin-user-list">
        {batch.deliveries.map((item, index) => (
          <li key={`${item.recipient}-${index}`}>
            <div className="admin-user-identity">
              <strong>{item.recipient}</strong>
              <span>
                {statusText(item.status)} · Attempts: {item.attempts}
                {item.error_code ? ` · Error: ${item.error_code}` : ""}
              </span>
            </div>
          </li>
        ))}
      </ul>
      {failed && (
        <div className="admin-actions">
          <button type="button" className="button secondary admin-compact" disabled={busy} onClick={() => retry.mutate({ batchId: batch.batch_id, retryUncertain: false })}>
            Retry failed deliveries
          </button>
        </div>
      )}
      {uncertain && (
        <>
          <p className="admin-warning">
            The SMTP server may already have accepted an uncertain message, so another attempt may deliver a duplicate.
          </p>
          <div className="admin-actions">
            <button type="button" className="button secondary admin-compact" disabled={busy} onClick={() => retry.mutate({ batchId: batch.batch_id, retryUncertain: true })}>
              Retry uncertain delivery
            </button>
          </div>
        </>
      )}
      {retry.isError && (
        <p className="admin-field-error" role="alert">{messageOf(retry.error, "The retry could not be requested.")}</p>
      )}
      {retry.data && <p className="admin-status">Retry requested; batch is {statusText(retry.data.status).toLowerCase()}.</p>}
    </div>
  );
}

function Receipt({ receipt, current }: { receipt: Batch; current: Batch | undefined }) {
  return (
    <div aria-live="polite">
      <p className="admin-status">
        {current
          ? "Latest status from the server for this request."
          : "The server returned this delivery status; a refreshed status will appear when available."}
      </p>
      <BatchDetails batch={current ?? receipt} />
    </div>
  );
}

function SmtpSection({ session }: { session: Session }) {
  const id = useId();
  const config = useNotificationConfig(session);
  return (
    <section className="admin-subsection" aria-labelledby={id}>
      <h3 id={id}>SMTP server</h3>
      {config.isPending ? (
        <p className="admin-muted" role="status">Loading notification settings…</p>
      ) : config.isError ? (
        <Unavailable what="Notification settings" error={config.error} onRetry={() => void config.refetch()} />
      ) : (
        <SmtpForm config={config.data} />
      )}
    </section>
  );
}

function TestEmail({ session }: { session: Session }) {
  const id = useId();
  const send = useSendTestEmail();
  const status = useNotificationStatus(session);
  const [recipient, setRecipient] = useState("");
  const [error, setError] = useState<string>();
  const current = send.data && status.data?.batches.find((b) => b.batch_id === send.data.batch_id);
  function submit(event: FormEvent) {
    event.preventDefault();
    const value = recipient.trim();
    if (!validEmail(value)) return setError("Enter one email address.");
    setError(undefined);
    send.mutate({ recipient: value });
  }
  return (
    <section className="admin-subsection" aria-labelledby={id}>
      <h3 id={id}>Test email</h3>
      <form noValidate onSubmit={submit}>
        <div className="admin-grid admin-grid-tight">
          <Field label="Test recipient" help="Sent with the saved settings; save any edits above first." error={error}>
            {(props) => (
              <input {...props} type="email" autoComplete="off" value={recipient} onChange={(e) => setRecipient(e.target.value)} />
            )}
          </Field>
        </div>
        <div className="admin-actions">
          <button type="submit" className="button primary" disabled={send.isPending}>
            {send.isPending ? "Sending…" : "Send test email"}
          </button>
        </div>
      </form>
      {send.isError && (
        <p className="admin-field-error admin-group-error" role="alert">{messageOf(send.error, "The test email could not be requested.")}</p>
      )}
      {send.data && <Receipt receipt={send.data} current={current} />}
    </section>
  );
}

function MorningReport({ session }: { session: Session }) {
  const id = useId();
  const report = useReportStatus(session);
  const send = useSendMorningReport();
  const current = send.data && report.data?.batches.find((b) => b.batch_id === send.data.batch_id);
  return (
    <section className="admin-subsection" aria-labelledby={id}>
      <h3 id={id}>Morning report</h3>
      <p className="admin-intro">
        The morning report uses the saved report time and recipient list. It includes a no-change message when there is nothing new to review.
      </p>
      {report.isPending ? (
        <p className="admin-muted" role="status">Loading report status…</p>
      ) : report.isError ? (
        <Unavailable what="Morning report status" error={report.error} onRetry={() => void report.refetch()} />
      ) : (
        <>
          <p className="admin-status">
            {report.data.last_report_at ? (
              <>Last report accepted by SMTP for every recipient: <LocalTime value={report.data.last_report_at} /></>
            ) : (
              "No morning report has been accepted for every recipient yet."
            )}
          </p>
          {report.data.batches.slice(0, 3).map((batch) => (
            <BatchDetails key={batch.batch_id} batch={batch} />
          ))}
        </>
      )}
      <div className="admin-actions">
        <button type="button" className="button primary" disabled={send.isPending} onClick={() => send.mutate()}>
          {send.isPending ? "Sending…" : "Send morning report now"}
        </button>
      </div>
      {send.isError && (
        <p className="admin-field-error admin-group-error" role="alert">{messageOf(send.error, "The report could not be requested.")}</p>
      )}
      {send.data && <Receipt receipt={send.data} current={current} />}
    </section>
  );
}

function DeliveryStatus({ session }: { session: Session }) {
  const id = useId();
  const status = useNotificationStatus(session);
  return (
    <section className="admin-subsection" aria-labelledby={id}>
      <h3 id={id}>Delivery status</h3>
      {status.isPending ? (
        <p className="admin-muted" role="status">Loading delivery status…</p>
      ) : status.isError ? (
        <Unavailable what="Delivery status" error={status.error} onRetry={() => void status.refetch()} />
      ) : (
        <>
          {status.data.health.length === 0 && <p className="admin-muted">The server reports no delivery problems.</p>}
          {status.data.health.map((item, index) => (
            <p key={`${item.kind}-${index}`} className={item.kind === "smtp_not_configured" ? "admin-muted" : "admin-warning"}>
              {item.message}
            </p>
          ))}
          {status.data.batches.slice(0, 5).map((batch) => (
            <BatchDetails key={batch.batch_id} batch={batch} />
          ))}
        </>
      )}
    </section>
  );
}

export function NotificationSettings({ session }: { session: Session }) {
  const id = useId();
  const workspace = useSettings(session);
  if (!canAdminister(session)) return null;
  return (
    <section className="panel settings-panel" aria-labelledby={id}>
      <h2 id={id}>Email notifications</h2>
      <p className="admin-intro">
        Morning reports, likely-match alerts and test messages go through your own SMTP server. Statuses below come from the server; queued is not delivered.
      </p>
      <p className="admin-muted">Report times use {workspace.data?.timezone ?? "the timezone in General settings"}.</p>
      <SmtpSection session={session} />
      <DeliveryStatus session={session} />
      <TestEmail session={session} />
      <MorningReport session={session} />
    </section>
  );
}

export function NotificationsBanner({ session }: { session: Session }) {
  const status = useNotificationStatus(session);
  const admin = canAdminister(session);
  if (status.isError)
    return (
      <section className="admin-warning" aria-label="Email delivery status">
        Email delivery status is unavailable, so delivery problems cannot be ruled out.{" "}
        <button type="button" className="button secondary admin-compact" onClick={() => void status.refetch()}>
          Retry status
        </button>
      </section>
    );
  const problems = status.data?.health.filter((item) => BANNER_KINDS.has(item.kind)) ?? [];
  if (!problems.length) return null;
  return (
    <section className="admin-warning" aria-label="Email delivery status">
      {problems.map((item, index) => (
        <p key={`${item.kind}-${index}`} className="admin-status">{item.message}</p>
      ))}
      <p className="admin-status">
        {admin ? <Link to="/settings">Review email delivery in Settings</Link> : "An Admin can review email delivery in Settings."}
      </p>
    </section>
  );
}
