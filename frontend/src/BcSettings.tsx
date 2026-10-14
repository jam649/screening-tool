import { useEffect, useId, useState, type FormEvent } from "react";
import { ApiError, type Session } from "./api";
import { canAdminister, messageOf } from "./adminApi";
import { Field } from "./SettingsControls";
import {
  canonicalClientId,
  clientSecretError,
  useBcSettings,
  useSaveBcSettings,
  type BcAppSettings,
  type BcSettingsChanges,
} from "./bcApi";

type Draft = { client_id: string; client_secret: string; clear_credentials: boolean };
type Errors = Partial<Record<keyof Draft, string>>;
const EMPTY: Draft = { client_id: "", client_secret: "", clear_credentials: false };

const saved = (flag: boolean) => (flag ? "Saved" : "Not saved");

/** Replacements and clears; the secret is sent byte for byte, only the client ID is canonicalized. */
function prepare(draft: Draft, settings: BcAppSettings) {
  const errors: Errors = {};
  const changes: BcSettingsChanges = {};
  if (draft.client_id) {
    const id = canonicalClientId(draft.client_id);
    if (id) changes.client_id = id;
    else
      errors.client_id =
        "Enter the application (client) ID from Microsoft Entra as a UUID: 32 hexadecimal digits in groups of 8-4-4-4-12.";
  }
  if (draft.client_secret) {
    const problem = clientSecretError(draft.client_secret);
    if (problem) errors.client_secret = problem;
    else changes.client_secret = draft.client_secret;
  }
  if (draft.client_id && !draft.client_secret && !settings.client_secret_configured)
    errors.client_secret ??=
      "No client secret is saved yet. Enter it together with the client ID.";
  if (draft.client_secret && !draft.client_id && !settings.client_id_configured)
    errors.client_id ??= "No client ID is saved yet. Enter it together with the secret.";
  if (draft.clear_credentials) {
    if (draft.client_id || draft.client_secret)
      errors.clear_credentials =
        "Either clear the saved client ID and secret or enter replacements, not both.";
    else changes.clear_credentials = true;
  }
  return { errors, changes };
}

