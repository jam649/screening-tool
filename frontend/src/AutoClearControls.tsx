import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import type { Session } from "./api";
import {
  canAdminister,
  changedSettings,
  messageOf,
  parseNumber,
  useSaveSettings,
  useSettings,
  type SettingsChanges,
  type WorkspaceSettings,
} from "./adminApi";
import { Field } from "./SettingsControls";

export const AUTO_CLEAR_PHRASE = "I understand";
export const AUTO_CLEAR_NOTICE =
  "Future review items scoring below the chosen value will be recorded without human review. Existing review items remain until screened again.";

type Draft = { enabled: boolean; threshold: string };
type AutoClearState = Pick<
  WorkspaceSettings,
  | "auto_clear_enabled"
  | "auto_clear_threshold"
  | "review_threshold"
  | "likely_threshold"
>;

const whole = (value: unknown): value is number =>
  typeof value === "number" && Number.isInteger(value);

/** The saved auto-clear state, or null when the server omitted or malformed a field it depends on. */
function autoClearState(
  settings: Partial<WorkspaceSettings> | undefined,
): AutoClearState | null {
  if (!settings) return null;
  const {
    auto_clear_enabled,
    auto_clear_threshold,
    review_threshold,
    likely_threshold,
  } = settings;
  if (
    typeof auto_clear_enabled !== "boolean" ||
    !whole(auto_clear_threshold) ||
    typeof review_threshold !== "number" || !Number.isFinite(review_threshold) ||
    typeof likely_threshold !== "number" || !Number.isFinite(likely_threshold) ||
    review_threshold > likely_threshold
  )
    return null;
  return {
    auto_clear_enabled,
    auto_clear_threshold,
    review_threshold,
    likely_threshold,
  };
}

