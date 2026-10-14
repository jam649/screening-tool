import { useId, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { sessionQuery } from "./api";
import {
  CSL_VERSION_LIMIT,
  describeChanges,
  describeRefreshError,
  formatCount,
  isAdminSession,
  parseSourceStamps,
  useCSL,
  type CSLFetchStatus,
  type CSLRefreshResult,
  type CSLSourceStamps,
  type SourceStamp,
} from "./csl";

export function LocalTime({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="csl-muted">Not recorded</span>;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return <span>{value}</span>;
  const iso = date.toISOString();
  return (
    <time dateTime={iso} title={`${iso.slice(0, 19).replace("T", " ")} UTC`}>
      {date.toLocaleString(undefined, {
        dateStyle: "medium",
        timeStyle: "short",
      })}
    </time>
  );
}

export function Hash({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="csl-muted">Not recorded</span>;
  return <code className="csl-hash">{value}</code>;
}

const fetchLabels: Record<CSLFetchStatus, string> = {
  fetching: "In progress",
  success: "Succeeded",
  failed: "Failed",
};

export function FetchStatusBadge({ status }: { status: CSLFetchStatus }) {
  const tone =
    status === "success"
      ? "success"
      : status === "failed"
        ? "csl-failed"
        : "csl-running";
  return <span className={`badge ${tone}`}>{fetchLabels[status] ?? status}</span>;
}

export function RefreshOutcome({ result }: { result: CSLRefreshResult }) {
  const version = result.current_version;
  return (
    <div className="notice csl-notice csl-success">
      {result.changed && version ? (
        <p>
          <strong>A new list version was saved.</strong> It has{" "}
          {formatCount(version.entry_count)} entries ({describeChanges(version)}
          ) and was fetched <LocalTime value={version.fetched_at} />.
        </p>
      ) : (
        <p>
          <strong>The list is unchanged.</strong> The download matched the
          current version
          {version && (
            <>
              {" "}
              fetched <LocalTime value={version.fetched_at} />
            </>
          )}
          , so no new version was saved. This check was recorded with its own
          source stamps.
        </p>
      )}
      {result.pending_task_id && (
        <p>
          Screening against this version is queued on the server. Results are
          not complete until it finishes.
        </p>
      )}
      {result.closed_hits > 0 && (
        <p>
          {formatCount(result.closed_hits)} open{" "}
          {result.closed_hits === 1 ? "match was" : "matches were"} closed by
          this update.
        </p>
      )}
    </div>
  );
}

export function RefreshError({
  error,
  keepsVersion,
}: {
  error: unknown;
  keepsVersion: boolean;
}) {
  const { title, detail } = describeRefreshError(error);
  return (
    <div className="notice danger csl-notice" role="alert">
      <p>
        <strong>{title}</strong> {detail}
      </p>
      {keepsVersion && (
        <p>The previously downloaded version remains current.</p>
      )}
    </div>
  );
}

export function StatusError({
  error,
  retrying,
  onRetry,
}: {
  error: unknown;
  retrying: boolean;
  onRetry: () => void;
}) {
  return (
    <>
      <div className="notice danger csl-notice" role="alert">
        <p>
          <strong>We couldn’t load the list status.</strong>{" "}
          {error instanceof Error ? error.message : null}
        </p>
        <p>Fetching is unavailable until the current state is known.</p>
      </div>
      <button
        type="button"
        className="button secondary"
        onClick={onRetry}
        disabled={retrying}
      >
        {retrying ? "Trying again…" : "Try again"}
      </button>
    </>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  const id = useId();
  return (
    <section className="csl-section" aria-labelledby={id}>
      <h3 id={id}>{title}</h3>
      {children}
    </section>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function ScrollTable({
  caption,
  children,
}: {
  caption: string;
  children: ReactNode;
}) {
  const id = useId();
  // Focusable so keyboard users can scroll the table on narrow screens.
  return (
    <div
      className="csl-table-wrap"
      role="region"
      aria-labelledby={id}
      tabIndex={0}
    >
      <table className="csl-table">
        <caption id={id}>{caption}</caption>
        {children}
      </table>
    </div>
  );
}

function StampTable({ caption, rows }: { caption: string; rows: SourceStamp[] }) {
  return (
    <ScrollTable caption={caption}>
      <thead>
        <tr>
          <th scope="col">List</th>
          <th scope="col">Source</th>
          <th scope="col">Last imported</th>
          <th scope="col">Source last updated</th>
          <th scope="col">Import rate</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.key}>
            <th scope="row">{row.key}</th>
            <td>{row.source ?? "—"}</td>
            <td className="csl-raw">{row.lastImported ?? "—"}</td>
            <td className="csl-raw">{row.sourceLastUpdated ?? "—"}</td>
            <td>{row.importRate ?? "—"}</td>
          </tr>
        ))}
      </tbody>
    </ScrollTable>
  );
}

function StampTables({
  stamps,
  label,
  empty,
}: {
  stamps: CSLSourceStamps;
  label: string;
  empty: string;
}) {
  const parsed = parseSourceStamps(stamps);
  if (parsed.lists.length === 0 && parsed.api === null)
    return <p className="csl-muted">{empty}</p>;
  return (
    <>
      {parsed.lists.length > 0 && (
        <StampTable caption={`Source list stamps ${label}`} rows={parsed.lists} />
      )}
      {parsed.api !== null &&
        (parsed.api.length > 0 ? (
          <StampTable
            caption={`Search API source stamps ${label}`}
            rows={parsed.api}
          />
        ) : (
          <p className="csl-muted">
            Search API source stamps ({label}): none recorded.
          </p>
        ))}
    </>
  );
}

export function CSLPanel() {
  const titleId = useId();
  // Reads the cached session without refetching it.
  const session = useQuery({
    queryKey: ["session"],
    queryFn: sessionQuery,
    enabled: false,
  });
  const admin = isAdminSession(session.data);
  const { status, versions, refresh, busy } = useCSL({
    enabled: admin,
    withVersions: true,
  });
  if (!admin) return null;

  const data = status.data;
  const current = data?.current_version ?? null;
  const lastFetch = data?.last_fetch ?? null;
  const canAct = status.isSuccess;
  const warnings = Array.isArray(lastFetch?.warnings) ? lastFetch.warnings : [];

  return (
    <section
      className="panel settings-panel csl-panel"
      aria-labelledby={titleId}
    >
      <div className="csl-header">
        <div>
          <h2 id={titleId}>Consolidated Screening List</h2>
          <p className="csl-intro">
            The list this workspace screens against, downloaded from the
            International Trade Administration. A fetch can take a minute.
          </p>
        </div>
        {canAct && (
          <button
            type="button"
            className="button primary"
            onClick={() => refresh.mutate()}
            disabled={busy}
          >
            {busy
              ? "Fetching the list…"
              : current
                ? "Refresh CSL"
                : "Fetch CSL now"}
          </button>
        )}
      </div>
      <div className="csl-live" role="status">
        {status.isPending && (
          <p className="csl-muted">Checking list status…</p>
        )}
        {canAct && busy && (
          <p className="csl-progress-note">
            A list download is underway. This page checks for completion every
            few seconds.
          </p>
        )}
        {refresh.isSuccess && <RefreshOutcome result={refresh.data} />}
      </div>
      {refresh.isError && (
        <RefreshError error={refresh.error} keepsVersion={Boolean(current)} />
      )}
      {status.isError && (
        <div className="csl-status-error">
          <StatusError
            error={status.error}
            retrying={status.isFetching}
            onRetry={() => void status.refetch()}
          />
          {data && (
            <p className="csl-muted">
              The details below are from the last successful status check.
            </p>
          )}
        </div>
      )}
      {data && (
        <>
          {lastFetch?.status === "failed" && current && !refresh.isError && (
            <div className="notice danger csl-notice">
              <p>
                <strong>The latest fetch failed.</strong> The version fetched{" "}
                <LocalTime value={current.fetched_at} /> remains current.
              </p>
            </div>
          )}
          <Section title="Current version">
            {current ? (
              <dl className="detail-list csl-details">
                <Row label="Fetched">
                  <LocalTime value={current.fetched_at} />
                </Row>
                <Row label="Entries">{formatCount(current.entry_count)}</Row>
                <Row label="Changes">{describeChanges(current)}</Row>
                <Row label="File SHA-256">
                  <Hash value={current.file_sha256} />
                </Row>
                <Row label="Semantic SHA-256">
                  <Hash value={current.semantic_sha256} />
                </Row>
                <Row label="Version ID">
                  <Hash value={current.id} />
                </Row>
              </dl>
            ) : (
              <p className="csl-muted">No list version has been saved yet.</p>
            )}
          </Section>
          <Section title="Server activity">
            <dl className="detail-list csl-details">
              <Row label="List download">
                {busy ? "Underway" : "None running"}
              </Row>
              <Row label="Queued screenings">
                {data.pending_screenings > 0
                  ? `${formatCount(data.pending_screenings)} queued, not yet complete`
                  : "None"}
              </Row>
            </dl>
          </Section>
          <Section title="Latest fetch attempt">
            {lastFetch ? (
              <dl className="detail-list csl-details">
                <Row label="Status">
                  <FetchStatusBadge status={lastFetch.status} />
                </Row>
                <Row label="Started">
                  <LocalTime value={lastFetch.fetched_at} />
                </Row>
                <Row label="Completed">
                  {lastFetch.completed_at ? (
                    <LocalTime value={lastFetch.completed_at} />
                  ) : lastFetch.status === "fetching" ? (
                    "Still running"
                  ) : (
                    <span className="csl-muted">Not recorded</span>
                  )}
                </Row>
                <Row label="Version">
                  {lastFetch.version_id ? (
                    <>
                      <Hash value={lastFetch.version_id} />
                      {lastFetch.version_id === current?.id && " (current)"}
                    </>
                  ) : (
                    <span className="csl-muted">No version saved</span>
                  )}
                </Row>
                <Row label="File SHA-256">
                  <Hash value={lastFetch.file_sha256} />
                </Row>
                {lastFetch.error && (
                  <Row label="Error">
                    <span className="csl-error-text">{lastFetch.error}</span>
                  </Row>
                )}
                <Row label="Warnings">
                  {warnings.length > 0 ? (
                    <ul className="csl-warnings">
                      {warnings.map((warning, index) => (
                        <li key={index}>{warning}</li>
                      ))}
                    </ul>
                  ) : (
                    "None"
                  )}
                </Row>
              </dl>
            ) : (
              <p className="csl-muted">No fetch has been attempted yet.</p>
            )}
          </Section>
          <Section title="Source stamps">
            <p className="csl-muted">
              Each source’s update times as reported during a fetch. They can
              change even when the list content does not.
            </p>
            {lastFetch ? (
              <StampTables
                stamps={lastFetch.source_list_stamps}
                label="reported by the latest fetch"
                empty="The latest fetch did not record source stamps."
              />
            ) : (
              <p className="csl-muted">No fetch has recorded stamps yet.</p>
            )}
            {current && (
              <details className="csl-disclosure">
                <summary>Show stamps recorded with the current version</summary>
                <StampTables
                  stamps={current.source_list_stamps}
                  label="recorded with the current version"
                  empty="No source stamps were recorded with this version."
                />
              </details>
            )}
          </Section>
        </>
      )}
      <Section title="Version history">
        {versions.isPending ? (
          <p className="csl-muted">Loading version history…</p>
        ) : versions.isError ? (
          <>
            <div className="notice danger csl-notice" role="alert">
              <p>
                <strong>We couldn’t load the version history.</strong>{" "}
                {versions.error.message}
              </p>
            </div>
            <button
              type="button"
              className="button secondary"
              onClick={() => void versions.refetch()}
              disabled={versions.isFetching}
            >
              Try loading history again
            </button>
          </>
        ) : versions.data.length === 0 ? (
          <p className="csl-muted">No versions have been saved yet.</p>
        ) : (
          <ScrollTable
            caption={`Saved versions, newest first (up to ${CSL_VERSION_LIMIT})`}
          >
            <thead>
              <tr>
                <th scope="col">Fetched</th>
                <th scope="col" className="csl-num">
                  Entries
                </th>
                <th scope="col" className="csl-num">
                  Added
                </th>
                <th scope="col" className="csl-num">
                  Changed
                </th>
                <th scope="col" className="csl-num">
                  Removed
                </th>
                <th scope="col">File SHA-256</th>
              </tr>
            </thead>
            <tbody>
              {versions.data.map((version) => (
                <tr key={version.id}>
                  <th scope="row">
                    <LocalTime value={version.fetched_at} />
                    {version.id === current?.id && (
                      <span className="badge success csl-current">Current</span>
                    )}
                  </th>
                  <td className="csl-num">
                    {formatCount(version.entry_count)}
                  </td>
                  <td className="csl-num">+{formatCount(version.added)}</td>
                  <td className="csl-num">~{formatCount(version.changed)}</td>
                  <td className="csl-num">−{formatCount(version.removed)}</td>
                  <td>
                    <Hash value={version.file_sha256} />
                  </td>
                </tr>
              ))}
            </tbody>
          </ScrollTable>
        )}
      </Section>
    </section>
  );
}
