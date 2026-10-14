import {
  useEffect,
  useId,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, type Session } from "./api";
import { canAdminister, messageOf } from "./adminApi";
import { LocalTime } from "./CSLPanel";
import { sourceKeys, type Source } from "./sourceApi";
import { qboKeys as appQboKeys, verifiedAuthorizationUrl } from "./qboApi";

// Private to this bundle so it doesn't depend on the separately authored QBO settings API.
type QboEnvironment = "sandbox" | "production";
type ConnectionStatus =
  | "configured"
  | "connected"
  | "reconnect_required"
  | "disconnecting"
  | "disconnected";
type SyncMode = "incremental" | "full";

type QboSource = Source & {
  environment?: QboEnvironment;
  connection_status?: ConnectionStatus | null;
};
type QboConnection = {
  source_id: string;
  environment: QboEnvironment;
  status: ConnectionStatus;
  realm_id: string | null;
  access_expires_at: string | null;
  refresh_expires_at: string | null;
  webhooks_enabled: boolean;
  revocation_status: null | "pending" | "revoked" | "failed" | "not_required";
  revocation_error_code: string | null;
};
type EnvironmentFlags = {
  client_id_configured: boolean;
  client_secret_configured: boolean;
  verifier_token_configured: boolean;
  configured: boolean;
};
type QboSettings = {
  sandbox: EnvironmentFlags;
  production: EnvironmentFlags;
  redirect_uri: string | null;
  webhook_urls: { sandbox: string | null; production: string | null };
};
type OAuthStart = { authorization_url: string; expires_at: string };
type QueuedAttempt = { attempt_id: string; operation: string; status: "queued" };
type DisconnectAttempt = QueuedAttempt & { connection?: QboConnection };
type Operation = {
  id: string;
  attempt_id?: string;
  operation: string;
  source_id: string | null;
  party_id: string | null;
  status: string;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  error_code: string | null;
  outcome: unknown;
};
type Attempt =
  | { id: string; kind: "sync"; mode: SyncMode }
  | { id: string; kind: "disconnect" };
type Outcome = Record<string, unknown>;

const qboKeys = {
  settings: appQboKeys.settings,
  // Under the sources family so source refreshes also reload the connection.
  connection: (id: string) => [...sourceKeys.all, "qbo", id, "connection"] as const,
  // Outside every refreshed family: only polling or an explicit retry reads it.
  operation: (id: string) => ["qbo-operation", id] as const,
};

// Query families (owned by other packages) whose figures a sync or disconnect can change.
const OPERATION_DEPENDENT = new Set([
  "sources",
  "monitoring",
  "dashboard",
  "overview",
  "review",
  "queue",
  "parties",
  "party",
  "runs",
  "audit",
  "csl",
]);

const POLL_MS = 1500;
const MAX_LABEL = 200;
const isActive = (status: string | undefined) =>
  status === "queued" || status === "running";
const enc = encodeURIComponent;
const formatNumber = (value: number) => value.toLocaleString();

const environmentLabels: Record<QboEnvironment, string> = {
  sandbox: "Sandbox",
  production: "Production",
};

type StatusInfo = { label: string; tone: string; detail: string };
const statusInfo: Record<ConnectionStatus, StatusInfo> = {
  configured: {
    label: "Not connected",
    tone: "",
    detail:
      "No QuickBooks company is connected to this source yet.",
  },
  connected: {
    label: "Connected",
    tone: "success",
    detail: "Scheduled polling reads customers and vendors from this company.",
  },
  reconnect_required: {
    label: "Reconnect required",
    tone: "source-error",
    detail:
      "QuickBooks no longer accepts this source’s authorization. Polling is paused until an Admin reconnects.",
  },
  disconnecting: {
    label: "Disconnecting",
    tone: "source-stale",
    detail: "Polling has stopped. The server is finishing the disconnect.",
  },
  disconnected: {
    label: "Disconnected",
    tone: "",
    detail:
      "Polling has stopped. Parties, screening history and audit records from this source are kept.",
  },
};

function describeStatus(status: string): StatusInfo {
  return (
    (statusInfo as Record<string, StatusInfo | undefined>)[status] ?? {
      label: `Unknown status (${status})`,
      tone: "source-stale",
      detail:
        "The server reported a status this page doesn’t recognize, so no actions are offered.",
    }
  );
}

