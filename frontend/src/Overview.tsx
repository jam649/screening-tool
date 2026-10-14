import { Link } from "react-router-dom";
import type { Session } from "./api";
import {
  LocalTime,
  RefreshError,
  RefreshOutcome,
  StatusError,
} from "./CSLPanel";
import { formatCount, isAdminSession, useCSL } from "./csl";
import { useSources, type Source } from "./sourceApi";

const TOTAL_STEPS = 3;

function ListStep({ csl }: { csl: ReturnType<typeof useCSL> }) {
  const { status, refresh, busy } = csl;
  // Only a successful read counts; loading or failed reads are unknown, not empty.
  const current = status.isSuccess ? status.data.current_version : null;
  const lastFetch = status.isSuccess ? status.data.last_fetch : null;
  const done = Boolean(current);
  return (
    <li>
      <span
        className={done ? "step-marker done" : "step-marker"}
        aria-hidden="true"
      >
        {done ? "✓" : "2"}
      </span>
      <div className="csl-step-body">
        <h3>
          {done
            ? "The Consolidated Screening List is downloaded"
            : "Download the Consolidated Screening List"}
        </h3>
        {status.isPending && <p>Checking the list status…</p>}
        {status.isError && (
          <StatusError
            error={status.error}
            retrying={status.isFetching}
            onRetry={() => void status.refetch()}
          />
        )}
        {current && (
          <>
            <p>
              Current version fetched <LocalTime value={current.fetched_at} />{" "}
              with {formatCount(current.entry_count)} entries.
            </p>
            {lastFetch?.status === "failed" && !refresh.isError && (
              <p className="csl-step-warning">
                The latest fetch, started{" "}
                <LocalTime value={lastFetch.fetched_at} />, failed. This
                version remains current.
              </p>
            )}
            <Link className="text-link" to="/settings">
              View list details
            </Link>
          </>
        )}
        {status.isSuccess && !current && (
          <>
            <p>
              The U.S. government’s list provides the source records for
              screening. Downloading it can take a minute.
            </p>
            {lastFetch?.status === "failed" && !refresh.isError && (
              <div className="notice danger csl-notice">
                <p>
                  <strong>The last attempt failed.</strong>{" "}
                  {lastFetch.error ?? "No error detail was recorded."}
                </p>
              </div>
            )}
            <div className="csl-step-actions">
              <button
                type="button"
                className="button primary"
                onClick={() => refresh.mutate()}
                disabled={busy}
              >
                {busy ? "Fetching the list…" : "Fetch CSL now"}
              </button>
            </div>
          </>
        )}
        <div className="csl-live" role="status">
          {status.isSuccess && busy && (
            <p className="csl-progress-note">
              A list download is underway. This can take a minute.
            </p>
          )}
          {refresh.isSuccess && <RefreshOutcome result={refresh.data} />}
        </div>
        {refresh.isError && (
          <RefreshError error={refresh.error} keepsVersion={done} />
        )}
      </div>
      {done && <span className="badge success">Complete</span>}
    </li>
  );
}

function latestImport(sources: Source[]): string | null {
  return sources.reduce<string | null>(
    (latest, source) =>
      source.last_synced_at &&
      (!latest || Date.parse(source.last_synced_at) > Date.parse(latest))
        ? source.last_synced_at
        : latest,
    null,
  );
}

function SourceStep({
  admin,
  sources,
}: {
  admin: boolean;
  sources: ReturnType<typeof useSources>;
}) {
  // Only a successful server import counts; an added but unsynced source does not.
  const list = sources.isSuccess ? sources.data : null;
  const imported = list?.filter((source) => source.last_synced_at) ?? [];
  const done = imported.length > 0;
  return (
    <li>
      <span
        className={done ? "step-marker done" : "step-marker"}
        aria-hidden="true"
      >
        {done ? "✓" : "3"}
      </span>
      <div className="csl-step-body">
        <h3>
          {done
            ? "Your customer and vendor records are imported"
            : admin
              ? "Add your customers and vendors"
              : "Your administrator adds customers and vendors"}
        </h3>
        {sources.isPending && <p>Checking your sources…</p>}
        {sources.isError && (
          <>
            <div className="notice danger csl-notice" role="alert">
              <p>
                <strong>We couldn’t load your sources.</strong>{" "}
                {sources.error.message}
              </p>
            </div>
            <button
              type="button"
              className="button secondary"
              onClick={() => void sources.refetch()}
              disabled={sources.isFetching}
            >
              {sources.isFetching ? "Checking again…" : "Check again"}
            </button>
          </>
        )}
        {list && done && (
          <p>
            {formatCount(imported.length)} of {formatCount(list.length)}{" "}
            {list.length === 1 ? "source has" : "sources have"} a successful
            import. Latest import <LocalTime value={latestImport(imported)} />.
          </p>
        )}
        {list && !done && (
          <p>
            {admin
              ? list.length > 0
                ? "A source is set up, but no file has been imported yet. Upload a CSV or XLSX file to bring in your records."
                : "Upload a CSV or XLSX file of your customer and vendor records."
              : "Your administrator imports customer and vendor records. You can view each source’s status."}
          </p>
        )}
        {admin && list && (
          <Link className="text-link" to="/sources">
            {done ? "View sources" : "Add source"}
          </Link>
        )}
        {!admin && (
          <Link className="text-link" to="/sources">
            Sources
          </Link>
        )}
      </div>
      {done && <span className="badge success">Imported</span>}
    </li>
  );
}

