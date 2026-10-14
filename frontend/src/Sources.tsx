import {
  useEffect,
  useId,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { ApiError, type Session } from "./api";
import { useSearchParams } from "react-router-dom";
import { LocalTime } from "./CSLPanel";
import { AddQboSource, QboSourceControls } from "./QboSourceControls";
import { formatCount, isAdminSession } from "./csl";
import {
  DEMO_CSV_URL,
  SINGLE_FIELDS,
  initialMapping,
  toColumnMapping,
  useCreateSource,
  usePreviewSource,
  useSources,
  useUploadSource,
  validateUploadFile,
  type MappingDraft,
  type Source,
  type SourceHealth,
  type SourcePreview,
  type UploadResult,
} from "./sourceApi";

const healthLabels: Record<SourceHealth, string> = {
  not_synced: "Not yet imported",
  healthy: "Healthy",
  stale: "Stale",
  error: "Needs attention",
};
const healthTones: Record<SourceHealth, string> = {
  not_synced: "",
  healthy: "success",
  stale: "source-stale",
  error: "source-error",
};

function plural(count: number, one: string, many: string) {
  return `${formatCount(count)} ${count === 1 ? one : many}`;
}

function errorText(error: unknown) {
  return error instanceof Error
    ? error.message
    : "Something went wrong. Please try again.";
}

function HealthBadge({ health, kind }: { health: SourceHealth; kind: string }) {
  return (
    <span
      className={`badge ${healthTones[health] ?? ""}`}
      title={
        health === "stale"
          ? "The last import was more than 24 hours ago."
          : undefined
      }
    >
      {health === "not_synced" && kind !== "csv" ? "Not yet synced" : healthLabels[health] ?? health}
    </span>
  );
}

function SourceCard({
  source,
  session,
  onUpload,
}: {
  source: Source;
  session?: Session;
  onUpload?: () => void;
}) {
  const titleId = useId();
  return (
    <article className="panel source-card" aria-labelledby={titleId}>
      <div className="source-card-head">
        <div>
          <span className="source-kind">
            {source.kind === "csv" ? "CSV / XLSX upload" : source.kind === "qbo" ? "QuickBooks Online" : source.kind}
          </span>
          <h3 id={titleId}>{source.label}</h3>
        </div>
        <HealthBadge health={source.health} kind={source.kind} />
      </div>
      <dl className="source-facts">
        <div>
          <dt>Records</dt>
          <dd>{formatCount(source.record_count)}</dd>
        </div>
        <div>
          <dt>{source.kind === "csv" ? "Last import" : "Last sync"}</dt>
          <dd>
            {source.last_synced_at ? (
              <LocalTime value={source.last_synced_at} />
            ) : (
              "Never"
            )}
          </dd>
        </div>
        <div>
          <dt>Next sync</dt>
          <dd>
            {source.next_sync_at ? (
              <LocalTime value={source.next_sync_at} />
            ) : source.kind === "csv" ? (
              "When a file is uploaded"
            ) : (
              "Not scheduled"
            )}
          </dd>
        </div>
        {source.kind === "qbo" && source.environment && (
          <div><dt>Environment</dt><dd className="capitalize">{source.environment}</dd></div>
        )}
        {source.kind === "qbo" && source.connection_status && (
          <div>
            <dt>Company connection</dt>
            <dd>{({ configured: "Not connected", connected: "Connected", reconnect_required: "Reconnect required", disconnecting: "Disconnecting", disconnected: "Disconnected" })[source.connection_status]}</dd>
          </div>
        )}
      </dl>
      {source.last_error && (
        <div className="notice danger">
          <p>
            <strong>Last error:</strong> {source.last_error}
          </p>
        </div>
      )}
      {source.kind === "qbo" && session && <QboSourceControls session={session} source={source} />}
      {onUpload && (
        <button type="button" className="button secondary" onClick={onUpload}>
          Upload CSV or XLSX
        </button>
      )}
    </article>
  );
}

function SourceList({
  sources,
  admin,
  session,
  onUpload,
}: {
  sources: ReturnType<typeof useSources>;
  session: Session;
  admin: boolean;
  onUpload?: (source: Source) => void;
}) {
  if (sources.isPending)
    return (
      <p className="source-muted" role="status">
        Loading sources…
      </p>
    );
  const retry = (
    <button
      type="button"
      className="button secondary"
      onClick={() => void sources.refetch()}
      disabled={sources.isFetching}
    >
      {sources.isFetching ? "Trying again…" : "Try again"}
    </button>
  );
  const failure = sources.isError && (
    <div className="notice danger" role="alert">
      <p>
        <strong>We couldn’t load your sources.</strong>{" "}
        {errorText(sources.error)}
      </p>
      {sources.data && (
        <p>The cards below are from the last successful check.</p>
      )}
    </div>
  );
  if (!sources.data)
    return (
      <>
        {failure}
        {retry}
      </>
    );
  return (
    <>
      {failure}
      {sources.isError && retry}
      {sources.data.length === 0 ? (
        <div className="panel source-empty">
          <p>
            <strong>No sources yet.</strong>{" "}
            {admin
              ? "Add a CSV or QuickBooks source below to bring in your customer and vendor records."
              : "Your administrator hasn’t added any customer or vendor sources."}
          </p>
        </div>
      ) : (
        <ul className="source-grid">
          {sources.data.map((source) => (
            <li key={source.id}>
              <SourceCard
                source={source}
                session={session}
                onUpload={
                  onUpload && source.kind === "csv"
                    ? () => onUpload(source)
                    : undefined
                }
              />
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

function AddSourceForm({ onCreated }: { onCreated: (source: Source) => void }) {
  const titleId = useId();
  const inputId = useId();
  const [label, setLabel] = useState("");
  const create = useCreateSource();
  const trimmed = label.trim();
  function submit(event: FormEvent) {
    event.preventDefault();
    if (!trimmed || create.isPending) return;
    create.mutate(trimmed, {
      onSuccess: (source) => {
        setLabel("");
        onCreated(source);
      },
    });
  }
  return (
    <section className="panel source-add" aria-labelledby={titleId}>
      <h2 id={titleId}>Add a CSV source</h2>
      <p className="source-muted">
        Name the system or spreadsheet these records come from. You’ll choose a
        CSV or XLSX file next.
      </p>
      <form onSubmit={submit}>
        <div className="source-add-row">
          <div className="source-field">
            <label htmlFor={inputId}>Source name</label>
            <input
              id={inputId}
              value={label}
              onChange={(event) => setLabel(event.target.value)}
              required
              maxLength={200}
              autoComplete="off"
              aria-describedby={`${inputId}-help`}
            />
          </div>
          <button
            type="submit"
            className="button primary"
            disabled={create.isPending || !trimmed}
          >
            {create.isPending ? "Adding…" : "Add CSV source"}
          </button>
        </div>
        <span id={`${inputId}-help`} className="field-help">
          For example, “Customer ledger” or “Vendor directory”.
        </span>
      </form>
      {create.isError && (
        <div className="notice danger" role="alert">
          <p>
            <strong>The source was not added.</strong>{" "}
            {errorText(create.error)}
          </p>
        </div>
      )}
    </section>
  );
}

function SampleDownload() {
  return (
    <div className="quiet-note">
      <span aria-hidden="true">i</span>
      <p>
        Want to try the workflow first?{" "}
        <a className="text-link" href={DEMO_CSV_URL} download>
          Download synthetic sample CSV
        </a>
        . It contains invented parties for testing, not customer data.
      </p>
    </div>
  );
}

function Step({
  number,
  title,
  disabled,
  children,
}: {
  number: number;
  title: string;
  disabled?: boolean;
  children: ReactNode;
}) {
  return (
    <fieldset className="source-step" disabled={disabled}>
      <legend>
        <span className="source-step-number" aria-hidden="true">
          {number}
        </span>
        {title}
      </legend>
      {children}
    </fieldset>
  );
}

function DataTable({
  caption,
  children,
}: {
  caption: string;
  children: ReactNode;
}) {
  const id = useId();
  // Focusable so keyboard users can scroll wide tables on narrow screens.
  return (
    <div
      className="source-table-wrap"
      role="region"
      aria-labelledby={id}
      tabIndex={0}
    >
      <table className="source-table">
        <caption id={id}>{caption}</caption>
        {children}
      </table>
    </div>
  );
}

function PreviewTable({
  preview,
  fileName,
}: {
  preview: SourcePreview;
  fileName: string;
}) {
  const shown = formatCount(preview.rows.length);
  const total = formatCount(preview.total_rows);
  return (
    <>
      <p className="source-muted">
        Showing the first {shown} of {total} data rows. The whole file is
        checked when you import. Row numbers count the header as row 1.
      </p>
      {preview.total_rows === 0 && (
        <p className="source-muted">
          This snapshot contains no party rows. Importing it marks all existing
          parties from this source inactive; their history and audit records are kept.
        </p>
      )}
      <DataTable
        caption={`Preview of ${fileName}: first ${shown} of ${total} rows`}
      >
        <thead>
          <tr>
            <th scope="col">Row</th>
            {preview.headers.map((header) => (
              <th scope="col" key={header}>
                <span className="source-cell">{header}</span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {preview.rows.map((row, index) => (
            <tr key={index}>
              <th scope="row">{index + 2}</th>
              {preview.headers.map((header) => (
                <td key={header}>
                  <span className="source-cell">
                    {String(row[header] ?? "")}
                  </span>
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </DataTable>
    </>
  );
}

function ColumnSelect({
  label,
  help,
  required,
  headers,
  value,
  onChange,
}: {
  label: string;
  help?: string;
  required: boolean;
  headers: string[];
  value: string;
  onChange: (value: string) => void;
}) {
  const id = useId();
  return (
    <div className="source-field">
      <label htmlFor={id}>{label} column</label>
      <select
        id={id}
        value={value}
        required={required}
        aria-describedby={help ? `${id}-help` : undefined}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">{required ? "Choose a column" : "Not mapped"}</option>
        {headers.map((header) => (
          <option key={header} value={header}>
            {header}
          </option>
        ))}
      </select>
      {help && (
        <span id={`${id}-help`} className="field-help">
          {help}
        </span>
      )}
    </div>
  );
}

function ColumnChecklist({
  legend,
  help,
  headers,
  selected,
  onChange,
}: {
  legend: string;
  help: string;
  headers: string[];
  selected: string[];
  onChange: (columns: string[]) => void;
}) {
  const helpId = useId();
  return (
    <fieldset className="source-checklist" aria-describedby={helpId}>
      <legend>{legend}</legend>
      <p id={helpId} className="field-help">
        {help}
      </p>
      <div className="source-checklist-options">
        {headers.map((header) => (
          <label key={header} className="source-check">
            <input
              type="checkbox"
              checked={selected.includes(header)}
              onChange={(event) =>
                // Appending keeps the order columns were chosen in.
                onChange(
                  event.target.checked
                    ? [...selected, header]
                    : selected.filter((column) => column !== header),
                )
              }
            />
            <span>{header}</span>
          </label>
        ))}
      </div>
      {selected.length > 1 && (
        <p className="field-help">
          Combined in this order: {selected.join(", ")}
        </p>
      )}
    </fieldset>
  );
}

function RejectedRows({ result }: { result: UploadResult }) {
  const shown = result.rejected.length;
  const omitted = result.omitted_rejection_details;
  if (shown === 0 && omitted === 0) return null;
  return (
    <div className="source-rejections">
      {shown > 0 && (
        <DataTable
          caption={`Rejected rows: ${formatCount(shown)} listed of ${formatCount(result.rejected_count)}`}
        >
          <thead>
            <tr>
              <th scope="col">Row</th>
              <th scope="col">Reason</th>
            </tr>
          </thead>
          <tbody>
            {result.rejected.map((row, index) => (
              <tr key={index}>
                <th scope="row">{row.row_number}</th>
                <td>
                  <span className="source-cell wide">{row.reason}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      )}
      {omitted > 0 && (
        <p className="source-muted">
          {plural(omitted, "more rejected row is", "more rejected rows are")}{" "}
          not listed. The server returns details for up to 500 rows.
        </p>
      )}
    </div>
  );
}

function AppliedOutcome({ result }: { result: UploadResult }) {
  const tasks = result.task_ids.length;
  const runs = result.screening_run_ids.length;
  return (
    <div className="notice source-outcome source-applied">
      <p>
        <strong>The import was applied.</strong> This source now has{" "}
        {plural(result.source.record_count, "active record", "active records")}
        .
      </p>
      <ul className="source-counts">
        <li>
          <strong>{formatCount(result.added)}</strong> added
        </li>
        <li>
          <strong>{formatCount(result.changed)}</strong> changed
        </li>
        <li>
          <strong>{formatCount(result.unchanged)}</strong> unchanged
        </li>
        <li>
          <strong>{formatCount(result.deleted)}</strong> deleted (marked
          inactive)
        </li>
      </ul>
      <p>
        {runs > 0
          ? `Screening recorded ${plural(runs, "run", "runs")} for new and changed parties.`
          : tasks > 0
            ? "New and changed parties are queued for screening."
            : result.added + result.changed > 0
              ? "Download the Consolidated Screening List to screen these changes."
              : "No parties were new or changed, so no screening was queued."}
      </p>
    </div>
  );
}

function RejectedOutcome({ result }: { result: UploadResult }) {
  return (
    <>
      <div className="notice danger source-outcome" role="alert">
        <p>
          <strong>Nothing was imported.</strong>{" "}
          {plural(result.rejected_count, "row was", "rows were")} rejected, so
          no parties were added, changed, or removed. Earlier records for this
          source are unchanged.
        </p>
        <p>Correct the file or the column mapping, then import again.</p>
      </div>
      <RejectedRows result={result} />
    </>
  );
}

function UploadError({ error }: { error: unknown }) {
  // Only a 4xx response proves the server refused before changing anything.
  const refused =
    error instanceof ApiError && error.status >= 400 && error.status < 500;
  return (
    <div className="notice danger source-outcome" role="alert">
      <p>
        <strong>{refused ? "The file was not imported." : "The import outcome could not be confirmed."}</strong> {errorText(error)}
      </p>
      <p>
        {refused
          ? "Earlier records for this source are unchanged."
          : "Check this source’s status before trying again."}
      </p>
    </div>
  );
}

function UploadWizard({
  source,
  onClose,
}: {
  source: Source;
  onClose: () => void;
}) {
  const titleId = useId();
  const fileId = useId();
  const confirmId = useId();
  const headingRef = useRef<HTMLHeadingElement>(null);
  const currentFile = useRef<File | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<SourcePreview | null>(null);
  const [mapping, setMapping] = useState<MappingDraft | null>(null);
  const [aliasesOpen, setAliasesOpen] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const previewing = usePreviewSource();
  const upload = useUploadSource();

  const fileError = file ? validateUploadFile(file) : null;
  const result = upload.data;
  const applied = result?.applied === true;
  const locked = upload.isPending || applied;
  const nameMapped = Boolean(
    preview && mapping?.name && preview.headers.includes(mapping.name),
  );
  const canImport = Boolean(
    file &&
      !fileError &&
      preview &&
      nameMapped &&
      confirmed &&
      !upload.isPending &&
      !applied,
  );

  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  function resetAttempt() {
    setPreview(null);
    setMapping(null);
    setConfirmed(false);
    upload.reset();
  }

  function chooseFile(next: File | null) {
    currentFile.current = next;
    setFile(next);
    resetAttempt();
    previewing.reset();
  }

  function runPreview() {
    if (!file || fileError) return;
    const chosen = file;
    resetAttempt();
    previewing.mutate(
      { file: chosen, sourceId: source.id },
      {
        onSuccess: (data) => {
          // Ignore a preview that finishes after a different file was chosen.
          if (currentFile.current !== chosen) return;
          const draft = initialMapping(
            data.headers,
            data.column_mapping ?? source.column_mapping,
          );
          setPreview(data);
          setMapping(draft);
          setAliasesOpen(draft.alt_names.length > 0);
        },
      },
    );
  }

  function update<K extends keyof MappingDraft>(
    field: K,
    value: MappingDraft[K],
  ) {
    setMapping((current) => (current ? { ...current, [field]: value } : current));
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!canImport || !file || !mapping) return;
    upload.mutate({
      sourceId: source.id,
      file,
      mapping: toColumnMapping(mapping),
    });
  }

  const hint =
    preview && !upload.isPending && !applied
      ? !nameMapped
        ? "Choose a name column to continue."
        : !confirmed
          ? "Confirm the file is the complete source snapshot to continue."
          : null
      : null;

  return (
    <section className="panel source-wizard" aria-labelledby={titleId}>
      <div className="source-wizard-head">
        <span className="eyebrow">CSV / XLSX upload</span>
        <h2 id={titleId} ref={headingRef} tabIndex={-1}>
          Upload a file to {source.label}
        </h2>
        <p className="source-muted">
          Each import is a complete snapshot of this source. Nothing changes
          until you review the preview, map the columns, and import.
        </p>
      </div>
      <SourceCard source={source} />
      <form onSubmit={submit}>
        <Step number={1} title="Select a file" disabled={upload.isPending}>
          <div className="source-file-row">
            <div className="source-field">
              <label htmlFor={fileId}>File to import</label>
              <input
                id={fileId}
                type="file"
                accept=".csv,.xlsx"
                aria-invalid={fileError ? true : undefined}
                aria-describedby={`${fileId}-help${fileError ? ` ${fileId}-error` : ""}`}
                onChange={(event) =>
                  chooseFile(event.target.files?.[0] ?? null)
                }
              />
              <span id={`${fileId}-help`} className="field-help">
                CSV / XLSX, up to 10 MiB. Only the first worksheet of an XLSX
                file is read.
              </span>
            </div>
            <button
              type="button"
              className="button secondary"
              onClick={runPreview}
              disabled={!file || Boolean(fileError) || previewing.isPending}
            >
              {previewing.isPending ? "Reading file…" : "Preview file"}
            </button>
          </div>
          {fileError && (
            <p
              id={`${fileId}-error`}
              className="source-field-error"
              role="alert"
            >
              {fileError}
            </p>
          )}
          {previewing.isError && (
            <div className="notice danger source-outcome" role="alert">
              <p>
                <strong>The file could not be previewed.</strong>{" "}
                {errorText(previewing.error)}
              </p>
            </div>
          )}
        </Step>
        {preview && mapping && file && (
          <>
            <Step number={2} title="Check the preview">
              <PreviewTable preview={preview} fileName={file.name} />
            </Step>
            <Step number={3} title="Map columns" disabled={locked}>
              <p className="source-muted">
                Suggestions come from your column headings and this source’s
                saved mapping. The mapping is saved only when an import is
                applied.
              </p>
              <div className="source-mapping-grid">
                {SINGLE_FIELDS.map(({ field, label, help }) => (
                  <ColumnSelect
                    key={field}
                    label={label}
                    help={help}
                    required={field === "name"}
                    headers={preview.headers}
                    value={mapping[field]}
                    onChange={(value) => update(field, value)}
                  />
                ))}
              </div>
              <ColumnChecklist
                legend="Address line columns"
                help="Select each column that holds part of the street address. Lines are combined in the order you select them."
                headers={preview.headers}
                selected={mapping.address_lines}
                onChange={(columns) => update("address_lines", columns)}
              />
              <details className="source-advanced" open={aliasesOpen}>
                <summary>
                  Advanced: alternate names ({mapping.alt_names.length}{" "}
                  selected)
                </summary>
                <ColumnChecklist
                  legend="Alternate name columns"
                  help="Optional. Columns with other names for the same party, such as a trade name."
                  headers={preview.headers}
                  selected={mapping.alt_names}
                  onChange={(columns) => update("alt_names", columns)}
                />
              </details>
              <div className="notice source-warning">
                <p>{preview.stable_id_warning}</p>
                {mapping.external_id && (
                  <p>
                    “{mapping.external_id}” is mapped as the ID, so each row
                    needs its own value in that column.
                  </p>
                )}
              </div>
            </Step>
            <Step number={4} title="Confirm and import" disabled={locked}>
              <label className="checkbox-label source-confirm">
                <input
                  type="checkbox"
                  checked={confirmed}
                  onChange={(event) => setConfirmed(event.target.checked)}
                  aria-describedby={confirmId}
                />
                <span>I confirm this file is the complete source snapshot</span>
              </label>
              <p id={confirmId} className="field-help source-confirm-help">
                Parties in this source that are missing from the file will be
                marked inactive. Their history and audit records are kept.
              </p>
            </Step>
          </>
        )}
        <div className="source-live" role="status">
          {upload.isPending && (
            <p className="source-progress">Importing and checking every row…</p>
          )}
          {result?.applied && <AppliedOutcome result={result} />}
        </div>
        {result?.applied && result.screening_pending && (
          <div className="notice danger source-outcome" role="alert">
            <p><strong>The records were imported, but screening is pending.</strong></p>
            <p>Some screening tasks need to be retried. Completed runs and saved party records are retained.</p>
            {(result.screening_errors?.length ?? 0) > 0 && (
              <ul>
                {result.screening_errors?.map((failure) => (
                  <li key={failure.task_id}>{failure.error} (attempt {failure.attempts})</li>
                ))}
              </ul>
            )}
          </div>
        )}
        {result?.applied && <RejectedRows result={result} />}
        {result && !result.applied && <RejectedOutcome result={result} />}
        {upload.isError && <UploadError error={upload.error} />}
        <div className="source-actions">
          {!applied && (
            <button
              type="submit"
              className="button primary"
              disabled={!canImport}
            >
              {upload.isPending ? "Importing…" : "Import file"}
            </button>
          )}
          <button
            type="button"
            className="button secondary"
            onClick={onClose}
            disabled={upload.isPending}
          >
            {applied ? "Back to sources" : "Cancel"}
          </button>
          {hint && <p className="source-muted">{hint}</p>}
          {applied && (
            <p className="source-muted">
              To import a newer snapshot, choose another file above.
            </p>
          )}
        </div>
      </form>
    </section>
  );
}

const qboCallbackErrors: Record<string, string> = {
  invalid_oauth_state: "The authorization link is no longer valid. Start a new connection request.",
  oauth_configuration_changed: "The app or source configuration changed during authorization. Start again with the current settings.",
  authorization_declined: "QuickBooks authorization was declined. You can connect again when ready.",
  invalid_oauth_response: "QuickBooks authorization could not be verified. Start a new connection request.",
  company_binding_mismatch: "This source belongs to a different QuickBooks company. Reconnect its original company, or add a new source for the new company.",
  company_already_connected: "This QuickBooks company already has a source in this environment. Use that source instead.",
  admin_session_required: "An active Admin session is required. Sign in and start authorization again.",
  authorization_cancelled: "QuickBooks authorization was cancelled. Start a new connection request when ready.",
  source_busy: "This source is busy. Wait for its operation to finish, then start authorization again.",
  revocation_cleanup_pending: "An earlier QuickBooks authorization is still being cleaned up. Wait for cleanup to finish, or retry failed revocation on the source card before reconnecting.",
  oauth_failed: "QuickBooks authorization could not be completed. Check the connection state below before trying again.",
};

function QboCallbackNotice() {
  const [params] = useSearchParams();
  const error = params.get("connection_error");
  if (error) return <div className="notice danger" role="alert">{Object.hasOwn(qboCallbackErrors, error) ? qboCallbackErrors[error] : qboCallbackErrors.oauth_failed}</div>;
  if (params.get("connection") === "connected")
    return <p className="notice" role="status">QuickBooks authorization returned. Check the current company connection state below.</p>;
  return null;
}

export function Sources({ session }: { session: Session }) {
  const admin = isAdminSession(session);
  const listId = useId();
  const sources = useSources({ enabled: true });
  const [uploading, setUploading] = useState<Source | null>(null);
  // Prefer the cached copy so the scoped card reflects the latest response.
  const active = uploading
    ? (sources.data?.find((source) => source.id === uploading.id) ??
      uploading)
    : null;
  return (
    <>
      <header className="page-header">
        <div>
          <div className="eyebrow">Your data</div>
          <h1>Sources</h1>
          <p className="lede">
            {admin
              ? "Bring in your customer and vendor records. Each CSV upload replaces that source’s records with a complete snapshot."
              : "The customer and vendor sources your administrator maintains, with their latest import status."}
          </p>
        </div>
        {!admin && <span className="badge">Read-only</span>}
      </header>
      {admin && <QboCallbackNotice />}
      {active ? (
        <UploadWizard
          key={active.id}
          source={active}
          onClose={() => setUploading(null)}
        />
      ) : (
        <>
          <section className="source-section" aria-labelledby={listId}>
            <h2 id={listId} className="source-section-title">
              Your sources
            </h2>
            <SourceList
              sources={sources}
              admin={admin}
              session={session}
              onUpload={admin ? setUploading : undefined}
            />
          </section>
          {admin && <AddSourceForm onCreated={setUploading} />}
          {admin && <AddQboSource session={session} />}
          {admin && <SampleDownload />}
        </>
      )}
    </>
  );
}