function problem(error: unknown, fallback: string) {
  if (error instanceof ApiError && error.status === 403)
    return "Only active Admins can manage QuickBooks connections. Sign in again if your role changed.";
  return messageOf(error, fallback);
}

// Only a 4xx response proves the server refused before changing anything.
const refused = (error: unknown) =>
  error instanceof ApiError && error.status >= 400 && error.status < 500;

const outcomeOf = (value: unknown): Outcome =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Outcome)
    : {};

function numberOf(outcome: Outcome, key: string): number | null {
  const value = outcome[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function useQboSettings(enabled: boolean) {
  return useQuery({
    queryKey: qboKeys.settings,
    queryFn: () => api<QboSettings>("/settings/connectors/qbo"),
    enabled,
    retry: false,
  });
}

function useCreateQboSource() {
  const client = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: (input: { label: string; environment: QboEnvironment }) =>
      api<QboSource>("/sources", {
        method: "POST",
        body: JSON.stringify({
          kind: "qbo",
          label: input.label,
          environment: input.environment,
        }),
      }),
    onSuccess: (source) => {
      client.setQueryData<Source[]>(sourceKeys.list, (list) =>
        list
          ? list.some((item) => item.id === source.id)
            ? list.map((item) => (item.id === source.id ? source : item))
            : [...list, source]
          : list,
      );
    },
    onSettled: () => {
      void client.invalidateQueries({ queryKey: sourceKeys.all });
    },
  });
}

/** Polls an attempt until it settles; a read error stops polling until an explicit retry. */
function useOperation(id: string | null) {
  const client = useQueryClient();
  const query = useQuery({
    queryKey: qboKeys.operation(id ?? ""),
    queryFn: () =>
      api<Operation>(`/monitoring/operations/${enc(id ?? "")}`),
    enabled: id !== null,
    retry: false,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    refetchInterval: (q) =>
      q.state.status !== "error" &&
      (!q.state.data || isActive(q.state.data.status))
        ? POLL_MS
        : false,
  });
  const settled = query.data !== undefined && !isActive(query.data.status);
  const uncertain = query.isError;
  useEffect(() => {
    if (id === null || !(settled || uncertain)) return;
    void client.invalidateQueries({
      predicate: (q) => OPERATION_DEPENDENT.has(String(q.queryKey[0])),
    });
  }, [client, id, settled, uncertain]);
  return query;
}

