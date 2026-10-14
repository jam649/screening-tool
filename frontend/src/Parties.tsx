import { useId, type FormEvent } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { ApiError, type Session } from "./api";
import { canAdminister } from "./adminApi";
import { PartyRetentionControls } from "./RetentionControls";
import { PartyExports, PartyViewExport } from "./ExportControls";
import { AuditEntries } from "./Audit";
import { Hash, LocalTime } from "./CSLPanel";
import { formatCount } from "./csl";
import { JsonBlock, Fact, HitEvidence, LoadError, Pager, actorName } from "./ReviewQueue";
import {
  PARTY_FILTER_KEYS,
  bucketLabel,
  displayValue,
  formatScore,
  humanize,
  kindLabel,
  useParties,
  usePartyFile,
  type HitDetail,
  type Party,
  type PartyEvent,
  type PartyFilters,
} from "./reviewApi";
import { useSources } from "./sourceApi";
import "./review.css";

const STATUS_OPTIONS: [string, string][] = [
  ["open_hit", "Open item"],
  ["confirmed_match", "Confirmed match"],
  ["escalated", "Escalated"],
  ["suppressed", "Cleared as false positive"],
  ["clear", "No open item"],
  ["unscreened", "Not screened yet"],
  ["deleted", "Inactive"],
];
const STATUS_LABELS = Object.fromEntries(STATUS_OPTIONS);

// "No open item" is never shown for a record without a current screening.
export function partyStatusLabel(party: Party) {
  if (
    party.status === "unscreened" ||
    (party.status === "clear" && (party.screened_current === false || !party.last_screened_at))
  )
    return "Not screened yet";
  return STATUS_LABELS[party.status] ?? humanize(party.status);
}

function StatusBadge({ party }: { party: Party }) {
  const label = partyStatusLabel(party);
  const tone =
    party.status === "open_hit" || party.status === "confirmed_match" || party.status === "escalated"
      ? "rv-likely"
      : label === "No open item"
        ? "success"
        : "";
  return <span className={`badge ${tone}`}>{label}</span>;
}

function readFilters(params: URLSearchParams): PartyFilters {
  const filters: PartyFilters = { offset: Math.max(0, Number(params.get("offset")) || 0) };
  for (const key of PARTY_FILTER_KEYS) {
    const value = params.get(key)?.trim();
    if (value) filters[key] = value;
  }
  return filters;
}

const list = (values: string[] | null) => (values?.length ? values.join("; ") : "Not recorded");