function failureText(error: unknown, submitted: string[]) {
  if (error instanceof ApiError && error.status === 0)
    return "The server could not be reached, so it is unknown whether the change was saved. Entered values were cleared; check the saved flags below before retrying.";
  const raw = messageOf(error, "The Business Central app settings could not be saved.");
  // Never echo a submitted value back, even if the server's message contains one.
  const safe = submitted.some((value) => raw.includes(value))
    ? "The server rejected the submitted values."
    : raw;
  let hint = "";
  if (error instanceof ApiError && error.status === 409)
    hint =
      " This happens while a Business Central source is syncing or being changed, or when the client ID would be replaced or cleared while sources still hold access tokens, webhook subscriptions or pending cleanup from this app. Wait for running work to finish, or disconnect those sources in Sources and let their cleanup complete first. A new secret for the same client ID can be saved while sources are idle.";
  if (error instanceof ApiError && error.status === 422)
    hint =
      " Check that the client ID is a UUID, the secret has no control characters and is at most 8,192 characters, and that a replacement isn’t combined with clearing.";
  return `${safe}${hint} Entered values were cleared; check the saved flags before retrying.`;
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

function AppFacts({ settings }: { settings: BcAppSettings }) {
  const id = useId();
  return (
    <section className="admin-subsection" aria-labelledby={id}>
      <h3 id={id}>Entra app</h3>
      <ul className="admin-user-list" aria-label="Business Central app credentials saved on the server">
        <Fact label="Client ID" value={saved(settings.client_id_configured)} />
        <Fact label="Client secret" value={saved(settings.client_secret_configured)} />
        <Fact
          label="App configuration"
          value={
            settings.configured
              ? "Configured. Business Central sources can discover their companies."
              : "Not configured. Save the client ID and client secret before discovering companies."
          }
        />
      </ul>
      <p className="admin-muted">
        A configured app is not a connected company. In Sources, add one
        Business Central source per company with its tenant ID and environment
        name, then discover companies and explicitly connect the one you choose.
        Each source shows its own connection status.{" "}
        <a href="/sources">Open Sources</a>
      </p>
    </section>
  );
}

function Requirements() {
  const id = useId();
  return (
    <section className="admin-subsection" aria-labelledby={id}>
      <h3 id={id}>What the Entra app needs</h3>
      <p className="admin-muted">
        This connector signs in as the app itself using the client credentials
        flow. There is no user sign-in or redirect.
      </p>
      <ul className="admin-user-list" aria-label="Entra app requirements">
        <li className="admin-muted">
          Register one app in Microsoft Entra ID and create a client secret for it.
          All Business Central sources in this workspace use this app.
        </li>
        <li className="admin-muted">
          Add the Dynamics 365 Business Central application permission{" "}
          <code>API.ReadWrite.All</code> and grant admin consent in each tenant
          you connect.
        </li>
        <li className="admin-muted">
          In each Business Central environment, add the app on the Microsoft
          Entra Applications page, set it to Enabled and assign restricted
          permission sets covering only what this connector uses. Don’t assign
          SUPER.
        </li>
        <li className="admin-muted">
          Disconnecting a source stops its polling here and requests cleanup of its
          webhook subscriptions; the source shows the actual cleanup outcome.
          It doesn’t withdraw the app’s consent. Remove consent
          or disable the app in Entra or Business Central if you no longer want
          it to have access.
        </li>
      </ul>
    </section>
  );
}

function WebhookBase({ url }: { url: string | null }) {
  return (
    <div className="admin-details">
      <p className="admin-status">
        <strong>Webhook base URL</strong>
      </p>
      <p className="admin-status">
        {url ? (
          <code>{url}</code>
        ) : (
          "Not set. Webhooks need a public HTTPS base URL: set the Public base URL in the Email notifications settings (SMTP server section), then check it here."
        )}
      </p>
      <p className="admin-muted">
        Webhooks are optional and only prompt an earlier poll; sources are
        polled on a schedule either way. Company discovery, connecting and
        polling don’t need a public URL. Each connected source registers its own
        subscriptions and shows its own webhook status.
      </p>
    </div>
  );
}

function BcAppForm({ settings }: { settings: BcAppSettings }) {
  const save = useSaveBcSettings();
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [errors, setErrors] = useState<Errors>({});
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const { reset } = save;
  useEffect(() => () => reset(), [reset]);

  const replacing = Boolean(draft.client_id || draft.client_secret);
  const edgeSpaces = draft.client_secret !== draft.client_secret.trim();
  const edit = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((prev) => ({ ...prev, [key]: value }));

  async function submit(event: FormEvent) {
    event.preventDefault();
    setResult(null);
    const prepared = prepare(draft, settings);
    setErrors(prepared.errors);
    if (Object.keys(prepared.errors).length) {
      setDraft(EMPTY);
      reset();
      setResult({
        ok: false,
        text: "Check the highlighted fields. Nothing was sent, and entered values were cleared.",
      });
      return;
    }
    if (!Object.keys(prepared.changes).length) {
      reset();
      setResult({ ok: true, text: "No changes to save." });
      return;
    }
    const submitted = [
      prepared.changes.client_id,
      draft.client_id.trim(),
      draft.client_secret,
      draft.client_secret.trim(),
    ].filter((value): value is string => Boolean(value));
    try {
      await save.mutateAsync(prepared.changes);
      setResult({ ok: true, text: "Business Central app settings saved. Saved values are never shown." });
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
      <AppFacts settings={settings} />
      <Requirements />
      <WebhookBase url={settings.webhook_base_url} />
      <form className="admin-form" noValidate autoComplete="off" onSubmit={submit}>
        <fieldset className="admin-fieldset" disabled={save.isPending}>
          <fieldset className="admin-group">
            <legend>Replace or clear app credentials</legend>
            <p className="admin-muted">
              Saved values are encrypted and never shown. Leave both fields
              blank to keep the saved values. Entered values stay on this page
              only and are cleared after every save attempt.
            </p>
            <div className="admin-grid">
              <Field
                label="New application (client) ID"
                help="The Entra application ID, a UUID. Surrounding spaces are removed and letters are saved in lowercase. Leave the secret blank to preserve a saved secret. Refused while sources still hold tokens, subscriptions or cleanup from the current app."
                error={errors.client_id}
              >
                {(props) => (
                  <input {...props} type="password" autoComplete="new-password" spellCheck={false} disabled={draft.clear_credentials} value={draft.client_id} onChange={(e) => edit("client_id", e.target.value)} />
                )}
              </Field>
              <Field
                label="New client secret"
                help="Saved exactly as entered, including any leading or trailing spaces. Control characters such as tabs are refused. A new secret for the saved client ID can be saved while sources are idle."
                error={errors.client_secret}
              >
                {(props) => (
                  <input {...props} type="password" autoComplete="new-password" spellCheck={false} disabled={draft.clear_credentials} value={draft.client_secret} onChange={(e) => edit("client_secret", e.target.value)} />
                )}
              </Field>
            </div>
            {edgeSpaces && (
              <p className="admin-warning">
                The secret starts or ends with spaces. They will be saved as
                part of the secret, unchanged.
              </p>
            )}
            {(settings.client_id_configured || settings.client_secret_configured) && (
              <>
                <label className="checkbox-label">
                  <input type="checkbox" checked={draft.clear_credentials} disabled={replacing} onChange={(e) => edit("clear_credentials", e.target.checked)} />
                  <span>Clear saved client ID and client secret</span>
                </label>
                {errors.clear_credentials && (
                  <p className="admin-field-error">{errors.clear_credentials}</p>
                )}
              </>
            )}
          </fieldset>
          <div className="admin-actions">
            <button type="submit" className="button primary">
              {save.isPending ? "Saving…" : "Save Business Central app settings"}
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

function AdminBcSettings({ session }: { session: Session }) {
  const titleId = useId();
  const settings = useBcSettings(session);
  return (
    <section className="panel settings-panel" aria-labelledby={titleId}>
      <h2 id={titleId}>Business Central app settings</h2>
      <p className="admin-intro">
        Configure one Microsoft Entra app for every Business Central source,
        then add one source per company in Sources. Live validation against a
        Business Central tenant is still pending for this connector.
      </p>
      {settings.data ? (
        <>
          {settings.isError && (
            <div className="notice danger" role="alert">
              The latest Business Central app settings could not be loaded.
              The details below are the last values received.{" "}
              {messageOf(settings.error, "")}
            </div>
          )}
          <BcAppForm settings={settings.data} />
        </>
      ) : settings.isError ? (
        <>
          <div className="notice danger" role="alert">
            We couldn’t load the Business Central app settings, so their
            current state is unknown. {messageOf(settings.error, "")}
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
          Loading Business Central app settings…
        </p>
      )}
    </section>
  );
}

/** Admin-only; for anyone else it renders nothing and mounts no queries. */
export function BcSettings({ session }: { session: Session }) {
  if (!canAdminister(session)) return null;
  return <AdminBcSettings session={session} />;
}
