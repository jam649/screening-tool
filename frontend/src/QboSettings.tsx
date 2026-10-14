import { useEffect, useId, useState, type FormEvent } from "react";
import { ApiError, type Session } from "./api";
import { canAdminister, messageOf } from "./adminApi";
import { Field } from "./SettingsControls";
import {
  QBO_ENVIRONMENTS,
  useQboSettings,
  useSaveQboSettings,
  type QboAppFlags,
  type QboAppSettings,
  type QboEnvironment,
  type QboSettingsChanges,
} from "./qboApi";

const ENV_LABEL: Record<QboEnvironment, string> = {
  sandbox: "Sandbox",
  production: "Production",
};
const SECRET_FIELDS = ["client_id", "client_secret", "verifier_token"] as const;

type Draft = {
  client_id: string;
  client_secret: string;
  verifier_token: string;
  clear_credentials: boolean;
  clear_verifier_token: boolean;
};
type Errors = Partial<Record<keyof Draft, string>>;
const EMPTY: Draft = {
  client_id: "",
  client_secret: "",
  verifier_token: "",
  clear_credentials: false,
  clear_verifier_token: false,
};

const saved = (flag: boolean) => (flag ? "Saved" : "Not saved");
const NO_BASE_URL =
  "Not available yet. Set the Public base URL in the Email notifications settings (SMTP server section), then check the updated URLs here.";

/** Replacements and clears for one environment; a field that is only whitespace is an error, not an omission. */
function prepare(environment: QboEnvironment, draft: Draft) {
  const errors: Errors = {};
  const changes: QboSettingsChanges = { environment };
  for (const key of SECRET_FIELDS) {
    if (!draft[key]) continue;
    const value = draft[key].trim();
    if (!value) errors[key] = "Enter a value, or leave this blank to keep the saved one.";
    else changes[key] = value;
  }
  if (draft.clear_credentials) {
    if (draft.client_id || draft.client_secret)
      errors.clear_credentials = "Either clear the saved client ID and secret or enter replacements, not both.";
    else changes.clear_credentials = true;
  }
  if (draft.clear_verifier_token) {
    if (draft.verifier_token)
      errors.clear_verifier_token = "Either clear the saved verifier token or enter a replacement, not both.";
    else changes.clear_verifier_token = true;
  }
  return { errors, changes };
}

function failureText(error: unknown, submitted: string[]) {
  const raw = messageOf(error, "The QuickBooks app settings could not be saved.");
  // Never echo a submitted value back, even if the server's message contains one.
  const safe = submitted.some((value) => raw.includes(value))
    ? "The server rejected the submitted values."
    : raw;
  let hint = "";
  if (error instanceof ApiError && error.status === 409)
    hint =
      " This happens while a source in this environment is syncing, or when the client ID would be replaced or cleared while sources still hold connection credentials from this app. Wait for the sync to finish, or disconnect those sources in Sources first.";
  if (error instanceof ApiError && error.status === 422)
    hint = " Check that a replacement and its clear option aren’t combined.";
  return `${safe}${hint} The save could not be confirmed. Entered values were cleared; check the saved flags before retrying.`;
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <li>
      <div className="admin-user-identity">
        <strong>{label}</strong> <span>{value}</span>
      </div>
    </li>
  );
}

function EnvironmentFacts({
  environment,
  flags,
  webhookUrl,
}: {
  environment: QboEnvironment;
  flags: QboAppFlags;
  webhookUrl: string | null;
}) {
  const id = useId();
  const label = ENV_LABEL[environment];
  return (
    <section className="admin-subsection" aria-labelledby={id}>
      <h3 id={id}>{label} app</h3>
      <ul className="admin-user-list" aria-label={`${label} app credentials saved on the server`}>
        <Fact label="Client ID" value={saved(flags.client_id_configured)} />
        <Fact label="Client secret" value={saved(flags.client_secret_configured)} />
        <Fact
          label="Webhook verifier token"
          value={`${saved(flags.verifier_token_configured)} (optional; used only for webhooks)`}
        />
        <Fact
          label="App configuration"
          value={
            flags.configured
              ? "Configured. Sources in this environment can begin Intuit authorization."
              : "Not configured. Save this environment’s client ID and client secret before connecting a company."
          }
        />
      </ul>
      <p className="admin-muted">
        A configured app is not a connected company. Add one QuickBooks source
        per company in Sources and authorize it there; each source shows its
        own connection status.
      </p>
      {environment === "production" ? (
        <p className="admin-warning">
          Production keys work only after Intuit approves your app through its
          app assessment. Complete that with Intuit before connecting real
          companies.
        </p>
      ) : (
        <p className="admin-muted">
          Use sandbox keys with Intuit sandbox companies only.
        </p>
      )}
      <div className="admin-details">
        <p className="admin-status">
          <strong>{label} webhook URL</strong>
        </p>
        <p className="admin-status">
          {webhookUrl ? <code>{webhookUrl}</code> : NO_BASE_URL}
        </p>
        <p className="admin-muted">
          Webhooks are optional. Sources are polled on a schedule either way;
          a verified webhook only prompts an earlier poll. To use them, register
          this URL in the Intuit app’s webhook settings, save its verifier token
          below and enable webhooks on each source. Enabling them requires a
          public HTTPS base URL, because Intuit must reach this server over HTTPS.
        </p>
      </div>
    </section>
  );
}

