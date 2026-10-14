import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useSearchParams } from "react-router-dom";
import { ApiError } from "./api";
import { Hash, LocalTime } from "./CSLPanel";
import { formatCount } from "./csl";
import {
  PAGE_SIZE,
  age,
  asRecord,
  bucketLabel,
  comparisonRows,
  displayValue,
  formatProbability,
  formatScore,
  humanize,
  kindLabel,
  matchedTokens,
  normalizeToken,
  reasons,
  safeHttpUrl,
  useDecision,
  useHit,
  useQueue,
  type DecisionKind,
  type Disposition,
  type HitDetail,
  type HitSummary,
  type JsonRecord,
} from "./reviewApi";
import "./review.css";

export function LoadError({
  title,
  error,
  onRetry,
  retrying,
}: {
  title: string;
  error: unknown;
  onRetry: () => void;
  retrying: boolean;
}) {
  return (
    <div className="rv-error">
      <div className="notice danger" role="alert">
        <strong>{title}</strong> {error instanceof Error ? error.message : null}
      </div>
      <button
        type="button"
        className="button secondary"
        onClick={onRetry}
        disabled={retrying}
      >
        {retrying ? "Trying again…" : "Try again"}
      </button>
    </div>
  );
}

export function JsonBlock({ value }: { value: unknown }) {
  return (
    <pre className="rv-json" tabIndex={0}>
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

export function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

// Text stays text: matched words are wrapped in <mark>, never injected as HTML.
export function Highlight({ text, tokens }: { text: string; tokens: ReadonlySet<string> }) {
  if (!tokens.size) return <>{text}</>;
  return (
    <>
      {text.split(/([\p{L}\p{N}]+)/u).map((part, index) =>
        index % 2 === 1 && tokens.has(normalizeToken(part)) ? (
          <mark key={index}>{part}</mark>
        ) : (
          part
        ),
      )}
    </>
  );
}

function Values({ values, tokens }: { values: string[]; tokens?: ReadonlySet<string> }) {
  if (!values.length) return <span className="rv-muted">Not recorded</span>;
  return (
    <ul className="rv-values">
      {values.map((value, index) => (
        <li key={index}>{tokens ? <Highlight text={value} tokens={tokens} /> : value}</li>
      ))}
    </ul>
  );
}

export function Comparison({ hit }: { hit: HitDetail }) {
  const tokens = matchedTokens(hit.explanation);
  return (
    <table className="rv-compare">
      <caption>Recorded at screening time</caption>
      <thead>
        <tr>
          <th scope="col">Field</th>
          <th scope="col">Your record</th>
          <th scope="col">Listing</th>
        </tr>
      </thead>
      <tbody>
        {comparisonRows(hit.party_snapshot, hit.entry_snapshot).map((row) => (
          <tr key={row.key}>
            <th scope="row">{row.label}</th>
            <td data-label="Your record">
              <Values values={row.party} tokens={row.names ? tokens.party : row.key === "address" ? tokens.address : undefined} />
            </td>
            <td data-label="Listing">
              <Values values={row.entry} tokens={row.names ? tokens.entry : row.key === "address" ? tokens.address : undefined} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function Scores({ hit }: { hit: HitSummary }) {
  return (
    <dl className="rv-scores">
      <Fact label="Confidence">{formatScore(hit.final_confidence)}</Fact>
      <Fact label="Deterministic score">{formatScore(hit.det_score)}</Fact>
      <Fact label="Model probability">{formatProbability(hit.model_prob)}</Fact>
      <Fact label="Bucket">{bucketLabel(hit.bucket)}</Fact>
    </dl>
  );
}

export function actorName(decision: Disposition) {
  if (decision.actor) return decision.actor.display_name;
  return decision.kind === "auto_cleared" ? "Automatic rule" : "No user recorded";
}

export function DecisionRecord({ decision, label }: { decision: Disposition; label?: string }) {
  return (
    <div className="rv-decision">
      <p>
        <strong>{label ?? kindLabel(decision.kind)}</strong> by{" "}
        {decision.actor
          ? `${decision.actor.display_name} (${decision.actor.email})`
          : actorName(decision)}{" "}
        on <LocalTime value={decision.created_at} />
      </p>
      <p className="rv-note">
        {decision.note || <span className="rv-muted">No note was recorded.</span>}
      </p>
    </div>
  );
}

type Side = "party" | "entry";
const sideLabel = (side: Side) => (side === "party" ? "Your record" : "Listing");
const RAW_KEY = "raw";

// Counts recorded top-level keys whose values differ, for the summary only; nothing is re-scored.
function differingKeys(before: unknown, after: unknown): number | null {
  if (asRecord(before) !== before || asRecord(after) !== after) return null;
  const a = before as JsonRecord;
  const b = after as JsonRecord;
  return [...new Set([...Object.keys(a), ...Object.keys(b)])].filter(
    (key) => JSON.stringify(a[key]) !== JSON.stringify(b[key]),
  ).length;
}

// The raw source payload can run to thousands of characters, so it is disclosed on request, in full.
function RawChange({ side, change }: { side: Side; change: JsonRecord }) {
  const count = differingKeys(change.before, change.after);
  return (
    <details className="rv-disclosure rv-raw">
      <summary>
        {sideLabel(side)} · Raw source data changed
        <span className="rv-muted">
          {" · "}
          {count === null
            ? "show exact before and after"
            : `${formatCount(count)} top-level ${count === 1 ? "field differs" : "fields differ"}; show exact before and after`}
        </span>
      </summary>
      <h5>Before</h5>
      <JsonBlock value={change.before} />
      <h5>After</h5>
      <JsonBlock value={change.after} />
    </details>
  );
}

export function WhatChanged({ changes }: { changes: unknown }) {
  const record = asRecord(changes);
  const rows = (["party", "entry"] as const).flatMap((side) =>
    Object.entries(asRecord(record[side])).map(([key, value]) => ({
      side,
      key,
      change: asRecord(value),
    })),
  );
  if (!rows.length)
    return <p className="rv-muted">No field differences were recorded since that decision.</p>;
  const fields = rows.filter((row) => row.key !== RAW_KEY);
  const raw = rows.filter((row) => row.key === RAW_KEY);
  return (
    <>
      {fields.length ? (
        <dl className="rv-changes">
          {fields.map(({ side, key, change }) => (
            <div key={`${side}.${key}`}>
              <dt>
                {sideLabel(side)} · {humanize(key)}
              </dt>
              <dd>
                <span className="rv-before">{displayValue(change.before)}</span>{" "}
                <span aria-hidden="true">→</span>
                <span className="rv-sr">changed to</span>{" "}
                <span className="rv-after">{displayValue(change.after)}</span>
              </dd>
            </div>
          ))}
        </dl>
      ) : (
        <p className="rv-muted">
          Only the raw source data changed since that decision; no named field differs.
        </p>
      )}
      {raw.map(({ side, change }) => (
        <RawChange key={side} side={side} change={change} />
      ))}
    </>
  );
}

export function PriorDecision({ hit }: { hit: HitDetail }) {
  const prior = hit.prior_disposition;
  if (!prior) return null;
  const rule = asRecord(hit.explanation.hash_rule_change);
  return (
    <section className="rv-prior" aria-label="Earlier decision on this pairing">
      <h3>Earlier decision on this pairing</h3>
      <DecisionRecord decision={prior} label={`Previously: ${kindLabel(prior.kind)}`} />
      {Object.keys(rule).length > 0 && (
        <p className="rv-muted">
          Matching rule changed from version {displayValue(rule.previous)} to{" "}
          {displayValue(rule.current)}.{" "}
          {typeof rule.reason === "string" ? rule.reason : null}
        </p>
      )}
      <h4>What changed since then</h4>
      {/* Keyed by comparison so a different pair always starts with its disclosures closed. */}
      <WhatChanged key={hit.id} changes={prior.what_changed} />
    </section>
  );
}

export function HitEvidence({ hit }: { hit: HitDetail }) {
  return (
    <>
      <Scores hit={hit} />
      <PriorDecision hit={hit} />
      {hit.completed_disposition && (
        <section className="rv-remembered" aria-label="Previous human decision retained">
          <h3>Previous human decision retained</h3>
          <p className="rv-muted">The same records were assessed earlier. This observation retains that decision; no new human decision was recorded.</p>
          <DecisionRecord decision={hit.completed_disposition} />
        </section>
      )}
      {hit.suppression_disposition && (
        <section className="rv-remembered" aria-label="Remembered false-positive decision">
          <h3>Remembered false-positive decision</h3>
          <p className="rv-muted">
            This exact pairing of records was cleared earlier, so this observation was not queued.
          </p>
          <DecisionRecord decision={hit.suppression_disposition} />
        </section>
      )}
      <Comparison hit={hit} />
      <section className="rv-why" aria-label="How this comparison scored">
        <h3>How this comparison scored</h3>
        <ul>
          {reasons(hit.explanation).map((reason) => (
            <li key={reason}>{reason}</li>
          ))}
        </ul>
        <details className="rv-disclosure">
          <summary>Full explanation</summary>
          <JsonBlock value={hit.explanation} />
        </details>
      </section>
      <section className="rv-history" aria-label="Decisions on this comparison">
        <h3>Decisions on this comparison</h3>
        {hit.dispositions.length ? (
          hit.dispositions.map((item) => <DecisionRecord key={item.id} decision={item} />)
        ) : (
          <p className="rv-muted">
            {hit.completed_disposition
              ? "No separate decision was made on this observation; the previous human decision above was retained."
              : hit.status === "suppressed"
              ? "No separate decision was made on this observation; the remembered decision above applied."
              : "No decision has been recorded on this comparison yet."}
          </p>
        )}
      </section>
      <details className="rv-disclosure">
        <summary>Provenance</summary>
        <dl className="detail-list">
          <Fact label="Comparison ID"><Hash value={hit.id} /></Fact>
          <Fact label="Screening run"><Hash value={hit.run_id} /></Fact>
          <Fact label="Party record hash"><Hash value={hit.party_hash} /></Fact>
          <Fact label="Listing hash"><Hash value={hit.entry_hash} /></Fact>
          <Fact label="Listing source">{hit.source_list ?? "Not recorded"}</Fact>
          <Fact label="Recorded"><LocalTime value={hit.created_at} /></Fact>
          <Fact label="Status">{humanize(hit.status)}</Fact>
        </dl>
      </details>
    </>
  );
}

export function Pager({
  offset,
  count,
  total,
  noun,
  busy,
  onChange,
}: {
  offset: number;
  count: number;
  total: number;
  noun: string;
  busy: boolean;
  onChange: (offset: number) => void;
}) {
  if (total === 0) return null;
  return (
    <nav className="rv-pager" aria-label={`${noun} pages`}>
      <span>
        {count > 0
          ? `Showing ${formatCount(offset + 1)}–${formatCount(offset + count)} of ${formatCount(total)} ${noun}`
          : `No ${noun} on this page; ${formatCount(total)} in total`}
      </span>
      <span className="rv-pager-buttons">
        <button
          type="button"
          className="button secondary"
          disabled={busy || offset === 0}
          onClick={() => onChange(Math.max(0, offset - PAGE_SIZE))}
        >
          Previous
        </button>
        <button
          type="button"
          className="button secondary"
          disabled={busy || offset + PAGE_SIZE >= total}
          onClick={() => onChange(offset + PAGE_SIZE)}
        >
          Next
        </button>
      </span>
    </nav>
  );
}

const FILTERS = [
  ["all", "All open items"],
  ["likely", "Likely matches"],
  ["review", "Needs review"],
] as const;
type Filter = (typeof FILTERS)[number][0];

const DECISIONS: { kind: DecisionKind; key: string; label: string; tone: string }[] = [
  { kind: "false_positive", key: "F", label: "Clear as false positive", tone: "secondary" },
  { kind: "confirmed_match", key: "M", label: "Confirmed match", tone: "primary" },
  { kind: "escalated", key: "E", label: "Escalate", tone: "secondary" },
];
const NOTE_REQUIRED: Record<DecisionKind, string> = {
  false_positive: "",
  confirmed_match: "Add a note explaining why this is a match.",
  escalated: "Add a note explaining why this needs escalation.",
};

function isTyping(target: EventTarget | null) {
  return (
    target instanceof HTMLElement &&
    (target.isContentEditable || target.closest("input, textarea, select") !== null)
  );
}

export function ReviewQueue() {
  const [params, setParams] = useSearchParams();
  const raw = params.get("bucket");
  const filter: Filter = raw === "likely" || raw === "review" ? raw : "all";
  const requested = params.get("hit");
  const queue = useQueue();
  const decision = useDecision();
  // Items the server confirmed as decided, hidden until the reloaded queue drops them.
  const [hidden, setHidden] = useState<ReadonlySet<string>>(() => new Set());
  const [selectedId, setSelectedId] = useState<string | null>(requested);
  const [position, setPosition] = useState(0);
  const [note, setNote] = useState("");
  const [noteHelp, setNoteHelp] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const [conflict, setConflict] = useState<string | null>(null);
  const pending = useRef(false);
  const noteRef = useRef<HTMLTextAreaElement>(null);
  const detailRef = useRef<HTMLElement>(null);
  const shortcut = useRef<(key: string) => boolean>(() => false);
  const headingId = useId();
  const noteId = useId();
  const helpId = useId();

  const all = useMemo(
    () => (queue.data?.items ?? []).filter((item) => !hidden.has(item.id)),
    [queue.data, hidden],
  );
  const items = useMemo(
    () => (filter === "all" ? all : all.filter((item) => item.bucket === filter)),
    [all, filter],
  );
  const found = items.findIndex((item) => item.id === selectedId);
  const index = found >= 0 ? found : Math.min(position, items.length - 1);
  const current: HitSummary | null = items[index] ?? null;
  const hit = useHit(current?.id ?? null);
  const detail = current && hit.data?.id === current.id ? hit.data : undefined;
  const sourceUrl = safeHttpUrl(detail?.source_url);
  const canDecide = detail?.status === "open" && !decision.isPending;
  const failure =
    decision.error instanceof ApiError && decision.error.status === 409 ? null : decision.error;
  const requestedMissing = Boolean(
    requested && queue.data && !queue.data.items.some((item) => item.id === requested),
  );

  const currentId = current?.id;
  const resetDecision = decision.reset;
  useEffect(() => {
    setNote("");
    setNoteHelp(null);
    resetDecision();
  }, [currentId, resetDecision]);

  function select(next: number) {
    const item = items[next];
    if (!item || pending.current) return;
    setSelectedId(item.id);
    setPosition(next);
    setConflict(null);
  }
  function move(step: number) {
    if (!items.length || pending.current) return;
    const next = index + step;
    if (next < 0 || next >= items.length) {
      setMessage(
        step > 0 ? "This is the last item in this view." : "This is the first item in this view.",
      );
      return;
    }
    setMessage("");
    select(next);
  }
  function changeFilter(value: string) {
    const next = new URLSearchParams(params);
    if (value === "all") next.delete("bucket");
    else next.set("bucket", value);
    next.delete("hit");
    setParams(next);
    setSelectedId(null);
    setPosition(0);
    setMessage("");
  }
  function openSource() {
    if (!detail) return;
    if (sourceUrl) window.open(sourceUrl, "_blank", "noopener,noreferrer");
    else setMessage("No source link is available for this listing.");
  }
  function decide(kind: DecisionKind) {
    if (!detail || detail.status !== "open" || pending.current) return;
    const text = note.trim();
    if (kind !== "false_positive" && !text) {
      setNoteHelp(NOTE_REQUIRED[kind]);
      noteRef.current?.focus();
      return;
    }
    pending.current = true;
    setConflict(null);
    setMessage("");
    const at = index;
    const name = detail.party_name ?? "this record";
    const linked = params.has("hit");
    decision.mutate(
      { hitId: detail.id, kind, note: text },
      {
        onSuccess: (result) => {
          setHidden((previous) => new Set(previous).add(result.hit_id));
          setSelectedId(null);
          setPosition(at);
          const count = result.observations_dispositioned;
          setMessage(
            `Recorded “${kindLabel(result.disposition.kind)}” for ${name}${
              count > 1 ? ` across ${formatCount(count)} observations` : ""
            }.`,
          );
          if (linked) {
            const next = new URLSearchParams(params);
            next.delete("hit");
            setParams(next, { replace: true });
          }
          detailRef.current?.focus();
        },
        onError: (error) => {
          if (error instanceof ApiError && error.status === 409) setConflict(error.message);
        },
        onSettled: () => {
          pending.current = false;
        },
      },
    );
  }

  useEffect(() => {
    shortcut.current = (key) => {
      const actions: Record<string, () => void> = {
        j: () => move(1),
        k: () => move(-1),
        f: () => decide("false_positive"),
        m: () => decide("confirmed_match"),
        e: () => decide("escalated"),
        v: openSource,
      };
      actions[key]?.();
      return key in actions;
    };
  });
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (
        event.defaultPrevented ||
        event.repeat ||
        event.ctrlKey ||
        event.metaKey ||
        event.altKey ||
        isTyping(event.target)
      )
        return;
      if (shortcut.current(event.key.toLowerCase())) event.preventDefault();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  let body: ReactNode;
  if (queue.isPending)
    body = (
      <p className="panel rv-empty" role="status">
        Loading the review queue…
      </p>
    );
  else if (!queue.data)
    body = (
      <LoadError
        title="We couldn’t load the review queue."
        error={queue.error}
        onRetry={() => void queue.refetch()}
        retrying={queue.isFetching}
      />
    );
  else if (!current)
    body = (
      <section className="panel rv-empty">
        {filter !== "all" && all.length > 0 ? (
          <>
            <h2>No {filter === "likely" ? "likely matches" : "items needing review"} are open.</h2>
            <p>
              {formatCount(all.length)} other open {all.length === 1 ? "item is" : "items are"} in
              the queue.
            </p>
            <button type="button" className="button secondary" onClick={() => changeFilter("all")}>
              Show all open items
            </button>
          </>
        ) : (
          <>
            <h2>There are no open items in the review queue.</h2>
            <p>
              New comparisons appear here after screening runs. Every decision stays in each
              party’s file.
            </p>
          </>
        )}
      </section>
    );
  else
    body = (
      <div className="rv-layout">
        <nav className="panel rv-list" aria-label="Open items">
          <ol>
            {items.map((item, i) => (
              <li key={item.id}>
                <button
                  type="button"
                  className="rv-item"
                  aria-current={i === index ? "true" : undefined}
                  onClick={() => select(i)}
                >
                  <span className="rv-item-names">
                    <strong>{item.party_name ?? "Unnamed record"}</strong>
                    <span>{item.entry_name ?? "Unnamed listing"}</span>
                  </span>
                  <span className="rv-item-meta">
                    <span className={`badge rv-${item.bucket}`}>
                      {bucketLabel(item.bucket)} · {formatScore(item.final_confidence)}
                    </span>
                    {item.source_list && <span className="badge">{item.source_list}</span>}
                    <time dateTime={item.created_at}>{age(item.created_at)}</time>
                  </span>
                </button>
              </li>
            ))}
          </ol>
        </nav>
        <section
          className="panel rv-detail"
          aria-labelledby={headingId}
          tabIndex={-1}
          ref={detailRef}
        >
          <h2 id={headingId}>
            {current.party_name ?? "Unnamed record"}{" "}
            <span className="rv-vs">compared with</span>{" "}
            {current.entry_name ?? "Unnamed listing"}
          </h2>
          <p className="rv-muted">
            {current.source_list ? `Listed on ${current.source_list}. ` : ""}Compared{" "}
            <LocalTime value={current.created_at} />.
          </p>
          {hit.isPending ? (
            <p className="rv-muted" role="status">
              Loading the comparison…
            </p>
          ) : hit.isError ? (
            <LoadError
              title="We couldn’t load this comparison."
              error={hit.error}
              onRetry={() => void hit.refetch()}
              retrying={hit.isFetching}
            />
          ) : detail ? (
            <HitEvidence hit={detail} />
          ) : null}
          {detail && detail.status !== "open" && (
            <p className="notice rv-info">
              This comparison is no longer open. The queue will update when it reloads.
            </p>
          )}
          <form
            className="rv-decide"
            aria-label="Record a decision"
            onSubmit={(event) => event.preventDefault()}
          >
            <label htmlFor={noteId}>Decision note</label>
            <p id={helpId} className={noteHelp ? "rv-note-help" : "field-help"} aria-live="polite">
              {noteHelp ??
                "Optional when clearing a false positive. Required to confirm a match or escalate."}
            </p>
            <textarea
              id={noteId}
              ref={noteRef}
              rows={3}
              maxLength={10000}
              value={note}
              readOnly={decision.isPending}
              aria-describedby={helpId}
              aria-invalid={noteHelp ? true : undefined}
              onChange={(event) => {
                setNote(event.target.value);
                if (event.target.value.trim()) setNoteHelp(null);
              }}
            />
            <div className="rv-actions">
              {DECISIONS.map(({ kind, key, label, tone }) => (
                <button
                  key={kind}
                  type="button"
                  className={`button ${tone}`}
                  aria-keyshortcuts={key}
                  disabled={!canDecide || (kind !== "false_positive" && !note.trim())}
                  onClick={() => decide(kind)}
                >
                  {label} <kbd aria-hidden="true">{key}</kbd>
                </button>
              ))}
              {sourceUrl ? (
                <a
                  className="button text"
                  href={sourceUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  aria-keyshortcuts="V"
                >
                  View source <kbd aria-hidden="true">V</kbd>
                </a>
              ) : detail ? (
                <span className="rv-muted">No source link for this listing</span>
              ) : null}
            </div>
            {decision.isPending && (
              <p className="rv-muted" role="status">
                Recording the decision…
              </p>
            )}
            {failure && (
              <div className="notice danger" role="alert">
                {failure.message}
              </div>
            )}
          </form>
        </section>
      </div>
    );

  return (
    <>
      <header className="page-header">
        <div>
          <div className="eyebrow">Review</div>
          <h1>Review queue</h1>
          <p className="lede">
            {queue.data
              ? `${formatCount(queue.data.total)} open ${queue.data.total === 1 ? "item" : "items"}, likely matches first. Each compares one of your records with one listing.`
              : "Open comparisons between your records and the screening list."}
          </p>
        </div>
        <label className="rv-filter">
          Show
          <select value={filter} onChange={(event) => changeFilter(event.target.value)}>
            {FILTERS.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
      </header>
      <p className="rv-keys">
        Keyboard: <kbd>J</kbd> next, <kbd>K</kbd> previous, <kbd>F</kbd> clear, <kbd>M</kbd>{" "}
        confirm, <kbd>E</kbd> escalate, <kbd>V</kbd> view source. Shortcuts pause while you type.
      </p>
      <p className="rv-live" role="status">
        {message}
      </p>
      {conflict && (
        <div className="notice danger" role="alert">
          <strong>This item changed.</strong> {conflict} No decision was recorded, and the queue
          has been reloaded.
        </div>
      )}
      {requestedMissing && (
        <p className="notice rv-info">
          The linked comparison is not open in the current queue. It may already have a decision
          or have been replaced by a newer comparison; its party file keeps the full history.
        </p>
      )}
      {queue.isError && queue.data && (
        <LoadError
          title="The queue could not be refreshed."
          error={queue.error}
          onRetry={() => void queue.refetch()}
          retrying={queue.isFetching}
        />
      )}
      {body}
    </>
  );
}