export function AddQboSource({ session }: { session: Session }) {
  const admin = canAdminister(session);
  const titleId = useId();
  const nameId = useId();
  const environmentId = useId();
  const [label, setLabel] = useState("");
  const [environment, setEnvironment] = useState<QboEnvironment>("sandbox");
  const settings = useQboSettings(admin);
  const create = useCreateQboSource();
  if (!admin) return null;

  const trimmed = label.trim().slice(0, MAX_LABEL);
  const ready = settings.isSuccess && settings.data?.[environment]?.configured === true;
  const blocked = settings.isSuccess && !ready;
  const environmentLabel = environmentLabels[environment];

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!trimmed || !ready || create.isPending) return;
    create.mutate(
      { label: trimmed, environment },
      { onSuccess: () => setLabel("") },
    );
  }

  return (
    <section className="panel source-add" aria-labelledby={titleId}>
      <h2 id={titleId}>Add a QuickBooks source</h2>
      <p className="source-muted">
        Each QuickBooks source connects to exactly one company. Add the source
        here, then use Connect to QuickBooks on its card to sign in at Intuit.
      </p>
      <form onSubmit={submit}>
        <div className="source-add-row">
          <div className="source-field">
            <label htmlFor={nameId}>Source name</label>
            <input
              id={nameId}
              value={label}
              onChange={(event) => setLabel(event.target.value)}
              required
              maxLength={MAX_LABEL}
              autoComplete="off"
              aria-describedby={`${nameId}-help`}
            />
          </div>
          <div className="source-field">
            <label htmlFor={environmentId}>QuickBooks environment</label>
            <select
              id={environmentId}
              value={environment}
              onChange={(event) =>
                setEnvironment(event.target.value as QboEnvironment)
              }
            >
              <option value="sandbox">Sandbox (Intuit test company)</option>
              <option value="production">Production</option>
            </select>
          </div>
          <button
            type="submit"
            className="button primary"
            disabled={!trimmed || !ready || create.isPending}
          >
            {create.isPending ? "Adding…" : "Add QuickBooks source"}
          </button>
        </div>
        <span id={`${nameId}-help`} className="field-help">
          For example, “Main company books”. A different company needs its
          own source.
        </span>
      </form>
      {blocked && (
        <div className="notice source-warning">
          <p>
            The {environmentLabel} Intuit app isn’t configured yet, so a{" "}
            {environmentLabel} source can’t be added or connected.{" "}
            <a className="text-link" href="/settings">
              Configure your Intuit app in Settings
            </a>
            .
          </p>
        </div>
      )}
      {settings.isError && (
        <div className="notice danger" role="alert">
          <p>
            <strong>We couldn’t check the QuickBooks app settings.</strong>{" "}
            {problem(settings.error, "Please try again.")}
          </p>
          <p>Adding a QuickBooks source is unavailable until they load.</p>
          <button
            type="button"
            className="button secondary"
            onClick={() => void settings.refetch()}
            disabled={settings.isFetching}
          >
            {settings.isFetching ? "Trying again…" : "Try again"}
          </button>
        </div>
      )}
      <div role="status">
        {settings.isPending && (
          <p className="source-muted">Checking the QuickBooks app settings…</p>
        )}
        {create.isSuccess && (
          <div className="notice source-applied source-outcome">
            <p>
              <strong>
                “{create.data.label}” was added but is not connected yet.
              </strong>{" "}
              Use Connect to QuickBooks on its card to sign in to the company.
              Nothing is imported until a company is connected and synced.
            </p>
          </div>
        )}
      </div>
      {create.isError && (
        <div className="notice danger" role="alert">
          <p>
            <strong>{refused(create.error) ? "The source was not added." : "The add-source request could not be confirmed. Check your sources before trying again."}</strong>{" "}
            {problem(create.error, "Please try again.")}
          </p>
        </div>
      )}
    </section>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

const COUNT_LABELS: [string, string][] = [
  ["added", "added"],
  ["changed", "changed"],
  ["deleted", "deleted (marked inactive)"],
  ["unchanged", "unchanged"],
];

// Only counts the server actually supplied; nothing is inferred or defaulted.
function SyncCounts({ outcome }: { outcome: Outcome }) {
  const counts = COUNT_LABELS.flatMap(([key, label]) => {
    const value = numberOf(outcome, key);
    return value === null ? [] : [{ key, label, value }];
  });
  const total = numberOf(outcome, "record_count");
  if (counts.length === 0 && total === null)
    return <p>The server didn’t report record counts for this sync.</p>;
  return (
    <>
      {counts.length > 0 && (
        <ul className="source-counts">
          {counts.map(({ key, label, value }) => (
            <li key={key}>
              <strong>{formatNumber(value)}</strong> {label}
            </li>
          ))}
        </ul>
      )}
      {total !== null && (
        <p>
          This source now has {formatNumber(total)} active{" "}
          {total === 1 ? "record" : "records"}.
        </p>
      )}
    </>
  );
}

function ScreeningOutcome({ outcome }: { outcome: Outcome }) {
  const status =
    typeof outcome.screening_status === "string"
      ? outcome.screening_status
      : null;
  const failed = Array.isArray(outcome.failed_task_ids)
    ? outcome.failed_task_ids.length
    : 0;
  const runs = Array.isArray(outcome.run_ids) ? outcome.run_ids.length : 0;
  const pendingTask =
    typeof outcome.pending_task_id === "string" && outcome.pending_task_id !== "";
  if (
    failed > 0 ||
    status === "failed" ||
    status === "error" ||
    status === "interrupted"
  )
    return (
      <div className="notice danger">
        <p>
          <strong>The records were saved, but screening needs attention.</strong>{" "}
          {failed > 0
            ? `${formatNumber(failed)} screening ${failed === 1 ? "task" : "tasks"} failed.`
            : `Screening ended with status “${status}”.`}{" "}
          Completed runs and saved party records are retained.
        </p>
      </div>
    );
  if (status === "success")
    return (
      <p>
        Screening finished for new and changed parties
        {runs > 0 &&
          ` and recorded ${formatNumber(runs)} ${runs === 1 ? "run" : "runs"}`}
        .
      </p>
    );
  if (
    pendingTask ||
    status === "queued" ||
    status === "running" ||
    status === "pending"
  )
    return (
      <p>
        Screening of new and changed parties is queued. Results aren’t complete
        until it finishes.
      </p>
    );
  if (status) return <p>Screening status: “{status}”.</p>;
  return null;
}

function attemptName(attempt: Attempt) {
  if (attempt.kind === "disconnect") return "The disconnect";
  return attempt.mode === "full" ? "The full sync" : "The incremental sync";
}

function OperationProgress({
  attempt,
  data,
  uncertain,
  connection,
}: {
  attempt: Attempt;
  data: Operation | undefined;
  uncertain: boolean;
  connection: QboConnection | undefined;
}) {
  const name = attemptName(attempt);
  // Before the first read, the accepted request itself reported "queued".
  const status = data?.status ?? "queued";
  if (isActive(status)) {
    if (uncertain)
      return (
        <p className="source-muted">
          {name} was last reported as “{status}”. Its current status couldn’t
          be checked.
        </p>
      );
    return (
      <p className="source-progress">
        {status === "queued"
          ? `${name} is queued. The server hasn’t started it yet.`
          : `${name} is running. This page checks for progress every few seconds.`}
        {attempt.kind === "disconnect" &&
          " Scheduled polling for this source has already stopped."}
      </p>
    );
  }
  const outcome = outcomeOf(data?.outcome);
  if (status === "success") {
    if (attempt.kind === "disconnect")
      return (
        <div className="notice source-applied source-outcome">
          <p>
            <strong>The disconnect finished.</strong> This source is no longer
            polled. Its parties, screening history and audit records are kept.
          </p>
        </div>
      );
    return (
      <div className="notice source-applied source-outcome">
        <p>
          <strong>{name} finished.</strong>
        </p>
        <SyncCounts outcome={outcome} />
        <ScreeningOutcome outcome={outcome} />
      </div>
    );
  }
  const code = data?.error_code ? ` Error code: ${data.error_code}.` : "";
  if (status === "skipped")
    return (
      <div className="notice source-warning">
        <p>
          <strong>The server skipped this request.</strong>
          {code}
        </p>
      </div>
    );
  return (
    <div className="notice danger source-outcome">
      <p>
        <strong>
          {name} ended with status “{status}”.
        </strong>
        {code}
      </p>
      {attempt.kind === "sync" ? (
        <>
          <p>Record counts on this source come from the server and refresh automatically; anything it saved before stopping is reflected there.</p>
          {COUNT_LABELS.some(([key]) => numberOf(outcome, key) !== null) && (
            <><p>The server reported these applied record counts before the operation stopped.</p><SyncCounts outcome={outcome} /><ScreeningOutcome outcome={outcome} /></>
          )}
        </>
      ) : connection?.status === "disconnected" ? (
        <p>This source is still disconnected here and won’t be polled.</p>
      ) : (
        <p>Check the connection status above before trying again.</p>
      )}
    </div>
  );
}

function RevocationNotice({ connection }: { connection: QboConnection }) {
  const { revocation_status: status, revocation_error_code: code } = connection;
  if (status === null || status === undefined) return null;
  if (status === "pending")
    return (
      <div className="notice source-warning">
        <p>Revoking this authorization with Intuit is still pending.</p>
      </div>
    );
  if (status === "not_required")
    return <p className="source-muted">No stored QuickBooks authorization needed revocation.</p>;
  if (status === "revoked")
    return (
      <div className="notice source-warning">
        <p>Intuit confirmed the authorization was revoked.</p>
      </div>
    );
  if (status === "failed")
    return (
      <div className="notice danger">
        <p>
          <strong>Intuit did not confirm revoking the authorization.</strong>
          {code && ` Error code: ${code}.`}
        </p>
        <p>
          {connection.status === "disconnected" &&
            "This source is still disconnected here, so it isn’t polled. "}
          You can also remove this app’s access from the company’s QuickBooks
          settings.
        </p>
      </div>
    );
  return (
    <div className="notice source-warning">
      <p>Revocation status: “{String(status)}”.</p>
    </div>
  );
}

function RequestError({
  error,
  refusedTitle,
  uncertainTitle,
}: {
  error: unknown;
  refusedTitle: string;
  uncertainTitle: string;
}) {
  const wasRefused = refused(error);
  return (
    <div className="notice danger source-outcome" role="alert">
      <p>
        <strong>{wasRefused ? refusedTitle : uncertainTitle}</strong>{" "}
        {problem(error, "Please try again.")}
      </p>
      {!wasRefused && (
        <p>The connection status is being rechecked. Review it before trying again.</p>
      )}
    </div>
  );
}

export function QboSourceControls({
  session,
  source,
}: {
  session: Session;
  source: Source;
}) {
  const admin = canAdminister(session) && source.kind === "qbo";
  const titleId = useId();
  const webhookId = useId();
  const client = useQueryClient();
  const connectionKey = qboKeys.connection(source.id);
  const settings = useQboSettings(admin);
  const connection = useQuery({
    queryKey: connectionKey,
    queryFn: () =>
      api<QboConnection>(`/sources/${enc(source.id)}/qbo/connection`),
    enabled: admin,
    retry: false,
  });
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [redirecting, setRedirecting] = useState(false);
  const [badUrl, setBadUrl] = useState(false);
  const operation = useOperation(attempt?.id ?? null);

  // Returning from Intuit through the back/forward cache restores this page.
  useEffect(() => {
    const restore = (event: PageTransitionEvent) => {
      if (event.persisted) setRedirecting(false);
    };
    window.addEventListener("pageshow", restore);
    return () => window.removeEventListener("pageshow", restore);
  }, []);

  function refreshConnection() {
    void client.invalidateQueries({ queryKey: connectionKey });
  }

  const start = useMutation({
    gcTime: 0,
    retry: false,
    mutationFn: () =>
      api<OAuthStart>(`/sources/${enc(source.id)}/qbo/oauth/start`, {
        method: "POST",
        body: "{}",
      }),
    onError: refreshConnection,
  });
  const sync = useMutation({
    retry: false,
    mutationFn: (mode: SyncMode) =>
      api<QueuedAttempt>(`/sources/${enc(source.id)}/sync`, {
        method: "POST",
        body: JSON.stringify({ mode }),
      }),
    onSuccess: (queued, mode) =>
      setAttempt({ id: queued.attempt_id, kind: "sync", mode }),
    onError: refreshConnection,
  });
  const disconnect = useMutation({
    retry: false,
    mutationFn: () =>
      api<DisconnectAttempt>(`/sources/${enc(source.id)}/qbo/disconnect`, {
        method: "POST",
        body: "{}",
      }),
    onSuccess: (queued) => {
      if (queued.connection)
        client.setQueryData(connectionKey, queued.connection);
      else refreshConnection();
      setAttempt({ id: queued.attempt_id, kind: "disconnect" });
      setConfirming(false);
      // Polling stops at once, so the card's next sync changes now.
      void client.invalidateQueries({ queryKey: sourceKeys.list });
    },
    onError: refreshConnection,
  });
  const webhook = useMutation({
    retry: false,
    mutationFn: (enabled: boolean) =>
      api<QboConnection>(`/sources/${enc(source.id)}/qbo/connection`, {
        method: "PATCH",
        body: JSON.stringify({ webhooks_enabled: enabled }),
      }),
    onSuccess: (saved) => client.setQueryData(connectionKey, saved),
    // The checkbox always reflects the server, so reload its real value.
    onError: refreshConnection,
  });

  if (!admin) return null;

  function clearNotices() {
    start.reset();
    sync.reset();
    disconnect.reset();
    webhook.reset();
    setBadUrl(false);
  }

  function connect() {
    clearNotices();
    start.mutate(undefined, {
      onSuccess: (result) => {
        const url = verifiedAuthorizationUrl(result.authorization_url);
        start.reset();
        if (!url) {
          setBadUrl(true);
          return;
        }
        setRedirecting(true);
        window.location.assign(url);
      },
    });
  }

  function runSync(mode: SyncMode) {
    clearNotices();
    sync.mutate(mode);
  }

  const data = connection.data;
  const status = data?.status;
  const known = data !== undefined && !connection.isError;
  const opPending =
    attempt !== null &&
    (operation.isError || !operation.data || isActive(operation.data.status));
  const busy =
    redirecting ||
    start.isPending ||
    sync.isPending ||
    disconnect.isPending ||
    webhook.isPending ||
    opPending;
  const reconnect =
    (status === "reconnect_required" || status === "disconnected") &&
    Boolean(data?.realm_id);
  const appReady = settings.isSuccess && data !== undefined && settings.data?.[data.environment]?.configured === true;
  const canConnect =
    known && appReady &&
    (status === "configured" ||
      status === "reconnect_required" ||
      status === "disconnected");
  const canSync = known && status === "connected";
  const canDisconnect =
    known && (status === "connected" || status === "reconnect_required" || ((status === "disconnected" || status === "configured") && data?.revocation_status === "failed"));
  const info = data ? describeStatus(data.status) : null;

  return (
    <section className="admin-subsection" aria-labelledby={titleId}>
      <h4 id={titleId}>QuickBooks connection</h4>
      {connection.isPending && (
        <p className="source-muted" role="status">
          Checking the QuickBooks connection…
        </p>
      )}
      {connection.isError && (
        <div className="notice danger" role="alert">
          <p>
            <strong>We couldn’t load the QuickBooks connection.</strong>{" "}
            {problem(connection.error, "Please try again.")}
          </p>
          <p>
            {data &&
              "The details below are from the last successful check. "}
            Actions are unavailable until the current state loads.
          </p>
          <button
            type="button"
            className="button secondary"
            onClick={() => void connection.refetch()}
            disabled={connection.isFetching}
          >
            {connection.isFetching ? "Checking…" : "Check connection again"}
          </button>
        </div>
      )}
      {data && info && (
        <>
          <p>
            <span className={`badge ${info.tone}`}>{info.label}</span>
          </p>
          <p className="source-muted">{info.detail}</p>
          <dl className="source-facts">
            <Fact label="Environment">
              {environmentLabels[data.environment] ?? data.environment}
            </Fact>
            <Fact label="Company ID">
              {data.realm_id ?? "No company connected"}
            </Fact>
            {data.access_expires_at && (
              <Fact label="Access renews by">
                <LocalTime value={data.access_expires_at} />
              </Fact>
            )}
            {data.refresh_expires_at && (
              <Fact label="Reconnect needed by">
                <LocalTime value={data.refresh_expires_at} />
              </Fact>
            )}
            <Fact label="Webhooks">{data.webhooks_enabled ? "On" : "Off"}</Fact>
          </dl>
          <RevocationNotice connection={data} />
        </>
      )}
      {known && !appReady && status !== "connected" && status !== "disconnecting" && (
        <p className="source-muted">App configuration could not be confirmed for this environment. <a className="text-link" href="/settings">Check QuickBooks app settings</a> before connecting.</p>
      )}
      {canConnect && (
        <p className="source-muted">
          {reconnect
            ? `Sign in to the same QuickBooks company (company ID ${data?.realm_id}). A different company can’t be connected to this source; add a new QuickBooks source for it.`
            : "You’ll sign in at Intuit and choose one company. Each source connects to exactly one company."}
        </p>
      )}
      {(canConnect || canSync || canDisconnect) && (
        <div className="admin-actions">
          {canConnect && (
            <button
              type="button"
              className="button primary"
              onClick={connect}
              disabled={busy}
            >
              {reconnect ? "Reconnect to QuickBooks" : "Connect to QuickBooks"}
            </button>
          )}
          {canSync && (
            <>
              <button
                type="button"
                className="button primary"
                onClick={() => runSync("incremental")}
                disabled={busy}
              >
                Sync changes
              </button>
              <button
                type="button"
                className="button secondary"
                onClick={() => runSync("full")}
                disabled={busy}
              >
                Full sync
              </button>
            </>
          )}
          {canDisconnect && !confirming && (
            <button
              type="button"
              className="button secondary"
              onClick={() => {
                clearNotices();
                setConfirming(true);
              }}
              disabled={busy}
            >
              {data?.revocation_status === "failed" && (status === "disconnected" || status === "configured") ? "Retry Intuit revocation" : "Disconnect"}
            </button>
          )}
        </div>
      )}
      {redirecting && (
        <p className="source-progress">Opening QuickBooks sign-in…</p>
      )}
      {canSync && (
        <p className="source-muted">
          Sync changes reads what changed since the last sync. Full sync
          re-reads every customer and vendor. Scheduled polling also runs on
          its own.
        </p>
      )}
      {confirming && canDisconnect && (
        <div
          className="notice source-warning"
          role="group"
          aria-label="Confirm disconnect"
        >
          <p>
            <strong>Disconnect this QuickBooks company?</strong>
          </p>
          <p>
            Scheduled polling stops immediately. Parties, screening history
            and audit records from this source are kept. Revoking the
            authorization with Intuit is attempted separately, and its actual
            outcome is shown here.
          </p>
          <div className="admin-actions">
            <button
              type="button"
              className="button primary"
              onClick={() => disconnect.mutate()}
              disabled={busy}
            >
              Confirm disconnect
            </button>
            <button
              type="button"
              className="button secondary"
              onClick={() => setConfirming(false)}
              disabled={disconnect.isPending}
            >
              {status === "disconnected" || status === "configured" ? "Cancel retry" : "Keep connected"}
            </button>
          </div>
        </div>
      )}
      {known && status === "connected" && data && (
        <div className="admin-form">
          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={data.webhooks_enabled}
              disabled={busy}
              aria-describedby={`${webhookId}-help`}
              onChange={(event) => {
                clearNotices();
                webhook.mutate(event.target.checked);
              }}
            />
            <span>Receive QuickBooks change notifications (webhooks)</span>
          </label>
          <span id={`${webhookId}-help`} className="field-help">
            Optional. Enabling needs a webhook verifier token for this
            environment in Settings and a public HTTPS address; the server
            checks both. Scheduled polling continues either way.
          </span>
        </div>
      )}
      <div role="status" aria-label="QuickBooks operation status">
        {webhook.isPending && (
          <p className="source-muted">Saving the notification setting…</p>
        )}
        {attempt && (
          <OperationProgress
            attempt={attempt}
            data={operation.data}
            uncertain={operation.isError}
            connection={data}
          />
        )}
      </div>
      {attempt && operation.isError && (
        <div className="notice danger" role="alert">
          <p>
            <strong>We couldn’t check the operation’s status.</strong>{" "}
            {problem(operation.error, "Please try again.")}
          </p>
          <p>
            Automatic checks have stopped, and the operation may still be
            running on the server. Other QuickBooks actions are paused until
            its status is known.
          </p>
          <button
            type="button"
            className="button secondary"
            onClick={() => void operation.refetch()}
            disabled={operation.isFetching}
          >
            {operation.isFetching ? "Checking…" : "Check status again"}
          </button>
        </div>
      )}
      {badUrl && (
        <div className="notice danger" role="alert">
          <p>
            <strong>QuickBooks sign-in wasn’t opened.</strong> The server
            returned a sign-in address that isn’t Intuit’s authorization page,
            so it was not opened. Ask your administrator to check the
            connector configuration.
          </p>
        </div>
      )}
      {start.isError && (
        <RequestError
          error={start.error}
          refusedTitle="QuickBooks sign-in couldn’t start."
          uncertainTitle="QuickBooks sign-in couldn’t start."
        />
      )}
      {sync.isError && (
        <RequestError
          error={sync.error}
          refusedTitle="The sync wasn’t queued."
          uncertainTitle="The sync request couldn’t be confirmed."
        />
      )}
      {disconnect.isError && (
        <RequestError
          error={disconnect.error}
          refusedTitle="The source wasn’t disconnected."
          uncertainTitle="The disconnect request couldn’t be confirmed."
        />
      )}
      {webhook.isError && (
        <div className="notice danger" role="alert">
          <p>
            <strong>{refused(webhook.error) ? "The notification setting wasn’t changed." : "The notification setting could not be confirmed."}</strong>{" "}
            {problem(webhook.error, "Please try again.")}
          </p>
          <p>
            The checkbox shows the server’s current setting. Scheduled polling
            continues.
          </p>
        </div>
      )}
    </section>
  );
}