export function Overview({ session }: { session: Session }) {
  const admin = isAdminSession(session);
  const csl = useCSL({ enabled: admin });
  const sources = useSources({ enabled: true });
  const { status } = csl;
  const listDone = Boolean(status.data?.current_version);
  const sourcesDone = Boolean(
    sources.data?.some((source) => source.last_synced_at),
  );
  // Progress needs both reads; either one unknown would make the count false.
  const known = status.isSuccess && sources.isSuccess;
  const completed = 1 + (listDone ? 1 : 0) + (sourcesDone ? 1 : 0);
  const ready = admin && known && listDone && sourcesDone;
  const role = session.user.role.toLowerCase();
  return (
    <>
      <header className="page-header">
        <div>
          <div className="eyebrow">Your workspace</div>
          <h1>A clear place to begin.</h1>
          <p className="lede">
            {admin
              ? "Set up your data to start screening customers and vendors."
              : "Your administrator prepares the screening data. Here is where your workspace stands."}
          </p>
        </div>
        <span className={ready ? "badge success" : "badge"}>
          {admin
            ? ready
              ? "Data is ready"
              : "Setup in progress"
            : "Reviewer access"}
        </span>
      </header>
      <section
        className="panel welcome-panel"
        aria-labelledby="overview-checklist"
      >
        <div className="section-heading">
          <span className="eyebrow">Getting started</span>
          <h2 id="overview-checklist">
            {admin
              ? "Build your screening routine"
              : "How your workspace is prepared"}
          </h2>
          {admin && (
            <div className="setup-progress">
              {known ? (
                <>
                  <span>
                    {completed} of {TOTAL_STEPS} steps complete
                  </span>
                  <progress
                    value={completed}
                    max={TOTAL_STEPS}
                    aria-label="Workspace setup progress"
                  />
                </>
              ) : status.isError || sources.isError ? (
                <span>
                  Progress is unavailable until the list and source status
                  load.
                </span>
              ) : (
                <span>Checking setup progress…</span>
              )}
            </div>
          )}
        </div>
        <ol className="onboarding-list">
          <li>
            <span className="step-marker done" aria-hidden="true">
              ✓
            </span>
            <div className="csl-step-body">
              <h3>Your {role} account is ready</h3>
              <p>
                Signed in as {session.user.display_name} ({session.user.email}
                ).
              </p>
            </div>
            <span className="badge success">Complete</span>
          </li>
          {admin ? (
            <ListStep csl={csl} />
          ) : (
            <li>
              <span className="step-marker" aria-hidden="true">
                2
              </span>
              <div className="csl-step-body">
                <h3>Your administrator manages the screening list</h3>
                <p>
                  Downloading and refreshing the Consolidated Screening List is
                  an administrator task. Contact your workspace administrator
                  with questions about list updates.
                </p>
              </div>
              <span className="badge">Admin task</span>
            </li>
          )}
          <SourceStep admin={admin} sources={sources} />
        </ol>
      </section>
      {admin && status.isSuccess && status.data.pending_screenings > 0 && (
        <div className="quiet-note">
          <span aria-hidden="true">i</span>
          <p>
            {formatCount(status.data.pending_screenings)}{" "}
            {status.data.pending_screenings === 1
              ? "screening is"
              : "screenings are"}{" "}
            queued on the server. Results are not complete until they finish.
          </p>
        </div>
      )}
      <div className="quiet-note">
        <span aria-hidden="true">i</span>
        <p>
          Screening supports your due diligence. Every possible match is an
          opportunity for a considered human decision.
        </p>
      </div>
    </>
  );
}
