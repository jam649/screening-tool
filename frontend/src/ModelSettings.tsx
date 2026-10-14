import { useEffect, useId, useState, type FormEvent } from "react";
import { ApiError, type Session } from "./api";
import { canAdminister, parseNumber } from "./adminApi";
import { Field } from "./SettingsControls";
import {
  WEIGHT_SUM_TOLERANCE,
  keyError,
  localEndpointError,
  useModelSettings,
  useSaveModelSettings,
  useTestModelConnection,
  type ModelConfig,
  type ModelProvider,
  type ModelSettingsChanges,
  type ModelTestResult,
  type SavedModelConfig,
} from "./modelApi";

type Draft = {
  provider: ModelProvider;
  w_det: string;
  w_model: string;
  laya_base_url: string;
  jev_api_key: string;
  laya_api_key: string;
  clear_jev_api_key: boolean;
  clear_laya_api_key: boolean;
  acknowledged: boolean;
};
type Errors = Partial<Record<keyof Draft | "weights", string>>;
type Message = { ok: boolean; text: string };
type Latest = { data?: ModelConfig; isError: boolean };

// Everything a save attempt or provider change must discard.
const NO_SECRETS = {
  jev_api_key: "",
  laya_api_key: "",
  clear_jev_api_key: false,
  clear_laya_api_key: false,
  acknowledged: false,
};

const PROVIDER_NAMES: Record<ModelProvider, string> = { none: "None", jev: "Jev", laya: "Laya" };
const providerName = (provider: string) =>
  provider === "none" || provider === "jev" || provider === "laya"
    ? PROVIDER_NAMES[provider]
    : "Unknown provider";
const saved = (flag: boolean) => (flag ? "Saved" : "Not saved");
const reported = (value: number | null) => (value === null ? "Not reported" : String(value));
const SAFE_CODE = /^[A-Za-z0-9_.:-]{1,64}$/;

const draftFrom = (config: ModelConfig): Draft => ({
  provider: config.provider,
  w_det: String(config.w_det),
  w_model: String(config.w_model),
  laya_base_url: config.laya_base_url,
  ...NO_SECRETS,
});

/** Only changed fields, at the generation the form was loaded from; keys are sent byte for byte. */
function prepare(draft: Draft, base: ModelConfig) {
  const errors: Errors = {};
  const changes: ModelSettingsChanges = { expected_generation: base.generation };
  if (draft.provider !== base.provider) changes.provider = draft.provider;

  const det = parseNumber(draft.w_det);
  const model = parseNumber(draft.w_model);
  if (det === null || det < 0 || det > 1)
    errors.w_det = "Enter a number from 0 to 1, such as 1 or 0.75.";
  if (model === null || model < 0 || model > 1)
    errors.w_model = "Enter a number from 0 to 1, such as 0 or 0.25.";
  if (det !== null && model !== null && !errors.w_det && !errors.w_model) {
    if (Math.abs(det + model - 1) > WEIGHT_SUM_TOLERANCE)
      errors.weights = `The two weights must add up to 1; they add up to ${Number((det + model).toPrecision(12))}.`;
    else {
      if (det !== base.w_det) changes.w_det = det;
      if (model !== base.w_model) changes.w_model = model;
    }
  }

  if (draft.provider === "laya" && draft.laya_base_url !== base.laya_base_url) {
    const problem = localEndpointError(draft.laya_base_url);
    if (problem) errors.laya_base_url = problem;
    else changes.laya_base_url = draft.laya_base_url;
  }

  if (draft.jev_api_key) {
    const problem = keyError(draft.jev_api_key);
    if (problem) errors.jev_api_key = problem;
    else if (draft.clear_jev_api_key)
      errors.clear_jev_api_key = "Either clear the saved Jev key or enter a replacement, not both.";
    else if (!base.privacy_notice)
      errors.jev_api_key = "The server didn’t provide the hosted data notice, so a Jev key can’t be saved.";
    else if (!draft.acknowledged)
      errors.acknowledged = "Every new Jev key needs this acknowledgment. Enter the key again and check the box.";
    else {
      changes.jev_api_key = draft.jev_api_key;
      changes.hosted_acknowledgment = base.privacy_notice;
    }
  } else if (draft.clear_jev_api_key) {
    if (draft.provider === "jev")
      errors.clear_jev_api_key = "Jev needs a saved key. Select another provider before clearing it.";
    else changes.clear_jev_api_key = true;
  }
  if (draft.provider === "jev" && changes.provider === "jev" && !draft.jev_api_key) {
    if (!base.jev_api_key_configured)
      errors.jev_api_key ??= "No Jev key is saved. Enter a key and acknowledge the notice to use Jev.";
    else if (!base.hosted_acknowledgment_valid)
      errors.jev_api_key ??=
        "The saved Jev key has no valid acknowledgment of the current notice. Enter the key again and acknowledge the notice.";
  }

  if (draft.laya_api_key) {
    const problem = keyError(draft.laya_api_key);
    if (problem) errors.laya_api_key = problem;
    else if (draft.clear_laya_api_key)
      errors.clear_laya_api_key = "Either clear the saved Laya key or enter a replacement, not both.";
    else changes.laya_api_key = draft.laya_api_key;
  } else if (draft.clear_laya_api_key) changes.clear_laya_api_key = true;

  return { errors, changes };
}