function QboAppForm({ settings }: { settings: QboAppSettings }) {
  const radioName = useId();
  const redirectId = useId();
  const save = useSaveQboSettings();
  const [environment, setEnvironment] = useState<QboEnvironment>("sandbox");
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [errors, setErrors] = useState<Errors>({});
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const { reset } = save;
  useEffect(() => () => reset(), [reset]);

  const flags = settings[environment];
  const label = ENV_LABEL[environment];
  const replacingClient = Boolean(draft.client_id || draft.client_secret);
  const edit = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((prev) => ({ ...prev, [key]: value }));

  // Unsent values and any mutation state belong to one environment only.
  function chooseEnvironment(next: QboEnvironment) {
    if (save.isPending || next === environment) return;
    setEnvironment(next);
    setDraft(EMPTY);
    setErrors({});
    setResult(null);
    reset();
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setResult(null);
    const prepared = prepare(environment, draft);
    setErrors(prepared.errors);
    if (Object.keys(prepared.errors).length) {
      setResult({ ok: false, text: "Check the highlighted fields. Nothing was sent." });
      return;
    }
    if (Object.keys(prepared.changes).length === 1) {
      reset();
      setResult({ ok: true, text: "No changes to save." });
      return;
    }
    const submitted = SECRET_FIELDS.map((key) => prepared.changes[key]).filter(
      (value): value is string => Boolean(value),
    );
    try {
      await save.mutateAsync(prepared.changes);
      setResult({ ok: true, text: `${label} app settings saved. Saved values are never shown.` });
    } catch (error) {
      setResult({ ok: false, text: failureText(error, submitted) });
    } finally {
      // Secrets leave component state and mutation variables whatever the outcome.
      setDraft(EMPTY);
      reset();
    }
  }

  return (
    <>
      <section className="admin-subsection" aria-labelledby={redirectId}>
        <h3 id={redirectId}>Redirect URI</h3>
        <p className="admin-status">
          {settings.redirect_uri ? <code style={{ overflowWrap: "anywhere" }}>{settings.redirect_uri}</code> : NO_BASE_URL}
        </p>
        <p className="admin-muted">
          Register this exact redirect URI in your Intuit app’s settings for
          each environment you use. It is the same for sandbox and production.
        </p>
      </section>
      <form className="admin-form" noValidate autoComplete="off" onSubmit={submit}>
        <fieldset className="admin-fieldset" disabled={save.isPending}>
          <fieldset className="admin-group">
            <legend>Environment</legend>
            <span className="field-help">
              Sandbox and production each use their own Intuit app keys. Only
              the selected environment is changed when you save.
            </span>
            {QBO_ENVIRONMENTS.map((env) => (
              <label key={env} className="checkbox-label">
                <input
                  type="radio"
                  name={radioName}
                  value={env}
                  checked={environment === env}
                  onChange={() => chooseEnvironment(env)}
                />
                <span>{ENV_LABEL[env]}</span>
              </label>
            ))}
          </fieldset>
          <EnvironmentFacts
            environment={environment}
            flags={flags}
            webhookUrl={settings.webhook_urls[environment]}
          />
          <fieldset className="admin-group">
            <legend>Replace or clear {label.toLowerCase()} app credentials</legend>
            <p className="admin-muted">
              Saved values are encrypted and never shown. Leave a field blank to
              keep its saved value. Entered values stay on this page only and
              are cleared after every save attempt or environment change.
            </p>
            <div className="admin-grid">
              <Field
                label="New client ID"
                help="Replaces the saved client ID. Refused while sources still hold connection credentials from this app."
                error={errors.client_id}
              >
                {(props) => (
                  <input {...props} type="password" autoComplete="new-password" spellCheck={false} disabled={draft.clear_credentials} value={draft.client_id} onChange={(e) => edit("client_id", e.target.value)} />
                )}
              </Field>
              <Field
                label="New client secret"
                help="Replaces the saved client secret. A new secret for the same app can be saved while companies stay connected."
                error={errors.client_secret}
              >
                {(props) => (
                  <input {...props} type="password" autoComplete="new-password" spellCheck={false} disabled={draft.clear_credentials} value={draft.client_secret} onChange={(e) => edit("client_secret", e.target.value)} />
                )}
              </Field>
            </div>
            {(flags.client_id_configured || flags.client_secret_configured) && (
              <>
                <label className="checkbox-label">
                  <input type="checkbox" checked={draft.clear_credentials} disabled={replacingClient} onChange={(e) => edit("clear_credentials", e.target.checked)} />
                  <span>Clear saved client ID and client secret</span>
                </label>
                {errors.clear_credentials && (
                  <p className="admin-field-error">{errors.clear_credentials}</p>
                )}
              </>
            )}
            <div className="admin-grid">
              <Field
                label="New webhook verifier token"
                help="Optional. Needed only to verify Intuit webhooks; polling works without it."
                error={errors.verifier_token}
              >
                {(props) => (
                  <input {...props} type="password" autoComplete="new-password" spellCheck={false} disabled={draft.clear_verifier_token} value={draft.verifier_token} onChange={(e) => edit("verifier_token", e.target.value)} />
                )}
              </Field>
            </div>
            {flags.verifier_token_configured && (
              <>
                <label className="checkbox-label">
                  <input type="checkbox" checked={draft.clear_verifier_token} disabled={Boolean(draft.verifier_token)} onChange={(e) => edit("clear_verifier_token", e.target.checked)} />
                  <span>Clear saved webhook verifier token</span>
                </label>
                {errors.clear_verifier_token && (
                  <p className="admin-field-error">{errors.clear_verifier_token}</p>
                )}
              </>
            )}
          </fieldset>
          <div className="admin-actions">
            <button type="submit" className="button primary">
              {save.isPending ? "Saving…" : `Save ${label.toLowerCase()} app settings`}
            </button>
          </div>
        </fieldset>
        {result && !result.ok ? (
          <p className="admin-field-error admin-group-error" role="alert">{result.text}</p>
        ) : (
          <p className="admin-status" role="status">{result?.text}</p>
        )}
      </form>
    </>
  );
}

