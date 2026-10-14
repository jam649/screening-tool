import { useId, useState, type FormEvent, type ReactNode } from "react";
import type { Session } from "./api";
import {
  MIN_RETENTION_YEARS,
  NAME_WEIGHTS,
  ADJUSTMENT_WEIGHTS,
  WEIGHT_KEYS,
  canAdminister,
  changedSettings,
  messageOf,
  parseList,
  parseNumber,
  useSaveSettings,
  useSettings,
  validTimezone,
  type SettingsChanges,
  type WeightKey,
  type WorkspaceSettings,
} from "./adminApi";
import "./admin.css";

type Errors = Record<string, string>;
type ControlProps = {
  id: string;
  "aria-invalid"?: true;
  "aria-describedby"?: string;
};

export function Field({
  label,
  help,
  error,
  children,
}: {
  label: string;
  help?: string;
  error?: string;
  children: (props: ControlProps) => ReactNode;
}) {
  const id = useId();
  const describedBy =
    [help && `${id}-help`, error && `${id}-error`].filter(Boolean).join(" ") ||
    undefined;
  return (
    <div className="admin-field">
      <label htmlFor={id}>{label}</label>
      {children({
        id,
        "aria-invalid": error ? true : undefined,
        "aria-describedby": describedBy,
      })}
      {help && (
        <span id={`${id}-help`} className="field-help">
          {help}
        </span>
      )}
      {error && (
        <span id={`${id}-error`} className="admin-field-error">
          {error}
        </span>
      )}
    </div>
  );
}

function Feedback({ error, message }: { error: unknown; message: string }) {
  return (
    <>
      {error ? (
        <div className="notice danger" role="alert">
          {messageOf(error, "The settings could not be saved.")} Your edits
          have been kept.
        </div>
      ) : null}
      <p className="admin-status" role="status">
        {message}
      </p>
    </>
  );
}

const TIMEZONES =
  (
    Intl as unknown as { supportedValuesOf?: (key: "timeZone") => string[] }
  ).supportedValuesOf?.("timeZone") ?? [];

function GeneralForm({ settings }: { settings: WorkspaceSettings }) {
  const titleId = useId();
  const zonesId = useId();
  const save = useSaveSettings();
  const [name, setName] = useState(settings.display_name);
  const [timezone, setTimezone] = useState(settings.timezone);
  const [retentionOn, setRetentionOn] = useState(
    settings.retention_years !== null,
  );
  const [years, setYears] = useState(
    String(settings.retention_years ?? MIN_RETENTION_YEARS),
  );
  const [errors, setErrors] = useState<Errors>({});
  const [message, setMessage] = useState("");

  function submit(event: FormEvent) {
    event.preventDefault();
    setMessage("");
    const next: Errors = {};
    const displayName = name.trim();
    const zone = timezone.trim();
    if (!displayName || displayName.length > 200)
      next.display_name = "Enter a workspace name of 1 to 200 characters.";
    if (!validTimezone(zone))
      next.timezone =
        "Enter an IANA timezone, such as UTC or America/New_York.";
    const retention = retentionOn ? parseNumber(years) : null;
    if (
      retentionOn &&
      (retention === null ||
        !Number.isInteger(retention) ||
        retention < MIN_RETENTION_YEARS)
    )
      next.retention_years = `Enter a whole number of years, ${MIN_RETENTION_YEARS} or more.`;
    setErrors(next);
    if (Object.keys(next).length) {
      setMessage("Check the highlighted fields.");
      return;
    }
    const changes = changedSettings(settings, {
      display_name: displayName,
      timezone: zone,
      retention_years: retention,
    });
    if (!Object.keys(changes).length) {
      save.reset();
      setMessage("No changes to save.");
      return;
    }
    save.mutate(changes, {
      onSuccess: (saved) => {
        setName(saved.display_name);
        setTimezone(saved.timezone);
        setRetentionOn(saved.retention_years !== null);
        setYears(String(saved.retention_years ?? MIN_RETENTION_YEARS));
        setMessage("General settings saved.");
      },
    });
  }

  return (
    <section className="panel settings-panel" aria-labelledby={titleId}>
      <h2 id={titleId}>General</h2>
      <p className="admin-intro">
        How this workspace is named and dated, and its record retention policy.
      </p>
      <form className="admin-form" noValidate onSubmit={submit}>
        <fieldset className="admin-fieldset" disabled={save.isPending}>
          <div className="admin-grid">
            <Field
              label="Workspace name"
              help="Shown in the sidebar, on the sign-in page and in the browser title."
              error={errors.display_name}
            >
              {(props) => (
                <input
                  {...props}
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  maxLength={200}
                  autoComplete="off"
                />
              )}
            </Field>
            <Field
              label="Timezone"
              help="An IANA timezone name."
              error={errors.timezone}
            >
              {(props) => (
                <input
                  {...props}
                  value={timezone}
                  onChange={(event) => setTimezone(event.target.value)}
                  list={zonesId}
                  autoComplete="off"
                  spellCheck={false}
                />
              )}
            </Field>
          </div>
          <datalist id={zonesId}>
            {TIMEZONES.map((zone) => (
              <option key={zone} value={zone} />
            ))}
          </datalist>
          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={retentionOn}
              onChange={(event) => setRetentionOn(event.target.checked)}
            />
            <span>Enable retention</span>
          </label>
          <div className="admin-grid admin-grid-tight">
            <Field
              label="Retention period (years)"
              help={`Off by default, so no retention period applies. When enabled, records are kept at least ${MIN_RETENTION_YEARS} years. Saving stores the policy; automatic retention processing is not active in this version.`}
              error={errors.retention_years}
            >
              {(props) => (
                <input
                  {...props}
                  type="number"
                  inputMode="numeric"
                  min={MIN_RETENTION_YEARS}
                  step={1}
                  value={years}
                  onChange={(event) => setYears(event.target.value)}
                  disabled={!retentionOn}
                />
              )}
            </Field>
          </div>
          <Feedback error={save.error} message={message} />
          <div className="admin-actions">
            <button className="button primary" type="submit">
              {save.isPending ? "Saving…" : "Save general settings"}
            </button>
          </div>
        </fieldset>
      </form>
    </section>
  );
}