function savedText(result: SavedModelConfig, previousGeneration: number) {
  if (result.generation === previousGeneration && !result.pending_screening_task_id)
    return `The server found no effective change: the configuration stays at generation ${result.generation} and no screening was queued. Entered keys were cleared.`;
  const queued = result.pending_screening_task_id
    ? ` A full re-screen was queued as task ${result.pending_screening_task_id}; earlier human decisions and history are kept, and results update as the task runs.`
    : " No re-screen was queued.";
  return `Saved as configuration generation ${result.generation}.${queued} Entered keys were cleared; saved keys are never shown.`;
}

// Fixed text only: server messages can echo submitted values, so they are never shown.
function saveFailure(error: unknown) {
  const status = error instanceof ApiError ? error.status : -1;
  if (status === 0)
    return "The server could not be reached, so it is unknown whether the change was saved. Entered keys and the acknowledgment were cleared and the saved settings are being reloaded; check them before saving again.";
  if (status === 409)
    return "The model configuration changed on the server after this form was loaded, so nothing from this form was saved. Entered keys were cleared. Review the latest saved settings, then choose Reload configuration before saving again.";
  if (status === 422)
    return "The server rejected the change. Check that the weights are between 0 and 1 and add up to 1, the Laya endpoint is local, keys have no control characters and are at most 8,192 characters, a replacement isn’t combined with clearing, and Jev has a key with the acknowledgment. Entered keys were cleared.";
  if (status === 403)
    return "The server refused the change for this account. Entered keys were cleared.";
  return `The model settings could not be saved${status > 0 ? ` (HTTP ${status})` : ""}. Entered keys were cleared; check the saved settings before retrying.`;
}

function loadFailure(error: unknown) {
  const status = error instanceof ApiError ? error.status : -1;
  if (status === 404)
    return "This server doesn’t provide the model configuration (HTTP 404), so the model settings are unknown. No defaults are shown.";
  if (status === 403) return "Your account can’t view the model settings.";
  if (status === 0) return "The server could not be reached, so the model settings are unknown.";
  return `The model settings could not be loaded${status > 0 ? ` (HTTP ${status})` : ""}, so their current state is unknown.`;
}