export function Parties() {
  const [params, setParams] = useSearchParams();
  const filters = readFilters(params);
  const parties = useParties(filters);
  const sources = useSources({ enabled: true });
  const captionId = useId();
  const filtered = PARTY_FILTER_KEYS.some((key) => filters[key]);
  const formKey = `${PARTY_FILTER_KEYS.map((key) => filters[key] ?? "").join("|")}|${sources.isSuccess}`;

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const next = new URLSearchParams();
    for (const key of PARTY_FILTER_KEYS) {
      const value = String(data.get(key) ?? "").trim();
      if (value) next.set(key, key === "country" ? value.toUpperCase() : value);
    }
    setParams(next);
  }
  function page(offset: number) {
    const next = new URLSearchParams(params);
    if (offset) next.set("offset", String(offset));
    else next.delete("offset");
    setParams(next);
  }

  const data = parties.data;
  return (
    <>
      <header className="page-header">
        <div>
          <div className="eyebrow">Records</div>
          <h1>Parties</h1>
          <p className="lede">
            Your customers and vendors as currently imported, with their screening status.
          </p>
        </div>
      </header>
      <form
        key={formKey}
        className="panel rv-section rv-filters"
        onSubmit={submit}
        aria-label="Filter parties"
      >
        <label>
          Search
          <input name="q" type="search" defaultValue={filters.q} maxLength={500} />
        </label>
        <label>
          Role
          <select name="role" defaultValue={filters.role ?? ""}>
            <option value="">Any role</option>
            <option value="customer">Customer</option>
            <option value="vendor">Vendor</option>
            <option value="both">Both</option>
          </select>
        </label>
        <label>
          Source
          <select name="source_id" defaultValue={filters.source_id ?? ""}>
            <option value="">All sources</option>
            {sources.data?.map((source) => (
              <option key={source.id} value={source.id}>
                {source.label}
              </option>
            ))}
            {filters.source_id &&
              !sources.data?.some((source) => source.id === filters.source_id) && (
                <option value={filters.source_id}>Selected source</option>
              )}
          </select>
        </label>
        <label>
          Country
          <input
            name="country"
            defaultValue={filters.country}
            maxLength={2}
            pattern="[A-Za-z]{2}"
            title="Two-letter country code, such as US"
            autoComplete="off"
          />
        </label>
        <label>
          Status
          <select name="status" defaultValue={filters.status ?? ""}>
            <option value="">Any status</option>
            {STATUS_OPTIONS.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <div className="rv-checks">
          <label className="checkbox-label">
            <input type="checkbox" name="stale" value="true" defaultChecked={filters.stale === "true"} />
            <span>Only stale parties</span>
          </label>
          <label className="checkbox-label">
            <input
              type="checkbox"
              name="auto_cleared"
              value="true"
              defaultChecked={filters.auto_cleared === "true"}
            />
            <span>Only auto-cleared since last report</span>
          </label>
          <label className="checkbox-label">
            <input
              type="checkbox"
              name="include_deleted"
              value="true"
              defaultChecked={filters.include_deleted === "true"}
            />
            <span>Include inactive parties</span>
          </label>
        </div>
        <div className="rv-form-actions">
          <button type="submit" className="button primary">
            Apply filters
          </button>
          <button
            type="button"
            className="button secondary"
            disabled={!filtered}
            onClick={() => setParams(new URLSearchParams())}
          >
            Clear filters
          </button>
        </div>
        {sources.isError && (
          <p className="rv-muted">Source names could not be loaded; other filters still work.</p>
        )}
      </form>
      <PartyViewExport filters={filters} />
      {parties.isPending ? (
        <p className="rv-muted" role="status">
          Loading parties…
        </p>
      ) : !data ? (
        <LoadError
          title="We couldn’t load parties."
          error={parties.error}
          onRetry={() => void parties.refetch()}
          retrying={parties.isFetching}
        />
      ) : data.total === 0 ? (
        <section className="panel rv-empty">
          {filtered ? (
            <p>No parties match these filters.</p>
          ) : (
            <p>
              No parties have been imported yet. <Link to="/sources">View sources</Link>
            </p>
          )}
        </section>
      ) : (
        <>
          {parties.isError && (
            <LoadError
              title="This page could not be refreshed."
              error={parties.error}
              onRetry={() => void parties.refetch()}
              retrying={parties.isFetching}
            />
          )}
          <p className="rv-live" role="status">
            {parties.isFetching ? "Updating…" : ""}
          </p>
          <div className="rv-table-wrap" role="region" aria-labelledby={captionId} tabIndex={0}>
            <table className="rv-table">
              <caption id={captionId}>Parties</caption>
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Role</th>
                  <th scope="col">Source</th>
                  <th scope="col">Country</th>
                  <th scope="col">Status</th>
                  <th scope="col">Last screened</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((party) => (
                  <tr key={party.id}>
                    <th scope="row">
                      <Link to={`/parties/${encodeURIComponent(party.id)}`}>{party.name}</Link>
                      {party.external_id && <span className="rv-sub">{party.external_id}</span>}
                    </th>
                    <td className="capitalize">{party.role}</td>
                    <td>{party.source?.label ?? "—"}</td>
                    <td>{party.country_iso2 ?? "—"}</td>
                    <td>
                      <span className="rv-badges">
                        <StatusBadge party={party} />
                        {party.stale && <span className="badge rv-review">Stale</span>}
                        {party.auto_cleared && <span className="badge">Auto-cleared since report</span>}
                        {party.suppressed_pairings > 0 && (
                          <span className="badge">
                            {formatCount(party.suppressed_pairings)} remembered false{" "}
                            {party.suppressed_pairings === 1 ? "positive" : "positives"}
                          </span>
                        )}
                      </span>
                    </td>
                    <td>
                      {party.last_screened_at ? (
                        <LocalTime value={party.last_screened_at} />
                      ) : (
                        <span className="rv-muted">Not recorded</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pager
            offset={filters.offset}
            count={data.items.length}
            total={data.total}
            noun="parties"
            busy={parties.isPlaceholderData}
            onChange={page}
          />
        </>
      )}
    </>
  );
}

function outcome(hit: HitDetail) {
  if (hit.completed_disposition) return `Previous human decision retained · ${kindLabel(hit.completed_disposition.kind)} · ${actorName(hit.completed_disposition)}`;
  const latest = hit.dispositions[0];
  if (latest) return `${kindLabel(latest.kind)} · ${actorName(latest)}`;
  if (hit.status === "suppressed")
    return hit.suppression_disposition
      ? `Remembered false positive · ${actorName(hit.suppression_disposition)}`
      : "Remembered false positive";
  if (hit.status === "open") return hit.bucket === "low" ? "Below the review threshold" : "Open";
  return humanize(hit.status);
}

function HitRecord({ hit }: { hit: HitDetail }) {
  return (
    <details className="rv-hit">
      <summary>
        <span>
          <strong>{displayValue(hit.party_snapshot.name)}</strong> compared with{" "}
          <strong>{displayValue(hit.entry_snapshot.name)}</strong>
        </span>
        <span className="rv-hit-meta">
          <span className={`badge rv-${hit.bucket}`}>
            {bucketLabel(hit.bucket)} · {formatScore(hit.final_confidence)}
          </span>
          <span>{outcome(hit)}</span>
        </span>
      </summary>
      <div className="rv-hit-body">
        {hit.status === "open" && hit.bucket !== "low" && (
          <Link className="text-link" to={`/queue?hit=${encodeURIComponent(hit.id)}`}>
            Find in the review queue
          </Link>
        )}
        <HitEvidence hit={hit} />
      </div>
    </details>
  );
}

function RunEvent({ event }: { event: PartyEvent }) {
  const { run, previously_cleared: cleared } = event;
  return (
    <li className="rv-event">
      <h3>
        Screening run <LocalTime value={run.finished_at ?? run.started_at} />
      </h3>
      <dl className="rv-facts">
        <Fact label="Trigger">{humanize(run.trigger)}</Fact>
        <Fact label="Started"><LocalTime value={run.started_at} /></Fact>
        <Fact label="Finished">
          {run.finished_at ? <LocalTime value={run.finished_at} /> : "In progress"}
        </Fact>
        <Fact label="List version"><Hash value={run.csl_version_id} /></Fact>
        <Fact label="Run ID"><Hash value={run.id} /></Fact>
        <Fact label="Party record hash"><Hash value={event.party_hash} /></Fact>
      </dl>
      {event.party_evidence && (
        <details className="rv-disclosure">
          <summary>Party record preserved for this run</summary>
          <p>{formatCount(event.party_evidence.candidates_considered)} candidates considered · {formatCount(event.party_evidence.hits_recorded)} hit records recorded.</p>
          <JsonBlock value={event.party_evidence.party_snapshot} />
          <h4>Source recorded for this run</h4><JsonBlock value={event.party_evidence.source_snapshot} />
          <h4>Recorded results</h4><JsonBlock value={event.party_evidence.result_counts} />
        </details>
      )}
      {(event.purged_comparisons ?? []).map((record, index) => (
        <details className="rv-disclosure" key={`${record.certificate_id}:${index}`}>
          <summary>Comparison preserved in purge certificate</summary>
          <p>The original comparison and decisions remain in the permanent certificate after retention removed their database rows.</p>
          <dl className="rv-facts"><Fact label="Certificate"><Hash value={record.certificate_id} /></Fact><Fact label="Certificate checksum"><Hash value={record.certificate_sha256} /></Fact></dl>
          <h4>Original comparison</h4><JsonBlock value={record.hit} />
          <h4>Original decisions and recorded actors</h4><JsonBlock value={record.dispositions} />
        </details>
      ))}
      {event.zero_hits ? (
        <p className="rv-zero">No candidate hits recorded in this run.</p>
      ) : (
        event.hits.length > 0 && (
          <ul className="rv-hits">
            {event.hits.map((hit) => (
              <li key={hit.id}>
                <HitRecord hit={hit} />
              </li>
            ))}
          </ul>
        )
      )}
      {cleared.count > 0 && (
        <details className="rv-cleared">
          <summary>Previously cleared ({formatCount(cleared.count)})</summary>
          <ul className="rv-hits">
            {cleared.hits.map((hit) => (
              <li key={hit.id}>
                <HitRecord hit={hit} />
              </li>
            ))}
          </ul>
        </details>
      )}
    </li>
  );
}

export function PartyFile({ session }: { session: Session }) {
  const { id = "" } = useParams();
  const file = usePartyFile(id);
  const currentId = useId();
  const historyId = useId();
  const auditId = useId();
  const back = (
    <p>
      <Link className="text-link" to="/parties">
        ← All parties
      </Link>
    </p>
  );
  if (file.isPending)
    return (
      <>
        {back}
        <p className="rv-muted" role="status">
          Loading the party file…
        </p>
      </>
    );
  if (!file.data)
    return (
      <>
        {back}
        {file.error instanceof ApiError && file.error.status === 404 ? (
          <section className="panel rv-empty">
            <h1>This party was not found.</h1>
          </section>
        ) : (
          <LoadError
            title="We couldn’t load this party file."
            error={file.error}
            onRetry={() => void file.refetch()}
            retrying={file.isFetching}
          />
        )}
      </>
    );
  const { party, source, events, audit_log } = file.data;
  return (
    <>
      {back}
      <header className="page-header">
        <div>
          <div className="eyebrow">Party file</div>
          <h1>{party.name}</h1>
          <p className="lede">
            Current record from {source?.label ?? "an unrecorded source"}. The history keeps each
            record exactly as it was compared.
          </p>
        </div>
        <div><StatusBadge party={party} /><PartyExports id={id} /></div>
      </header>
      <section className="panel rv-section" aria-labelledby={currentId}>
        <h2 id={currentId}>Current record</h2>
        <dl className="detail-list">
          <Fact label="External ID">{party.external_id ?? "Not recorded"}</Fact>
          <Fact label="Role"><span className="capitalize">{party.role}</span></Fact>
          <Fact label="Party type"><span className="capitalize">{party.party_type}</span></Fact>
          <Fact label="Alternate names">{list(party.alt_names)}</Fact>
          <Fact label="Address">{list(party.address_lines)}</Fact>
          <Fact label="City">{party.city ?? "Not recorded"}</Fact>
          <Fact label="Region">{party.region ?? "Not recorded"}</Fact>
          <Fact label="Postal code">{party.postal_code ?? "Not recorded"}</Fact>
          <Fact label="Country">{party.country_iso2 ?? "Not recorded"}</Fact>
          <Fact label="Source">{source?.label ?? "Not recorded"}</Fact>
          <Fact label="Last screened">
            {party.last_screened_at ? (
              <LocalTime value={party.last_screened_at} />
            ) : (
              "Not screened yet in its current form"
            )}
          </Fact>
          <Fact label="Stale">
            {party.stale ? "Yes, not screened in the last 24 hours" : "No"}
          </Fact>
          <Fact label="Remembered false positives">{formatCount(party.suppressed_pairings)}</Fact>
          {party.deleted_at && (
            <Fact label="Inactive since"><LocalTime value={party.deleted_at} /></Fact>
          )}
          <Fact label="Record hash"><Hash value={party.record_hash} /></Fact>
          <Fact label="Party ID"><Hash value={party.id} /></Fact>
        </dl>
      </section>
      {canAdminister(session) && <section className="panel rv-section"><PartyRetentionControls session={session} partyId={id} /></section>}
      <section className="panel rv-section" aria-labelledby={historyId}>
        <h2 id={historyId}>Screening history</h2>
        {events.length === 0 ? (
          <p className="rv-muted">No completed screening run includes this party yet.</p>
        ) : (
          <ol className="rv-events">
            {events.map((event) => (
              <RunEvent key={event.run.id} event={event} />
            ))}
          </ol>
        )}
      </section>
      <section className="panel rv-section" aria-labelledby={auditId}>
        <h2 id={auditId}>Audit log</h2>
        {audit_log.length ? (
          <AuditEntries entries={audit_log} />
        ) : (
          <p className="rv-muted">No audit events are recorded for this party.</p>
        )}
      </section>
    </>
  );
}