const THRESHOLDS = [
  ["candidate_min", "Candidate minimum", "Lowest score kept as a candidate."],
  ["review_threshold", "Review threshold", "Score at which a candidate needs review."],
  [
    "likely_threshold",
    "Likely match threshold",
    "Score at which a candidate is labeled a likely match.",
  ],
] as const;
type ThresholdKey = (typeof THRESHOLDS)[number][0];

const WEIGHT_LABELS: Record<WeightKey, string> = {
  token_set: "Token set weight",
  token_sort: "Token sort weight",
  partial: "Partial match weight",
  country_match: "Country match",
  country_mismatch: "Country mismatch",
  city_match: "City match",
  address_max: "Address match (maximum)",
  entity_mismatch: "Entity type mismatch",
  short_name: "Short name",
};

type ScoringDraft = Record<ThresholdKey, string> & {
  weights: Record<WeightKey, string>;
  legal_suffixes: string;
  candidate_stopwords: string;
  allLists: boolean;
  sourceLists: string;
};

function scoringDraft(settings: WorkspaceSettings): ScoringDraft {
  return {
    candidate_min: String(settings.candidate_min),
    review_threshold: String(settings.review_threshold),
    likely_threshold: String(settings.likely_threshold),
    weights: Object.fromEntries(
      WEIGHT_KEYS.map((key) => [key, String(settings.score_weights[key] ?? "")]),
    ) as Record<WeightKey, string>,
    legal_suffixes: settings.legal_suffixes.join("\n"),
    candidate_stopwords: settings.candidate_stopwords.join("\n"),
    allLists: settings.enabled_source_lists === null,
    sourceLists: (settings.enabled_source_lists ?? []).join("\n"),
  };
}

const SOURCE_SEPARATORS = /[\n,]/;

