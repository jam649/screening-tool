import { useId, useState, type FormEvent } from "react";
import { Hash, LocalTime } from "./CSLPanel";
import { formatCount } from "./csl";
import { Fact, JsonBlock, LoadError, Pager } from "./ReviewQueue";
import {
  asRecord,
  duration,
  humanize,
  useAuditLog,
  useRuns,
  type AuditEntry,
  type AuditFilters,
  type Run,
} from "./reviewApi";
import "./review.css";

export function AuditEntries({ entries }: { entries: AuditEntry[] }) {
  return (
    <ol className="rv-audit">
      {entries.map((entry) => {
        const note = asRecord(entry.after).note;
        return (
          <li key={entry.id}>
            <div className="rv-audit-head">
              <code className="rv-code">{entry.action}</code>
              <LocalTime value={entry.created_at} />
            </div>
            <p>
              {entry.actor
                ? `${entry.actor.display_name} (${entry.actor.email})`
                : "No user recorded"}{" "}
              · {humanize(entry.target_table)}{" "}
              {entry.target_id && <Hash value={entry.target_id} />}
            </p>
            {typeof note === "string" && note && <p className="rv-note">Note: {note}</p>}
            {(entry.before || entry.after) && (
              <details className="rv-disclosure">
                <summary>Recorded values</summary>
                {entry.before && (
                  <>
                    <h4>Before</h4>
                    <JsonBlock value={entry.before} />
                  </>
                )}
                {entry.after && (
                  <>
                    <h4>After</h4>
                    <JsonBlock value={entry.after} />
                  </>
                )}
              </details>
            )}
          </li>
        );
      })}
    </ol>
  );
}

function RunSummary({ run }: { run: Run }) {
  const took = duration(run.started_at, run.finished_at);
  const fallbacks = Array.isArray(run.model_fallbacks) ? run.model_fallbacks : [];
  return (
    <details className="rv-run">
      <summary>
        <strong>
          <LocalTime value={run.started_at} />
        </strong>
        <span>{humanize(run.trigger)}</span>
        <span>
          {formatCount(run.parties_screened)} parties · {formatCount(run.hits_new)} new ·{" "}
          {formatCount(run.hits_suppressed)} remembered
        </span>
        <span className={run.finished_at ? "badge success" : "badge"}>
          {run.finished_at ? (took ? `Finished in ${took}` : "Finished") : "In progress"}
        </span>
      </summary>
      <dl className="detail-list">
        <Fact label="Run ID"><Hash value={run.id} /></Fact>
        <Fact label="List version"><Hash value={run.csl_version_id} /></Fact>
        <Fact label="Started"><LocalTime value={run.started_at} /></Fact>
        <Fact label="Finished">
          {run.finished_at ? <LocalTime value={run.finished_at} /> : "In progress"}
        </Fact>
        <Fact label="Parties screened">{formatCount(run.parties_screened)}</Fact>
        <Fact label="New open items">{formatCount(run.hits_new)}</Fact>
        <Fact label="Matched a remembered false positive">{formatCount(run.hits_suppressed)}</Fact>
        {typeof run.auto_cleared === "number" && (
          <Fact label="Auto-cleared">{formatCount(run.auto_cleared)}</Fact>
        )}
        <Fact label="Model">{run.model_name ?? "Not used"}</Fact>
      </dl>
      <details className="rv-disclosure">
        <summary>Settings used for this run</summary>
        <JsonBlock value={run.settings_snapshot} />
      </details>
      {fallbacks.length > 0 && (
        <details className="rv-disclosure">
          <summary>Model fallbacks ({formatCount(fallbacks.length)})</summary>
          <JsonBlock value={fallbacks} />
        </details>
      )}
    </details>
  );
}

function RunsSection() {
  const [offset, setOffset] = useState(0);
  const runs = useRuns(offset);
  const titleId = useId();
  return (
    <section className="panel rv-section" aria-labelledby={titleId}>
      <h2 id={titleId}>Screening runs</h2>
      {runs.isPending ? (
        <p className="rv-muted" role="status">
          Loading screening runs…
        </p>
      ) : runs.isError || !runs.data ? (
        <LoadError
          title="We couldn’t load screening runs."
          error={runs.error}
          onRetry={() => void runs.refetch()}
          retrying={runs.isFetching}
        />
      ) : runs.data.total === 0 ? (
        <p className="rv-muted">No screening run has been recorded yet.</p>
      ) : (
        <>
          <ol className="rv-runs">
            {runs.data.items.map((run) => (
              <li key={run.id}>
                <RunSummary run={run} />
              </li>
            ))}
          </ol>
          <Pager
            offset={offset}
            count={runs.data.items.length}
            total={runs.data.total}
            noun="runs"
            busy={runs.isPlaceholderData}
            onChange={setOffset}
          />
        </>
      )}
    </section>
  );
}

function AuditLogSection() {
  const [filters, setFilters] = useState<AuditFilters>({ offset: 0 });
  const log = useAuditLog(filters);
  const titleId = useId();
  const filtered = Boolean(filters.action || filters.target_table || filters.target_id);
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const read = (key: string) => String(data.get(key) ?? "").trim() || undefined;
    setFilters({
      action: read("action"),
      target_table: read("target_table"),
      target_id: read("target_id"),
      offset: 0,
    });
  }
  return (
    <section className="panel rv-section" aria-labelledby={titleId}>
      <h2 id={titleId}>Audit log</h2>
      <form
        key={`${filters.action}|${filters.target_table}|${filters.target_id}`}
        className="rv-filters"
        onSubmit={submit}
        aria-label="Filter audit log"
      >
        <label>
          Action
          <input name="action" defaultValue={filters.action} placeholder="review.disposition" />
        </label>
        <label>
          Record type
          <input name="target_table" defaultValue={filters.target_table} placeholder="dispositions" />
        </label>
        <label>
          Record ID
          <input name="target_id" defaultValue={filters.target_id} />
        </label>
        <div className="rv-form-actions">
          <button type="submit" className="button primary">
            Apply filters
          </button>
          <button
            type="button"
            className="button secondary"
            disabled={!filtered}
            onClick={() => setFilters({ offset: 0 })}
          >
            Clear filters
          </button>
        </div>
      </form>
      {log.isPending ? (
        <p className="rv-muted" role="status">
          Loading the audit log…
        </p>
      ) : log.isError || !log.data ? (
        <LoadError
          title="We couldn’t load the audit log."
          error={log.error}
          onRetry={() => void log.refetch()}
          retrying={log.isFetching}
        />
      ) : log.data.total === 0 ? (
        <p className="rv-muted">
          {filtered ? "No audit records match these filters." : "No audit records yet."}
        </p>
      ) : (
        <>
          <AuditEntries entries={log.data.items} />
          <Pager
            offset={filters.offset}
            count={log.data.items.length}
            total={log.data.total}
            noun="audit records"
            busy={log.isPlaceholderData}
            onChange={(offset) => setFilters({ ...filters, offset })}
          />
        </>
      )}
    </section>
  );
}

export function Audit() {
  return (
    <>
      <header className="page-header">
        <div>
          <div className="eyebrow">Records</div>
          <h1>Audit</h1>
          <p className="lede">
            Every screening run and recorded action, as stored on the server.
          </p>
        </div>
      </header>
      <RunsSection />
      <AuditLogSection />
    </>
  );
}