function AdminQboSettings({ session }: { session: Session }) {
  const titleId = useId();
  const settings = useQboSettings(session);
  return (
    <section className="panel settings-panel" aria-labelledby={titleId}>
      <h2 id={titleId}>QuickBooks app settings</h2>
      <p className="admin-intro">
        Configure one Intuit app per environment here, then add one QuickBooks
        source per company in Sources. Live validation against an Intuit
        sandbox is still pending for this connector.
      </p>
      {settings.data ? (
        <>
          {settings.isError && (
            <div className="notice danger" role="alert">
              The latest QuickBooks app settings could not be loaded. The
              details below are the last values received.{" "}
              {messageOf(settings.error, "")}
            </div>
          )}
          <QboAppForm settings={settings.data} />
        </>
      ) : settings.isError ? (
        <>
          <div className="notice danger" role="alert">
            We couldn’t load the QuickBooks app settings, so their current
            state is unknown. {messageOf(settings.error, "")}
          </div>
          <button
            type="button"
            className="button secondary"
            onClick={() => void settings.refetch()}
            disabled={settings.isFetching}
          >
            {settings.isFetching ? "Trying again…" : "Try again"}
          </button>
        </>
      ) : (
        <p className="admin-muted" role="status">
          Loading QuickBooks app settings…
        </p>
      )}
    </section>
  );
}

/** Admin-only; for anyone else it renders nothing and mounts no queries. */
export function QboSettings({ session }: { session: Session }) {
  if (!canAdminister(session)) return null;
  return <AdminQboSettings session={session} />;
}