function validateScoring(draft: ScoringDraft): {
  errors: Errors;
  values?: SettingsChanges;
} {
  const errors: Errors = {};
  const read = (key: string, text: string, label: string) => {
    const value = parseNumber(text);
    if (value === null) errors[key] = `Enter a number for ${label.toLowerCase()}.`;
    return value;
  };
  const [candidate, review, likely] = THRESHOLDS.map(([key, label]) => {
    const value = read(key, draft[key], label);
    if (value !== null && (value < 0 || value > 100))
      errors[key] = "Use a value from 0 to 100.";
    return value;
  });
  if (
    candidate !== null &&
    review !== null &&
    !errors.candidate_min &&
    !errors.review_threshold &&
    candidate > review
  )
    errors.candidate_min =
      "The candidate minimum can’t be higher than the review threshold.";
  if (
    review !== null &&
    likely !== null &&
    !errors.review_threshold &&
    !errors.likely_threshold &&
    review > likely
  )
    errors.review_threshold =
      "The review threshold can’t be higher than the likely match threshold.";
  const weights = {} as Record<WeightKey, number>;
  for (const key of WEIGHT_KEYS) {
    const value = read(key, draft.weights[key], WEIGHT_LABELS[key]);
    if (value === null) continue;
    weights[key] = value;
    if ((NAME_WEIGHTS as readonly string[]).includes(key) && value < 0)
      errors[key] = "Name weights can’t be negative.";
  }
  if (NAME_WEIGHTS.every((key) => !errors[key])) {
    const sum = NAME_WEIGHTS.reduce((total, key) => total + weights[key], 0);
    if (Math.abs(sum - 1) > 1e-9)
      errors.name_weights = `The three name weights must add up to 1 (currently ${Number(sum.toFixed(6))}).`;
  }
  if (Object.keys(errors).length) return { errors };
  return {
    errors,
    values: {
      candidate_min: candidate!,
      review_threshold: review!,
      likely_threshold: likely!,
      score_weights: weights,
      legal_suffixes: parseList(draft.legal_suffixes),
      candidate_stopwords: parseList(draft.candidate_stopwords),
      enabled_source_lists: draft.allLists
        ? null
        : parseList(draft.sourceLists, SOURCE_SEPARATORS),
    },
  };
}