export function AutoClearControls({ session }: { session: Session }) {
  const settings = useSettings(session);
  const save = useSaveSettings();
  const [draft, setDraft] = useState<Draft | null>(null);
  const [ack, setAck] = useState("");
  const [status, setStatus] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [echoError, setEchoError] = useState("");
  const headingId = useId();
  const noticeId = useId();
  const dialogHeadingId = useId();
  const dialogNoticeId = useId();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const checkboxRef = useRef<HTMLInputElement>(null);
  const phraseRef = useRef<HTMLInputElement>(null);

  // Keep the native modal in step with state; focus moves to the phrase and back to the checkbox.
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (confirming && !dialog.open) {
      dialog.showModal();
      phraseRef.current?.focus();
    } else if (!confirming && dialog.open) {
      dialog.close();
      checkboxRef.current?.focus();
    }
  }, [confirming]);

  if (!canAdminister(session)) return null;

  const retry = (
    <button
      type="button"
      className="button secondary admin-compact"
      onClick={() => void settings.refetch()}
      disabled={settings.isFetching}
    >
      Retry
    </button>
  );
  const shell = (body: React.ReactNode) => (
    <section className="panel settings-panel" aria-labelledby={headingId}>
      <h2 id={headingId}>Auto-clear</h2>
      {body}
    </section>
  );

  if (!settings.data) {
    if (settings.isError)
      return shell(
        <div role="alert">
          <p className="admin-field-error">
            {messageOf(settings.error, "Auto-clear settings could not be loaded.")}
          </p>
          <div className="form-actions">{retry}</div>
        </div>,
      );
    return shell(
      <p className="rv-muted" role="status">
        Loading auto-clear settings…
      </p>,
    );
  }

  const loaded = settings.data;
  const saved = autoClearState(loaded);
  if (!saved)
    return shell(
      <div role="alert">
        <p className="admin-field-error">
          Auto-clear settings are unavailable because the server response is
          incomplete. No auto-clear changes can be applied.
        </p>
        <div className="form-actions">{retry}</div>
      </div>,
    );

  const min = Math.ceil(saved.review_threshold);
  const max = Math.floor(saved.likely_threshold);
  const current: Draft = draft ?? {
    enabled: saved.auto_clear_enabled,
    threshold: String(saved.auto_clear_threshold),
  };
  const parsed = parseNumber(current.threshold);
  const thresholdValid =
    parsed !== null && Number.isInteger(parsed) && parsed >= min && parsed <= max;
  const changes: SettingsChanges = thresholdValid
    ? changedSettings(loaded, {
        auto_clear_enabled: current.enabled,
        auto_clear_threshold: parsed,
      })
    : {};
  const turningOn = current.enabled && !saved.auto_clear_enabled;
  const acknowledged = !turningOn || ack === AUTO_CLEAR_PHRASE;
  const canApply =
    thresholdValid &&
    Object.keys(changes).length > 0 &&
    acknowledged &&
    !save.isPending;
  const sliderValue = Math.min(
    max,
    Math.max(min, Math.round(parsed ?? saved.auto_clear_threshold)),
  );
  const saveError = save.isError && (
    <p className="admin-field-error admin-group-error" role="alert">
      {messageOf(save.error, "Auto-clear settings could not be saved.")}
    </p>
  );

  const edit = (next: Partial<Draft>) => {
    setDraft({ ...current, ...next });
    setStatus("");
    setEchoError("");
    if (save.isError) save.reset();
  };

  // Escape or Cancel restores the saved off state; the threshold edit is kept.
  const cancel = () => {
    if (save.isPending) return;
    setAck("");
    edit({ enabled: false });
    setConfirming(false);
  };

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canApply) return;
    // The phrase travels only with this off-to-on request; it is never a stored setting.
    const body: SettingsChanges = turningOn
      ? { ...changes, auto_clear_acknowledgment: ack }
      : changes;
    save.mutate(body, {
      onSuccess: (next) => {
        if (turningOn && next.auto_clear_enabled !== true) {
          setEchoError(
            "The server did not confirm that auto-clear is on. Retry or cancel.",
          );
          return;
        }
        setConfirming(false);
        setDraft(null);
        setAck("");
        setStatus(
          next.pending_screening_task_id
            ? "Auto-clear settings saved. A screening task is pending; queued items change only when a future run covers them."
            : "Auto-clear settings saved.",
        );
      },
    });
  }

  return shell(
    <>
      <p className="admin-intro">
        Currently saved: auto-clear {saved.auto_clear_enabled ? "on" : "off"},
        threshold {saved.auto_clear_threshold}.
      </p>
      <p className="notice rv-warning" id={noticeId}>
        Only unresolved review items scoring strictly below the threshold are
        recorded without human review, and only in future screening runs. A
        score equal to the threshold stays in the queue. Likely matches are
        never auto-cleared. Saving does not change past decisions or start a
        full run; existing review items remain until screened again.
      </p>
      <form className="admin-form" onSubmit={submit} noValidate>
        <fieldset className="admin-fieldset" disabled={save.isPending}>
          <label className="checkbox-label">
            <input
              ref={checkboxRef}
              type="checkbox"
              checked={current.enabled}
              aria-describedby={noticeId}
              onChange={(event) => {
                const on = event.target.checked;
                // Each off-to-on change opens a fresh dialog with an empty phrase.
                setAck("");
                edit({ enabled: on });
                setConfirming(on && !saved.auto_clear_enabled);
              }}
            />
            Enable auto-clear
          </label>
          <div className="admin-grid admin-grid-tight">
            <Field
              label="Auto-clear threshold"
              help={`Whole number from ${min} to ${max}. Saved review: ${saved.review_threshold}; likely: ${saved.likely_threshold}.`}
              error={
                thresholdValid
                  ? undefined
                  : `Enter a whole number from ${min} to ${max}.`
              }
            >
              {(props) => (
                <input
                  {...props}
                  type="number"
                  inputMode="numeric"
                  min={min}
                  max={max}
                  step={1}
                  value={current.threshold}
                  onChange={(event) => edit({ threshold: event.target.value })}
                />
              )}
            </Field>
            <Field label="Adjust auto-clear threshold">
              {(props) => (
                <input
                  {...props}
                  type="range"
                  min={min}
                  max={max}
                  step={1}
                  value={sliderValue}
                  onChange={(event) => edit({ threshold: event.target.value })}
                />
              )}
            </Field>
          </div>
        </fieldset>
        {!turningOn && saveError}
        {!turningOn && (
          <div className="form-actions">
            <button className="button primary" type="submit" disabled={!canApply}>
              {save.isPending ? "Applying…" : "Apply auto-clear settings"}
            </button>
          </div>
        )}
        <p className="admin-status" role="status">
          {status}
        </p>
      </form>
      <dialog
        ref={dialogRef}
        className="panel settings-panel auto-clear-dialog"
        aria-labelledby={dialogHeadingId}
        aria-describedby={dialogNoticeId}
        onCancel={(event) => {
          event.preventDefault();
          cancel();
        }}
      >
        <form className="admin-form" onSubmit={submit} noValidate>
          <h2 id={dialogHeadingId}>Turn on auto-clear?</h2>
          <p className="notice rv-warning" id={dialogNoticeId}>
            {loaded.auto_clear_notice ?? AUTO_CLEAR_NOTICE}
          </p>
          <p className="admin-intro">
            {thresholdValid
              ? `Chosen threshold: ${parsed}. Review items scoring below ${parsed} will not be queued; a score of ${parsed} or higher stays in the queue.`
              : `The threshold is not valid. Cancel and enter a whole number from ${min} to ${max}.`}{" "}
            The threshold must stay between the review ({saved.review_threshold}) and likely ({saved.likely_threshold})
            thresholds, and likely matches are never auto-cleared.
          </p>
          <fieldset className="admin-fieldset" disabled={save.isPending}>
            <Field
              label={`Type "${AUTO_CLEAR_PHRASE}" to turn on auto-clear`}
              help="Required each time auto-clear is turned on. The phrase is sent once with this change and is not saved as a setting."
              error={
                ack && !acknowledged
                  ? `Type exactly: ${AUTO_CLEAR_PHRASE}`
                  : undefined
              }
            >
              {(props) => (
                <input
                  {...props}
                  ref={phraseRef}
                  type="text"
                  autoComplete="off"
                  spellCheck={false}
                  value={ack}
                  onChange={(event) => {
                    setAck(event.target.value);
                    setEchoError("");
                    if (save.isError) save.reset();
                  }}
                />
              )}
            </Field>
          </fieldset>
          {turningOn && saveError}
          {echoError && (
            <p className="admin-field-error admin-group-error" role="alert">
              {echoError}
            </p>
          )}
          <div className="form-actions">
            <button
              type="button"
              className="button secondary"
              onClick={cancel}
              disabled={save.isPending}
            >
              Cancel
            </button>
            <button className="button primary" type="submit" disabled={!canApply}>
              {save.isPending ? "Applying…" : "Apply auto-clear settings"}
            </button>
          </div>
        </form>
      </dialog>
    </>,
  );
}