function testFailureText(error: unknown) {
  const status = error instanceof ApiError ? error.status : -1;
  if (status === 0)
    return "The server could not be reached, so the outcome of the connection test is unknown.";
  if (status === 409)
    return "The saved configuration changed during the test, so no result for the current configuration was accepted. The latest saved settings are being reloaded; review them and test again.";
  if (status === 422) return "The server has no saved provider to test. Save Jev or Laya first.";
  if (status === 403) return "The server refused the connection test for this account.";
  return `The connection test could not be run${status > 0 ? ` (HTTP ${status})` : ""}.`;
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

function SavedFacts({ config }: { config: ModelConfig }) {
  const id = useId();
  return (
    <section className="admin-subsection" aria-labelledby={id}>
      <h3 id={id}>Saved configuration</h3>
      <ul className="admin-user-list" aria-label="Model configuration saved on the server">
        <Fact
          label="Provider"
          value={config.provider === "none" ? "None (deterministic screening only)" : providerName(config.provider)}
        />
        <Fact label="Configuration generation" value={String(config.generation)} />
        <Fact label="Blend weights" value={`Deterministic ${config.w_det}, model ${config.w_model}`} />
        <Fact label="Jev model" value={`${config.jev_model} (pinned, read-only)`} />
        <Fact label="Laya endpoint" value={config.laya_base_url || "Not set"} />
        <Fact
          label="Laya model"
          value={`${config.laya_model}, ${config.laya_max_tokens}-token budget (pinned, read-only)`}
        />
        <Fact label="Jev API key" value={saved(config.jev_api_key_configured)} />
        <Fact label="Laya API key" value={saved(config.laya_api_key_configured)} />
        <Fact
          label="Hosted data acknowledgment"
          value={config.hosted_acknowledgment_valid ? "Valid for the saved Jev key" : "Not recorded for a saved Jev key"}
        />
      </ul>
      <p className="admin-muted">
        A saved key or endpoint is configuration, not a working connection.
        Use the connection test below to check the saved provider. A passing
        test isn’t evidence of screening quality or calibration.
      </p>
    </section>
  );
}

function HowItWorks() {
  const id = useId();
  return (
    <section className="admin-subsection" aria-labelledby={id}>
      <h3 id={id}>How the model is used</h3>
      <ul className="admin-user-list" aria-label="How model adjudication affects screening">
        <li className="admin-muted">
          None keeps screening fully deterministic and calls no model.
        </li>
        <li className="admin-muted">
          A model never generates candidates. It only scores comparisons the
          deterministic matcher already found, and a high deterministic likely
          match is never demoted by a low model score.
        </li>
        <li className="admin-muted">
          The defaults are deterministic weight 1 and model weight 0. In the
          actual local measurement of 121 requests, model scores didn’t improve
          screening quality enough to justify a positive weight, so by default a
          selected model’s probability is recorded without changing confidence.
        </li>
        <li className="admin-muted">
          Changing the provider, weights or endpoint requests a full re-screen when a CSL version is available.
          Earlier human decisions and history are kept.
        </li>
      </ul>
    </section>
  );
}

function TestOutcome({ result, savedGeneration }: { result: ModelTestResult; savedGeneration: number }) {
  const name = providerName(result.provider);
  const code = result.error_code && SAFE_CODE.test(result.error_code) ? result.error_code : null;
  return (
    <>
      {result.generation !== savedGeneration && (
        <p className="admin-warning">
          This result is historical: it tested configuration generation{" "}
          {result.generation}, and the saved configuration is now generation{" "}
          {savedGeneration}. Test again to check the current configuration.
        </p>
      )}
      {result.status === "connected" ? (
        <p className="admin-status" role="status">
          Connected: the saved {name} configuration answered the synthetic test
          request. This doesn’t prove screening quality or calibration.
        </p>
      ) : (
        <p className="admin-field-error admin-group-error" role="alert" style={{ overflowWrap: "anywhere" }}>
          The saved {name} configuration failed the connection test
          {code ? <> with error code <code>{code}</code></> : " with an unrecognized error code"}.
          Screening falls back to deterministic confidence when the model can’t answer.
        </p>
      )}
      <ul className="admin-user-list" aria-label="Tested configuration facts">
        <Fact label="Tested provider" value={name} />
        <Fact label="Tested generation" value={String(result.generation)} />
        <Fact label="Requested model" value={result.requested_model} />
        <Fact label="Returned model" value={result.returned_model ?? "Not reported"} />
        <Fact label="Model probability" value={reported(result.model_prob)} />
        <Fact label="Input tokens" value={reported(result.usage.input_tokens)} />
        <Fact label="Output tokens" value={reported(result.usage.output_tokens)} />
        <Fact label="Attempts" value={String(result.attempts)} />
        <Fact label="Elapsed" value={`${result.elapsed_ms} ms`} />
      </ul>
    </>
  );
}

function ModelForm({ latest, reloadLatest }: { latest: ModelConfig; reloadLatest: () => Promise<Latest> }) {
  const save = useSaveModelSettings();
  const connection = useTestModelConnection();
  const testId = useId();
  const noticeId = useId();
  const [baseline, setBaseline] = useState<ModelConfig>(latest);
  const [draft, setDraft] = useState<Draft>(() => draftFrom(latest));
  const [errors, setErrors] = useState<Errors>({});
  const [message, setMessage] = useState<Message | null>(null);
  const [conflict, setConflict] = useState(false);
  const [reloading, setReloading] = useState(false);
  const [tested, setTested] = useState<ModelTestResult | null>(null);
  const [testError, setTestError] = useState<string | null>(null);
  const { reset: resetSave } = save;
  const { reset: resetTest } = connection;
  useEffect(() => () => {
    resetSave();
    resetTest();
  }, [resetSave, resetTest]);

  // Never rebase a draft onto a generation the admin hasn't reviewed.
  const stale = conflict || latest.generation !== baseline.generation;
  const dirty =
    draft.provider !== baseline.provider ||
    draft.w_det !== String(baseline.w_det) ||
    draft.w_model !== String(baseline.w_model) ||
    draft.laya_base_url !== baseline.laya_base_url ||
    Boolean(draft.jev_api_key || draft.laya_api_key) ||
    draft.clear_jev_api_key ||
    draft.clear_laya_api_key;
  const edit = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((prev) => ({ ...prev, [key]: value }));
  const clearTest = () => {
    setTested(null);
    setTestError(null);
  };

  function chooseProvider(provider: ModelProvider) {
    setDraft((prev) => ({ ...prev, ...NO_SECRETS, provider, laya_base_url: provider === "laya" ? prev.laya_base_url : baseline.laya_base_url }));
    setErrors({});
    clearTest();
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (stale) return;
    setMessage(null);
    clearTest();
    const prepared = prepare(draft, baseline);
    setErrors(prepared.errors);
    // Keys, clears and the acknowledgment leave component state whatever the outcome.
    setDraft((prev) => ({ ...prev, ...NO_SECRETS }));
    if (Object.keys(prepared.errors).length) {
      resetSave();
      setMessage({
        ok: false,
        text: "Check the highlighted fields. Nothing was sent, and entered keys and the acknowledgment were cleared.",
      });
      return;
    }
    if (Object.keys(prepared.changes).length === 1) {
      resetSave();
      // Equivalent numeric formatting is no unsaved configuration change.
      setDraft(draftFrom(baseline));
      setMessage({ ok: true, text: "No changes to save." });
      return;
    }
    try {
      const result = await save.mutateAsync(prepared.changes);
      setBaseline(result);
      setDraft(draftFrom(result));
      setMessage({ ok: true, text: savedText(result, baseline.generation) });
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) setConflict(true);
      setMessage({ ok: false, text: saveFailure(error) });
    } finally {
      resetSave();
    }
  }

  async function reload() {
    setReloading(true);
    setMessage(null);
    setErrors({});
    clearTest();
    try {
      const fresh = await reloadLatest();
      if (fresh.isError || !fresh.data) {
        setMessage({ ok: false, text: "The latest saved settings could not be loaded. Try again." });
        return;
      }
      setBaseline(fresh.data);
      setDraft(draftFrom(fresh.data));
      setConflict(false);
    } finally {
      setReloading(false);
    }
  }

  async function runTest() {
    clearTest();
    try {
      setTested(await connection.mutateAsync({ expected_generation: latest.generation }));
    } catch (error) {
      setTestError(testFailureText(error));
    } finally {
      resetTest();
    }
  }

  const savedName = providerName(latest.provider);
  const testHelp =
    latest.provider === "none"
      ? "Select Jev or Laya and save it before testing. None calls no model."
      : stale
        ? "Reload configuration before testing."
        : dirty
          ? "Save or undo your changes first: the test only uses the saved configuration, never unsaved keys, provider or endpoint."
          : latest.provider === "jev"
            ? "Uses synthetic data with the saved Jev key. Provider requests may incur TypeSafe AI charges; this test does not verify your account’s billing or entitlements."
            : "Sends one synthetic request to the saved local Laya endpoint.";
  const jevProof = baseline.jev_api_key_configured
    ? baseline.hosted_acknowledgment_valid
      ? "A Jev key is saved with a valid acknowledgment of this notice. Leave the key blank to keep using it."
      : "A Jev key is saved, but without a valid acknowledgment of this notice. To use Jev, enter the key again and acknowledge the notice."
    : "No Jev key is saved. Enter one and acknowledge the notice to use Jev.";
  const spaces = (key: string) => key !== key.trim();

  return (
    <>
      <SavedFacts config={latest} />
      <HowItWorks />
      {stale && (
        <div className="notice danger" role="alert">
          <p>
            {conflict
              ? "Your save was refused because the model configuration changed on the server."
              : "The saved model configuration changed on the server."}{" "}
            The saved configuration above is generation {latest.generation};
            this form was loaded from generation {baseline.generation}. Saving is
            paused so your draft isn’t applied on top of settings you haven’t
            reviewed. Reload configuration to discard this form’s unsaved
            changes and start from the latest saved settings.
          </p>
          <button type="button" className="button secondary" onClick={() => void reload()} disabled={reloading}>
            {reloading ? "Reloading…" : "Reload configuration"}
          </button>
        </div>
      )}
      <form className="admin-form" noValidate autoComplete="off" onSubmit={submit}>
        <fieldset className="admin-fieldset" disabled={save.isPending || stale}>
          <fieldset className="admin-group">
            <legend>Provider</legend>
            <div className="admin-grid admin-grid-tight">
              <Field
                label="Model provider"
                help="Jev is hosted by TypeSafe AI. Laya runs on this server. Changing the provider discards keys entered for the previous one."
              >
                {(props) => (
                  <select {...props} value={draft.provider} onChange={(e) => chooseProvider(e.target.value as ModelProvider)}>
                    <option value="none">None (deterministic only)</option>
                    <option value="jev">Jev (hosted by TypeSafe AI)</option>
                    <option value="laya">Laya (local on this server)</option>
                  </select>
                )}
              </Field>
            </div>
          </fieldset>

          {draft.provider === "jev" && (
            <fieldset className="admin-group">
              <legend>Jev</legend>
              <p className="admin-warning" id={noticeId}>{baseline.privacy_notice}</p>
              <p className="admin-muted">
                Model {baseline.jev_model} is pinned. {jevProof}
              </p>
              <div className="admin-grid">
                <Field
                  label="New Jev API key"
                  help="Saved exactly as entered, including leading or trailing spaces, and never shown again. Leave blank to keep the saved key."
                  error={errors.jev_api_key}
                >
                  {(props) => (
                    <input
                      {...props}
                      type="password"
                      autoComplete="new-password"
                      spellCheck={false}
                      disabled={draft.clear_jev_api_key}
                      value={draft.jev_api_key}
                      onChange={(e) => {
                        const value = e.target.value;
                        // A changed key always needs a fresh acknowledgment.
                        setDraft((prev) => ({ ...prev, jev_api_key: value, acknowledged: false }));
                      }}
                    />
                  )}
                </Field>
              </div>
              {spaces(draft.jev_api_key) && (
                <p className="admin-warning">
                  The key starts or ends with spaces. They will be saved as part of the key, unchanged.
                </p>
              )}
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={draft.acknowledged}
                  disabled={!draft.jev_api_key || !baseline.privacy_notice}
                  aria-describedby={noticeId}
                  onChange={(e) => edit("acknowledged", e.target.checked)}
                />
                <span>
                  I have read the notice above and agree to send customer and
                  vendor names and addresses to TypeSafe AI with this new Jev key
                </span>
              </label>
              {errors.acknowledged && <p className="admin-field-error">{errors.acknowledged}</p>}
            </fieldset>
          )}

          {draft.provider === "laya" && (
            <fieldset className="admin-group">
              <legend>Laya</legend>
              <p className="admin-muted">
                Laya keeps all data on this server. Model {baseline.laya_model} with a{" "}
                {baseline.laya_max_tokens}-token budget is pinned; /v1/systemone is
                added to the endpoint automatically.
              </p>
              <div className="admin-grid">
                <Field
                  label="Laya endpoint"
                  help="The Laya server on this machine: http or https with localhost, a loopback address such as 127.0.0.1 or [::1], or host.docker.internal, plus the port. No path, query or other hosts. If this app runs in Docker and Laya runs on the same physical host, use http://host.docker.internal:8009."
                  error={errors.laya_base_url}
                >
                  {(props) => (
                    <input
                      {...props}
                      type="text"
                      inputMode="url"
                      spellCheck={false}
                      value={draft.laya_base_url}
                      onChange={(e) => edit("laya_base_url", e.target.value)}
                    />
                  )}
                </Field>
                <Field
                  label="New Laya API key"
                  help="Only if your Laya server requires one. Saved exactly as entered and never shown again. Leave blank to keep the saved key."
                  error={errors.laya_api_key}
                >
                  {(props) => (
                    <input
                      {...props}
                      type="password"
                      autoComplete="new-password"
                      spellCheck={false}
                      disabled={draft.clear_laya_api_key}
                      value={draft.laya_api_key}
                      onChange={(e) => edit("laya_api_key", e.target.value)}
                    />
                  )}
                </Field>
              </div>
              {spaces(draft.laya_api_key) && (
                <p className="admin-warning">
                  The key starts or ends with spaces. They will be saved as part of the key, unchanged.
                </p>
              )}
            </fieldset>
          )}

          <fieldset className="admin-group">
            <legend>Blend weights</legend>
            <p className="admin-muted">
              Two numbers from 0 to 1 that add up to 1. They are saved exactly as
              entered; neither is adjusted for you.
            </p>
            <div className="admin-grid">
              <Field label="Deterministic weight" help="Default 1." error={errors.w_det}>
                {(props) => (
                  <input {...props} type="text" inputMode="decimal" value={draft.w_det} onChange={(e) => edit("w_det", e.target.value)} />
                )}
              </Field>
              <Field label="Model weight" help="Default 0." error={errors.w_model}>
                {(props) => (
                  <input {...props} type="text" inputMode="decimal" value={draft.w_model} onChange={(e) => edit("w_model", e.target.value)} />
                )}
              </Field>
            </div>
            {errors.weights && <p className="admin-field-error admin-group-error">{errors.weights}</p>}
          </fieldset>

          {(baseline.jev_api_key_configured || baseline.laya_api_key_configured) && (
            <fieldset className="admin-group">
              <legend>Clear saved keys</legend>
              <p className="admin-muted">
                Keys you don’t replace or clear stay saved, including the key of a provider that isn’t selected.
              </p>
              {baseline.jev_api_key_configured && (
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={draft.clear_jev_api_key}
                    disabled={Boolean(draft.jev_api_key)}
                    onChange={(e) => edit("clear_jev_api_key", e.target.checked)}
                  />
                  <span>Clear the saved Jev API key</span>
                </label>
              )}
              {errors.clear_jev_api_key && <p className="admin-field-error">{errors.clear_jev_api_key}</p>}
              {baseline.laya_api_key_configured && (
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={draft.clear_laya_api_key}
                    disabled={Boolean(draft.laya_api_key)}
                    onChange={(e) => edit("clear_laya_api_key", e.target.checked)}
                  />
                  <span>Clear the saved Laya API key</span>
                </label>
              )}
              {errors.clear_laya_api_key && <p className="admin-field-error">{errors.clear_laya_api_key}</p>}
            </fieldset>
          )}

          <div className="admin-actions">
            <button type="submit" className="button primary">
              {save.isPending ? "Saving…" : "Save model settings"}
            </button>
          </div>
        </fieldset>
        {message && !message.ok ? (
          <p className="admin-field-error admin-group-error" role="alert">{message.text}</p>
        ) : (
          <p className="admin-status" role="status" style={{ overflowWrap: "anywhere" }}>{message?.text}</p>
        )}
      </form>

      <section className="admin-subsection" aria-labelledby={testId}>
        <h3 id={testId}>Connection test</h3>
        <p className="admin-muted">
          Tests the saved configuration with fixed synthetic data. No customer or
          vendor records are sent or screened, nothing is queued, and the browser
          doesn’t retry the request.
        </p>
        <div className="admin-actions">
          <button
            type="button"
            className="button secondary"
            aria-describedby={`${testId}-help`}
            disabled={latest.provider === "none" || save.isPending || connection.isPending || dirty || stale}
            onClick={() => void runTest()}
          >
            {connection.isPending
              ? `Testing saved ${savedName} connection…`
              : latest.provider === "none"
                ? "Test saved connection"
                : `Test saved ${savedName} connection`}
          </button>
        </div>
        <p id={`${testId}-help`} className="field-help">{testHelp}</p>
        {connection.isPending && (
          <p className="admin-status" role="status">
            Waiting for the saved {savedName} configuration to answer. This can take a while.
          </p>
        )}
        {testError && <p className="admin-field-error admin-group-error" role="alert">{testError}</p>}
        {tested && <TestOutcome result={tested} savedGeneration={latest.generation} />}
      </section>
    </>
  );
}

function AdminModelSettings({ session }: { session: Session }) {
  const titleId = useId();
  const settings = useModelSettings(session);
  return (
    <section className="panel settings-panel" aria-labelledby={titleId}>
      <h2 id={titleId}>Model settings</h2>
      <p className="admin-intro">
        Optionally have a model adjudicator score comparisons the deterministic
        matcher already found. Deterministic screening stays authoritative.
      </p>
      {settings.data ? (
        <>
          {settings.isError && (
            <div className="notice danger" role="alert">
              The latest model settings could not be loaded. The details below
              are the last values received. {loadFailure(settings.error)}
            </div>
          )}
          <ModelForm latest={settings.data} reloadLatest={() => settings.refetch()} />
        </>
      ) : settings.isError ? (
        <>
          <div className="notice danger" role="alert">{loadFailure(settings.error)}</div>
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
        <p className="admin-muted" role="status">Loading model settings…</p>
      )}
    </section>
  );
}

/** Admin-only; for anyone else it renders nothing and mounts no queries. */
export function ModelSettings({ session }: { session: Session }) {
  if (!canAdminister(session)) return null;
  return <AdminModelSettings session={session} />;
}