function ScoringForm({ settings }: { settings: WorkspaceSettings }) {
  const titleId = useId();
  const nameErrorId = useId();
  const save = useSaveSettings();
  const [draft, setDraft] = useState(() => scoringDraft(settings));
  const [errors, setErrors] = useState<Errors>({});
  const [message, setMessage] = useState("");
  const update = (patch: Partial<ScoringDraft>) =>
    setDraft((current) => ({ ...current, ...patch }));
  const setWeight = (key: WeightKey, value: string) =>
    setDraft((current) => ({
      ...current,
      weights: { ...current.weights, [key]: value },
    }));
  const noLists =
    !draft.allLists &&
    parseList(draft.sourceLists, SOURCE_SEPARATORS).length === 0;

  function submit(event: FormEvent) {
    event.preventDefault();
    setMessage("");
    const result = validateScoring(draft);
    setErrors(result.errors);
    if (!result.values) {
      setMessage("Check the highlighted fields.");
      return;
    }
    const changes = changedSettings(settings, result.values);
    if (!Object.keys(changes).length) {
      save.reset();
      setMessage("No changes to save.");
      return;
    }
    save.mutate(changes, {
      onSuccess: (saved) => {
        setDraft(scoringDraft(saved));
        setMessage(
          "Screening settings saved. The server uses them for screening and the review queue from now on; this page does not start a new screening run.",
        );
      },
    });
  }

  const weightField = (key: WeightKey, help?: string) => (
    <Field key={key} label={WEIGHT_LABELS[key]} help={help} error={errors[key]}>
      {(props) => (
        <input
          {...props}
          type="number"
          inputMode="decimal"
          step="any"
          value={draft.weights[key]}
          onChange={(event) => setWeight(key, event.target.value)}
          aria-describedby={
            [props["aria-describedby"], errors.name_weights && (NAME_WEIGHTS as readonly string[]).includes(key) ? nameErrorId : ""]
              .filter(Boolean)
              .join(" ") || undefined
          }
        />
      )}
    </Field>
  );

  return (
    <section className="panel settings-panel" aria-labelledby={titleId}>
      <h2 id={titleId}>Screening</h2>
      <p className="admin-intro">
        Scores range from 0 to 100. Each threshold must be no higher than the
        next. The server validates every change before it is saved.
      </p>
      <form className="admin-form" noValidate onSubmit={submit}>
        <fieldset className="admin-fieldset" disabled={save.isPending}>
          <div className="admin-grid">
            {THRESHOLDS.map(([key, label, help]) => (
              <Field key={key} label={label} help={help} error={errors[key]}>
                {(props) => (
                  <input
                    {...props}
                    type="number"
                    inputMode="decimal"
                    min={0}
                    max={100}
                    step="any"
                    value={draft[key]}
                    onChange={(event) => update({ [key]: event.target.value })}
                  />
                )}
              </Field>
            ))}
          </div>
          <details className="admin-details">
            <summary>Advanced scoring</summary>
            <fieldset className="admin-group">
              <legend>Name similarity weights</legend>
              <span className="field-help">
                Nonnegative shares of the name score. Together they must add up
                to 1.
              </span>
              <div className="admin-grid">
                {NAME_WEIGHTS.map((key) => weightField(key))}
              </div>
              {errors.name_weights && (
                <p id={nameErrorId} className="admin-field-error admin-group-error">
                  {errors.name_weights}
                </p>
              )}
            </fieldset>
            <fieldset className="admin-group">
              <legend>Score adjustments</legend>
              <span className="field-help">
                Points added to or subtracted from a candidate’s score.
                Negative values are allowed.
              </span>
              <div className="admin-grid">
                {ADJUSTMENT_WEIGHTS.map((key) => weightField(key))}
              </div>
            </fieldset>
            <fieldset className="admin-group">
              <legend>Name normalization</legend>
              <div className="admin-grid">
                <Field
                  label="Legal suffixes"
                  help="One per line, such as inc or gmbh. Blank lines and repeats are ignored."
                >
                  {(props) => (
                    <textarea
                      {...props}
                      rows={6}
                      spellCheck={false}
                      value={draft.legal_suffixes}
                      onChange={(event) =>
                        update({ legal_suffixes: event.target.value })
                      }
                    />
                  )}
                </Field>
                <Field
                  label="Candidate stopwords"
                  help="One per line. Common words ignored when finding candidates."
                >
                  {(props) => (
                    <textarea
                      {...props}
                      rows={6}
                      spellCheck={false}
                      value={draft.candidate_stopwords}
                      onChange={(event) =>
                        update({ candidate_stopwords: event.target.value })
                      }
                    />
                  )}
                </Field>
              </div>
            </fieldset>
            <fieldset className="admin-group">
              <legend>Source lists</legend>
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={draft.allLists}
                  onChange={(event) => update({ allLists: event.target.checked })}
                />
                <span>Screen against all source lists</span>
              </label>
              {!draft.allLists && (
                <>
                  <Field
                    label="Only these source lists"
                    help="List codes separated by commas or new lines, matching the codes stored with CSL entries. Leaving this blank enables no lists."
                  >
                    {(props) => (
                      <textarea
                        {...props}
                        rows={4}
                        spellCheck={false}
                        value={draft.sourceLists}
                        onChange={(event) =>
                          update({ sourceLists: event.target.value })
                        }
                      />
                    )}
                  </Field>
                  {noLists && (
                    <p className="admin-warning">
                      No source lists are entered, so no lists would be enabled
                      for screening.
                    </p>
                  )}
                </>
              )}
            </fieldset>
          </details>
          <Feedback error={save.error} message={message} />
          <div className="admin-actions">
            <button className="button primary" type="submit">
              {save.isPending ? "Saving…" : "Save screening settings"}
            </button>
          </div>
        </fieldset>
      </form>
    </section>
  );
}

export function SettingsControls({ session }: { session: Session }) {
  const settings = useSettings(session);
  if (!canAdminister(session)) return null;
  if (settings.data)
    return (
      <>
        {settings.isError && (
          <div className="notice danger" role="alert">
            The latest settings could not be loaded. The forms show the last
            values received. {messageOf(settings.error, "")}
          </div>
        )}
        <GeneralForm settings={settings.data} />
        <ScoringForm settings={settings.data} />
      </>
    );
  return (
    <section className="panel settings-panel" aria-label="Workspace settings">
      <h2>Workspace settings</h2>
      {settings.isError ? (
        <>
          <div className="notice danger" role="alert">
            We couldn’t load the workspace settings.{" "}
            {messageOf(settings.error, "")}
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
          Loading settings…
        </p>
      )}
    </section>
  );
}
