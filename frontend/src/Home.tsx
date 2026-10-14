import { Link } from "react-router-dom";
import type { ReactNode } from "react";
import type { Session } from "./api";
import { Hash, LocalTime } from "./CSLPanel";
import { formatCount } from "./csl";
import { Overview } from "./Overview";
import { LoadError } from "./ReviewQueue";
import { useDashboard, type DashboardData } from "./reviewApi";
import "./review.css";

function Summary({
  data,
  version,
}: {
  data: DashboardData;
  version: NonNullable<DashboardData["csl_version"]>;
}) {
  const covered = data.coverage_latest_version ?? data.parties_screened;
  const remaining = data.total_parties - covered;
  const attention = data.sync_health.filter((source) => source.stale || source.last_error);
  const counts: { to: string; value: number; label: string; help: ReactNode }[] = [
    { to: "/queue", value: data.open_items, label: "Open items", help: "Awaiting a human decision" },
    {
      to: "/queue?bucket=likely",
      value: data.likely_matches,
      label: "Likely matches",
      help: "Highest-confidence comparisons",
    },
    {
      to: "/parties?auto_cleared=true",
      value: data.auto_cleared_since_report,
      label: "Auto-cleared",
      help: data.last_report_at ? (
        <>
          Since the report on <LocalTime value={data.last_report_at} />
        </>
      ) : (
        "All time; no report has been recorded yet"
      ),
    },
    {
      to: "/parties?stale=true",
      value: data.stale_parties,
      label: "Stale parties",
      help: "Not screened in the last 24 hours",
    },
  ];
  return (
    <>
      <header className="page-header">
        <div>
          <div className="eyebrow">Your workspace</div>
          <h1>Overview</h1>
          <p className="lede">
            {formatCount(covered)} of {formatCount(data.total_parties)} parties screened against
            list version <Hash value={version.id} /> fetched <LocalTime value={version.last_fetched_at ?? version.fetched_at} />
            .
          </p>
        </div>
      </header>
      {remaining > 0 && (
        <div className="notice rv-info">
          {formatCount(remaining)} {remaining === 1 ? "party has" : "parties have"} not yet been
          screened against the latest list version. Coverage is not complete until{" "}
          {remaining === 1 ? "it is" : "they are"}.
        </div>
      )}
      <ul className="rv-counts" aria-label="Work to review">
        {counts.map((count) => (
          <li key={count.to}>
            <Link className="panel rv-count" to={count.to}>
              <strong>{formatCount(count.value)}</strong>
              <span>{count.label}</span>
              <small>{count.help}</small>
            </Link>
          </li>
        ))}
      </ul>
      {attention.length > 0 && (
        <div className="notice rv-warning">
          <p>
            <strong>Some sources need attention.</strong>
          </p>
          <ul>
            {attention.map((source) => (
              <li key={source.id}>
                {source.label}:{" "}
                {source.last_error ? (
                  `the last sync failed. ${source.last_error}`
                ) : source.last_synced_at ? (
                  <>
                    last synced <LocalTime value={source.last_synced_at} />, more than 24 hours ago.
                  </>
                ) : (
                  "never synced."
                )}
              </li>
            ))}
          </ul>
          <Link className="text-link" to="/sources">
            Review sources
          </Link>
        </div>
      )}
      <section className="panel rv-section" aria-label="Screening list and runs">
        <p>
          The current list version has {formatCount(version.entry_count)} entries:{" "}
          {formatCount(version.added)} added, {formatCount(version.changed)} changed and{" "}
          {formatCount(version.removed)} removed.
        </p>
        <p className="rv-muted">
          {data.last_run?.finished_at ? (
            <>
              The last completed screening run finished{" "}
              <LocalTime value={data.last_run.finished_at} />.
            </>
          ) : (
            data.last_run ? "The latest screening run has not finished yet." : "No screening run has finished yet."
          )}
        </p>
      </section>
      <div className="quiet-note">
        <span aria-hidden="true">i</span>
        <p>
          Screening supports your due diligence. Every possible match is an opportunity for a
          considered human decision.
        </p>
      </div>
    </>
  );
}

export function Home({ session }: { session: Session }) {
  const dashboard = useDashboard();
  if (dashboard.isPending)
    return (
      <p className="rv-muted" role="status">
        Loading your workspace…
      </p>
    );
  // An unreadable summary is unknown, never treated as a first run.
  if (!dashboard.data)
    return (
      <>
        <header className="page-header">
          <div>
            <div className="eyebrow">Your workspace</div>
            <h1>Overview</h1>
          </div>
        </header>
        <LoadError
          title="We couldn’t load your workspace summary."
          error={dashboard.error}
          onRetry={() => void dashboard.refetch()}
          retrying={dashboard.isFetching}
        />
      </>
    );
  const data = dashboard.data;
  if (!data.csl_version || data.total_parties === 0) return <Overview session={session} />;
  return <>
    {dashboard.isError && <LoadError title="These figures could not be refreshed; they may be out of date." error={dashboard.error} onRetry={() => void dashboard.refetch()} retrying={dashboard.isFetching} />}
    <Summary data={data} version={data.csl_version} />
  </>;
}
