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
import {
  bcKeys,
  useBcSettings,
  type BcAcceptedAttempt,
  type BcAppSettings,
  type BcCompany,
  type BcConnection,
} from "./bcApi";

// Source and operation hooks are private to this bundle; the shared settings
// types and hook come from bcApi.
type BcSource = Source & { bc_environment?: string | null };
type SyncMode = "incremental" | "full";
type QueuedSync = { attempt_id: string; operation: string; status: "queued" };
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
  | { id: string; kind: "connect" }
  | { id: string; kind: "sync"; mode: SyncMode }
  | { id: string; kind: "subscriptions"; action: "webhooks" | "reconcile" }
  | { id: string; kind: "disconnect"; retry: boolean; accessOnly: boolean };
type Outcome = Record<string, unknown>;
type Subscription = BcConnection["subscriptions"][number];

const keys = {
  // Under the sources family so source refreshes also reload the connection.
  connection: (id: string) => [...sourceKeys.all, "bc", id, "connection"] as const,
  // Outside every refreshed family: only polling or an explicit retry reads it.
  operation: (id: string) => ["bc-operation", id] as const,
};

// Query families (owned by other packages) whose figures a BC operation can change.
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
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const ENVIRONMENT_PATTERN = /^[A-Za-z][A-Za-z0-9_-]{0,28}$/;
// Server details that look like addresses or credentials are never echoed.
const UNSAFE_DETAIL =
  /https?:\/\/|bearer\s|client[_ ]?secret|client[_ ]?state|access[_ ]?token|[A-Za-z0-9_\-.]{60,}/i;

const enc = encodeURIComponent;
const isActive = (status: string | undefined) =>
  status === "queued" || status === "running";
const formatNumber = (value: number) => value.toLocaleString();
const appConfigured = (settings: BcAppSettings | undefined) =>
  settings?.configured === true;

function tenantProblem(raw: string): string | null {
  const value = raw.trim();
  if (!value) return "Enter the tenant ID.";
  return UUID_PATTERN.test(value)
    ? null
    : "Enter the Directory (tenant) ID as a UUID in 8-4-4-4-12 hexadecimal form.";
}

function environmentProblem(raw: string): string | null {
  const value = raw.trim();
  if (!value) return "Enter the environment name.";
  if (value.length > 29) return "Environment names are at most 29 characters.";
  if (!/^[A-Za-z]/.test(value)) return "Environment names start with a letter.";
  return ENVIRONMENT_PATTERN.test(value)
    ? null
    : "Use only letters, digits, underscores and hyphens.";
}

function problem(error: unknown, fallback: string) {
  if (error instanceof ApiError) {
    if (error.status === 403)
      return "Only active Admins can manage Business Central connections. Sign in again if your role changed.";
    if (UNSAFE_DETAIL.test(error.message))
      return error.status === 409
        ? "This source is busy or its state changed. Check its status, then try again."
        : error.status === 422
          ? "The server didn’t accept this request for this source."
          : fallback;
  }
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

function useCreateBcSource() {
  const client = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: (input: {
      label: string;
      tenant_id: string;
      bc_environment: string;
    }) =>
      api<BcSource>("/sources", {
        method: "POST",
        body: JSON.stringify({ kind: "bc", ...input }),
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
    queryKey: keys.operation(id ?? ""),
    queryFn: () => api<Operation>(`/monitoring/operations/${enc(id ?? "")}`),
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

export function AddBcSource({ session }: { session: Session }) {
  const admin = canAdminister(session);
  const titleId = useId();
  const nameId = useId();
  const tenantId = useId();
  const environmentId = useId();
  const [label, setLabel] = useState("");
  const [tenant, setTenant] = useState("");
  const [environment, setEnvironment] = useState("");
  const [tried, setTried] = useState(false);
  const settings = useBcSettings(session);
  const create = useCreateBcSource();
  if (!admin) return null;

  const trimmed = label.trim().slice(0, MAX_LABEL);
  const tenantError = tenantProblem(tenant);
  const environmentError = environmentProblem(environment);
  const showTenantError = Boolean(tenantError && (tried || tenant !== ""));
  const showEnvironmentError = Boolean(
    environmentError && (tried || environment !== ""),
  );
  const ready = settings.isSuccess && appConfigured(settings.data);
  const blocked = settings.isSuccess && !ready;
  const valid = Boolean(trimmed) && !tenantError && !environmentError;

  function submit(event: FormEvent) {
    event.preventDefault();
    setTried(true);
    if (!valid || !ready || create.isPending) return;
    create.mutate(
      {
        label: trimmed,
        tenant_id: tenant.trim().toLowerCase(),
        // Environment names route requests, so their case is kept exactly.
        bc_environment: environment.trim(),
      },
      {
        onSuccess: () => {
          setLabel("");
          setTenant("");
          setEnvironment("");
          setTried(false);
        },
      },
    );
  }

  return (
    <section className="panel source-add" aria-labelledby={titleId}>
      <h2 id={titleId}>Add a Business Central source</h2>
      <p className="source-muted">
        Each Business Central source reads one company in one tenant
        environment. Add the source here, then discover and choose its company
        on the source card.
      </p>
      <p className="source-muted">
        This connector signs in as your Entra app with its client credentials;
        there is no Microsoft sign-in step. Before discovery, a tenant
        administrator must grant admin consent to the app’s Business Central
        application permission (API.ReadWrite.All) and add the app on the
        Microsoft Entra applications page in Business Central with the
        permission sets it needs.
      </p>
      <form onSubmit={submit} noValidate>
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
            <span id={`${nameId}-help`} className="field-help">
              For example, “EU trading company”. A different company needs its
              own source.
            </span>
          </div>
        </div>
        <div className="source-add-row">
          <div className="source-field">
            <label htmlFor={tenantId}>Tenant ID</label>
            <input
              id={tenantId}
              value={tenant}
              onChange={(event) => setTenant(event.target.value)}
              required
              autoComplete="off"
              spellCheck={false}
              aria-invalid={showTenantError ? true : undefined}
              aria-describedby={`${tenantId}-help${showTenantError ? ` ${tenantId}-error` : ""}`}
            />
            <span id={`${tenantId}-help`} className="field-help">
              The Directory (tenant) ID shown in the Microsoft Entra admin
              center. It can’t be changed after the source is added.
            </span>
            {showTenantError && (
              <span id={`${tenantId}-error`} className="source-field-error">
                {tenantError}
              </span>
            )}
          </div>
          <div className="source-field">
            <label htmlFor={environmentId}>Environment name</label>
            <input
              id={environmentId}
              value={environment}
              onChange={(event) => setEnvironment(event.target.value)}
              required
              autoComplete="off"
              spellCheck={false}
              aria-invalid={showEnvironmentError ? true : undefined}
              aria-describedby={`${environmentId}-help${showEnvironmentError ? ` ${environmentId}-error` : ""}`}
            />
            <span id={`${environmentId}-help`} className="field-help">
              The environment’s name exactly as listed in the Business Central
              admin center, including letter case. It can’t be changed later.
            </span>
            {showEnvironmentError && (
              <span
                id={`${environmentId}-error`}
                className="source-field-error"
              >
                {environmentError}
              </span>
            )}
          </div>
        </div>
        <div className="source-add-row">
          <button
            type="submit"
            className="button primary"
            disabled={!valid || !ready || create.isPending}
          >
            {create.isPending ? "Adding…" : "Add Business Central source"}
          </button>
        </div>
      </form>
      {blocked && (
        <div className="notice source-warning">
          <p>
            The Business Central app isn’t configured yet, so a source can’t be
            added or connected.{" "}
            <a className="text-link" href="/settings">
              Configure the Business Central app in Settings
            </a>
            .
          </p>
        </div>
      )}
      {settings.isError && (
        <div className="notice danger" role="alert">
          <p>
            <strong>We couldn’t check the Business Central app settings.</strong>{" "}
            {problem(settings.error, "Please try again.")}
          </p>
          <p>Adding a Business Central source is unavailable until they load.</p>
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
          <p className="source-muted">
            Checking the Business Central app settings…
          </p>
        )}
        {create.isSuccess && (
          <div className="notice source-applied source-outcome">
            <p>
              <strong>
                “{create.data.label}” was added but is not connected yet.
              </strong>{" "}
              Use Discover companies on its card to choose the company.
              Nothing is imported until a company is connected and synced.
            </p>
          </div>
        )}
      </div>
      {create.isError && (
        <div className="notice danger" role="alert">
          <p>
            <strong>
              {refused(create.error)
                ? "The source was not added."
                : "The add-source request could not be confirmed. Check your sources before trying again."}
            </strong>{" "}
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

type StatusInfo = { label: string; tone: string; detail: string };
const statusInfo: Record<string, StatusInfo> = {
  configured: {
    label: "Not connected",
    tone: "",
    detail:
      "The tenant and environment are recorded, but no company is connected. A configured app in Settings doesn’t connect a company by itself. Nothing is imported until a company is connected and its first sync finishes.",
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
      "Business Central no longer accepts this source’s app access. Polling is paused until an Admin reconnects the same company.",
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
    statusInfo[status] ?? {
      label: `Unknown status (${status})`,
      tone: "source-stale",
      detail:
        "The server reported a status this page doesn’t recognize, so no actions are offered.",
    }
  );
}

const subscriptionHealth: Record<string, StatusInfo> = {
  disabled: {
    label: "Webhooks off",
    tone: "",
    detail: "Scheduled polling reads changes.",
  },
  pending: {
    label: "Subscriptions pending",
    tone: "source-stale",
    detail:
      "Subscriptions are being registered or updated. Notifications aren’t confirmed yet; scheduled polling continues.",
  },
  active: {
    label: "Subscriptions active",
    tone: "success",
    detail:
      "The server reports the listed subscriptions active. Scheduled polling continues as a backstop.",
  },
  degraded: {
    label: "Subscriptions degraded",
    tone: "source-error",
    detail:
      "Webhooks are on, but at least one subscription isn’t active. Scheduled polling continues. Reconcile to retry.",
  },
  cleanup_pending: {
    label: "Cleanup pending",
    tone: "source-stale",
    detail: "Subscriptions are waiting to be removed from Business Central.",
  },
};

const subscriptionStatusLabels: Record<string, string> = {
  pending: "Pending",
  active: "Active",
  expired: "Expired",
  failed: "Failed",
};
const resourceLabels: Record<string, string> = {
  customers: "Customers",
  vendors: "Vendors",
};

/** Names are shown as reported; duplicates gain their ID and blanks show only the ID. */
function companyOptions(items: BcCompany[]) {
  const seen = new Set<string>();
  const unique = items.filter((item) => {
    if (!item || typeof item.id !== "string" || !item.id || seen.has(item.id))
      return false;
    seen.add(item.id);
    return true;
  });
  const nameOf = (item: BcCompany) =>
    typeof item.display_name === "string" ? item.display_name.trim() : "";
  const counts = new Map<string, number>();
  for (const item of unique) {
    const name = nameOf(item).toLowerCase();
    if (name) counts.set(name, (counts.get(name) ?? 0) + 1);
  }
  return unique.map((item) => {
    const name = nameOf(item);
    return {
      id: item.id,
      label: !name
        ? `${item.id} (no display name)`
        : (counts.get(name.toLowerCase()) ?? 0) > 1
          ? `${name} (${item.id})`
          : name,
    };
  });
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
  const runs = Array.isArray(outcome.run_ids) ? outcome.run_ids.length : null;
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
  // A finished screening wins over a task ID kept only as provenance.
  if (status === "success")
    return (
      <p>
        Screening finished for new and changed parties
        {runs !== null &&
          runs > 0 &&
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
  switch (attempt.kind) {
    case "connect":
      return "The first full sync";
    case "sync":
      return attempt.mode === "full" ? "The full sync" : "The incremental sync";
    case "subscriptions":
      return attempt.action === "reconcile"
        ? "The subscription reconcile"
        : "The webhook change";
    case "disconnect":
      return attempt.accessOnly ? "The stored app access clear" : attempt.retry ? "The subscription cleanup retry" : "The disconnect";
  }
}

function OperationProgress({
  attempt,
  data,
  uncertain,
}: {
  attempt: Attempt;
  data: Operation | undefined;
  uncertain: boolean;
}) {
  const name = attemptName(attempt);
  // Before the first read, the accepted request itself reported "queued".
  const status = data?.status ?? "queued";
  if (isActive(status)) {
    if (uncertain)
      return (
        <p className="source-muted">
          {name} was last reported as “{status}”. Its current status couldn’t
          be checked, so it isn’t known to be complete.
        </p>
      );
    return (
      <p className="source-progress">
        {attempt.kind === "connect" && status === "queued"
          ? "The company is connected and its first full sync is queued. Nothing is imported until it finishes."
          : status === "queued"
            ? `${name} is queued. The server hasn’t started it yet.`
            : `${name} is running. This page checks for progress every few seconds.`}
        {attempt.kind === "disconnect" &&
          " Scheduled polling for this source has already stopped."}
      </p>
    );
  }
  const outcome = outcomeOf(data?.outcome);
  const code = data?.error_code ? ` Error code: ${data.error_code}.` : "";
  if (status === "success") {
    if (attempt.kind === "disconnect")
      return (
        <div className="notice source-applied source-outcome">
          <p>
            <strong>{name} finished.</strong>{" "}
            {attempt.accessOnly
              ? "Stored app access was cleared without connecting a company or importing records."
              : "This source is no longer polled."}{" "}
            Its parties, screening history and audit records are kept. The
            current access and subscription cleanup status are shown above.
          </p>
        </div>
      );
    if (attempt.kind === "subscriptions")
      return (
        <div className="notice source-applied source-outcome">
          <p>
            <strong>{name} finished.</strong> The subscription details above
            are the server’s current report.
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
  if (status === "skipped")
    return (
      <div className="notice source-warning">
        <p>
          <strong>The server skipped this request.</strong>
          {code}
        </p>
      </div>
    );
  const syncing = attempt.kind === "sync" || attempt.kind === "connect";
  return (
    <div className="notice danger source-outcome">
      <p>
        <strong>
          {name} ended with status “{status}”.
        </strong>
        {code}
      </p>
      {syncing ? (
        <>
          <p>
            Record counts on this source come from the server and refresh
            automatically; anything saved before the operation stopped is
            reflected there.
          </p>
          {COUNT_LABELS.some(([key]) => numberOf(outcome, key) !== null) && (
            <>
              <p>
                The server reported these applied record counts before the
                operation stopped.
              </p>
              <SyncCounts outcome={outcome} />
              <ScreeningOutcome outcome={outcome} />
            </>
          )}
        </>
      ) : attempt.kind === "subscriptions" ? (
        <p>
          The subscription details above show the server’s last reported
          state. Scheduled polling continues.
        </p>
      ) : (
        <p>Check the connection and cleanup status above before trying again.</p>
      )}
    </div>
  );
}

function CleanupNotice({ connection }: { connection: BcConnection }) {
  const { cleanup_status: cleanup, cleanup_error_code: code, status } =
    connection;
  if (cleanup === "pending")
    return (
      <div className="notice source-warning">
        <p>
          <strong>Subscription cleanup is pending.</strong> The server still
          has to remove this source’s Business Central subscriptions.
          Discovery and connecting are unavailable until it finishes.
        </p>
      </div>
    );
  if (cleanup === "failed")
    return (
      <div className="notice danger">
        <p>
          <strong>Subscription cleanup failed.</strong>
          {code && ` Error code: ${code}.`}
        </p>
        <p>
          {status === "disconnected" &&
            "This source is still disconnected here, so it isn’t polled. "}
          The server keeps what it needs to retry the cleanup. Discovery and
          connecting are unavailable until cleanup succeeds.
        </p>
      </div>
    );
  if (cleanup === "completed")
    return <p className="source-muted">Subscription cleanup completed.</p>;
  if (
    cleanup === "not_required" &&
    (status === "disconnected" || status === "disconnecting")
  )
    return <p className="source-muted">No subscriptions needed cleanup.</p>;
  return null;
}

function SubscriptionTable({
  subscriptions,
}: {
  subscriptions: Subscription[];
}) {
  const captionId = useId();
  if (subscriptions.length === 0)
    return (
      <p className="source-muted">
        The server lists no Business Central subscriptions for this source.
      </p>
    );
  // Focusable so keyboard users can scroll the table on narrow screens.
  return (
    <div
      className="source-table-wrap"
      role="region"
      aria-labelledby={captionId}
      tabIndex={0}
    >
      <table className="source-table">
        <caption id={captionId}>Business Central subscriptions</caption>
        <thead>
          <tr>
            <th scope="col">Resource</th>
            <th scope="col">Subscription ID</th>
            <th scope="col">Expires</th>
            <th scope="col">Reported status</th>
            <th scope="col">Last error code</th>
          </tr>
        </thead>
        <tbody>
          {subscriptions.map((item, index) => (
            <tr key={`${item.resource}-${item.subscription_id ?? index}`}>
              <th scope="row">
                {resourceLabels[item.resource] ?? item.resource}
              </th>
              <td>
                <span className="source-cell">
                  {item.subscription_id ?? "Not registered"}
                </span>
              </td>
              <td>
                {item.expiration_date_time ? (
                  <LocalTime value={item.expiration_date_time} />
                ) : (
                  "Not reported"
                )}
              </td>
              <td>{subscriptionStatusLabels[item.status] ?? item.status}</td>
              <td>{item.last_error_code ?? "None"}</td>
            </tr>
          ))}
        </tbody>
      </table>
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
        <p>
          The request may have reached the server. The connection status is
          being rechecked; review it before trying again.
        </p>
      )}
    </div>
  );
}

export function BcSourceControls({
  session,
  source,
}: {
  session: Session;
  source: Source;
}) {
  const admin = canAdminister(session) && source.kind === "bc";
  const titleId = useId();
  const webhookId = useId();
  const companyId = useId();
  const client = useQueryClient();
  const connectionKey = keys.connection(source.id);
  const settings = useBcSettings(session);
  const connection = useQuery({
    queryKey: connectionKey,
    queryFn: () =>
      api<BcConnection>(`/sources/${enc(source.id)}/bc/connection`),
    enabled: admin,
    retry: false,
  });
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [selected, setSelected] = useState("");
  const [confirming, setConfirming] = useState(false);
  const operation = useOperation(attempt?.id ?? null);

  function refreshConnection() {
    void client.invalidateQueries({ queryKey: connectionKey });
  }
  function recheck(error: unknown) {
    refreshConnection();
    // A refusal may mean the app configuration changed elsewhere.
    if (
      error instanceof ApiError &&
      (error.status === 409 || error.status === 422)
    )
      void client.invalidateQueries({ queryKey: bcKeys.settings });
  }
  function accept(result: BcAcceptedAttempt, next: Attempt) {
    if (result.connection) client.setQueryData(connectionKey, result.connection);
    else refreshConnection();
    setAttempt(next);
    void client.invalidateQueries({ queryKey: sourceKeys.list });
  }

  const discover = useMutation({
    gcTime: 0,
    retry: false,
    mutationFn: () =>
      api<{ items: BcCompany[] }>(`/sources/${enc(source.id)}/bc/companies`, {
        method: "POST",
        body: "{}",
      }),
    onSuccess: () => {
      setSelected("");
      // Discovery may cache app access while the source remains unbound.
      refreshConnection();
    },
    onError: recheck,
  });
  const connect = useMutation({
    retry: false,
    mutationFn: (company: string) =>
      api<BcAcceptedAttempt>(`/sources/${enc(source.id)}/bc/connect`, {
        method: "POST",
        body: JSON.stringify({ company_id: company }),
      }),
    onSuccess: (result) => {
      accept(result, { id: result.attempt_id, kind: "connect" });
      discover.reset();
      setSelected("");
    },
    onError: recheck,
  });
  const sync = useMutation({
    retry: false,
    mutationFn: (mode: SyncMode) =>
      api<QueuedSync>(`/sources/${enc(source.id)}/sync`, {
        method: "POST",
        body: JSON.stringify({ mode }),
      }),
    onSuccess: (queued, mode) =>
      setAttempt({ id: queued.attempt_id, kind: "sync", mode }),
    onError: refreshConnection,
  });
  const webhook = useMutation({
    retry: false,
    mutationFn: (enabled: boolean) =>
      api<BcAcceptedAttempt>(`/sources/${enc(source.id)}/bc/connection`, {
        method: "PATCH",
        body: JSON.stringify({ webhooks_enabled: enabled }),
      }),
    onSuccess: (result) =>
      accept(result, {
        id: result.attempt_id,
        kind: "subscriptions",
        action: "webhooks",
      }),
    // The checkbox always reflects the server, so reload its real value.
    onError: refreshConnection,
  });
  const reconcile = useMutation({
    retry: false,
    mutationFn: () =>
      api<BcAcceptedAttempt>(
        `/sources/${enc(source.id)}/bc/subscriptions/reconcile`,
        { method: "POST", body: "{}" },
      ),
    onSuccess: (result) =>
      accept(result, {
        id: result.attempt_id,
        kind: "subscriptions",
        action: "reconcile",
      }),
    onError: refreshConnection,
  });
  const disconnect = useMutation<BcAcceptedAttempt, Error, { retry: boolean; accessOnly: boolean }>({
    retry: false,
    mutationFn: () =>
      api<BcAcceptedAttempt>(`/sources/${enc(source.id)}/bc/disconnect`, {
        method: "POST",
        body: "{}",
      }),
    onSuccess: (result, choice) => {
      accept(result, { id: result.attempt_id, kind: "disconnect", ...choice });
      discover.reset();
      setSelected("");
      setConfirming(false);
    },
    onError: refreshConnection,
  });

  if (!admin) return null;

  function clearNotices() {
    connect.reset();
    sync.reset();
    webhook.reset();
    reconcile.reset();
    disconnect.reset();
  }

  const data = connection.data;
  const status = data?.status;
  const subscriptions: Subscription[] = Array.isArray(data?.subscriptions)
    ? data.subscriptions
    : [];
  // Every action needs current connection facts and app flags.
  const factsKnown =
    data !== undefined &&
    !connection.isError &&
    settings.isSuccess &&
    !settings.isError;
  const appReady = appConfigured(settings.data);
  const opPending =
    attempt !== null &&
    (operation.isError || !operation.data || isActive(operation.data.status));
  const busy =
    discover.isPending ||
    connect.isPending ||
    sync.isPending ||
    webhook.isPending ||
    reconcile.isPending ||
    disconnect.isPending ||
    connection.isFetching ||
    opPending;
  const bound = Boolean(data?.company_id);
  const backlog =
    data?.cleanup_status === "pending" || data?.cleanup_status === "failed";
  const needsCompany =
    status === "configured" ||
    status === "disconnected" ||
    status === "reconnect_required";
  const canDiscover =
    factsKnown &&
    appReady &&
    !bound &&
    (status === "configured" || status === "disconnected") &&
    !backlog;
  const canReconnect =
    factsKnown &&
    appReady &&
    bound &&
    (status === "reconnect_required" || status === "disconnected") &&
    !backlog;
  const canSync = factsKnown && status === "connected";
  const retryCleanup =
    (status === "disconnected" || status === "configured") &&
    data?.cleanup_status === "failed";
  const clearStoredAccess =
    (status === "configured" || (status === "disconnected" && !bound)) &&
    data?.access_token_configured === true && !backlog;
  const disconnectLabel = clearStoredAccess ? "Clear stored app access" : retryCleanup ? "Retry subscription cleanup" : "Disconnect";
  const confirmationLabel = clearStoredAccess ? "Confirm clear stored app access" : retryCleanup ? "Confirm cleanup retry" : "Confirm disconnect";
  const canDisconnect =
    factsKnown &&
    (status === "connected" || status === "reconnect_required" || retryCleanup || clearStoredAccess);
  const canReconcile =
    factsKnown &&
    status === "connected" &&
    (data?.webhooks_enabled === true || subscriptions.length > 0);
  const info = data ? describeStatus(data.status) : null;
  const health = data
    ? (subscriptionHealth[data.subscription_status] ?? {
        label: `Unknown (${data.subscription_status})`,
        tone: "source-stale",
        detail: "The server reported a subscription state this page doesn’t recognize.",
      })
    : null;
  const options = discover.data
    ? companyOptions(
        Array.isArray(discover.data.items) ? discover.data.items : [],
      )
    : null;
  const selectedValid = Boolean(
    options?.some((option) => option.id === selected),
  );

  function runDiscover() {
    clearNotices();
    setSelected("");
    discover.mutate();
  }

  function connectSelected() {
    if (!selectedValid || busy) return;
    clearNotices();
    connect.mutate(selected);
  }

  return (
    <section className="admin-subsection" aria-labelledby={titleId}>
      <h4 id={titleId}>Business Central connection</h4>
      {connection.isPending && (
        <p className="source-muted" role="status">
          Checking the Business Central connection…
        </p>
      )}
      {connection.isError && (
        <div className="notice danger" role="alert">
          <p>
            <strong>We couldn’t load the Business Central connection.</strong>{" "}
            {problem(connection.error, "Please try again.")}
          </p>
          <p>
            {data && "The details below are from the last successful check. "}
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
      {settings.isError && (
        <div className="notice danger" role="alert">
          <p>
            <strong>We couldn’t check the Business Central app settings.</strong>{" "}
            {problem(settings.error, "Please try again.")}
          </p>
          <p>Actions are unavailable until they load.</p>
          <button
            type="button"
            className="button secondary"
            onClick={() => void settings.refetch()}
            disabled={settings.isFetching}
          >
            {settings.isFetching ? "Checking…" : "Check app settings again"}
          </button>
        </div>
      )}
      {data && info && health && (
        <>
          <p>
            <span className={`badge ${info.tone}`}>{info.label}</span>
          </p>
          <p className="source-muted">{info.detail}</p>
          <dl className="source-facts">
            <Fact label="Tenant ID">{data.tenant_id}</Fact>
            <Fact label="Environment">{data.bc_environment}</Fact>
            <Fact label="Company ID">
              {data.company_id ?? "No company connected yet"}
            </Fact>
            <Fact label="Access token">
              {data.access_token_configured ? "Stored" : "None stored"}
            </Fact>
            {data.access_expires_at && (
              <Fact label="Token expires">
                <LocalTime value={data.access_expires_at} />
              </Fact>
            )}
            <Fact label="Webhooks">{data.webhooks_enabled ? "On" : "Off"}</Fact>
            <Fact label="Subscriptions">
              <span className={`badge ${health.tone}`}>{health.label}</span>
            </Fact>
            {data.webhook_url && (
              <Fact label="Notification URL">
                <code>{data.webhook_url}</code>
              </Fact>
            )}
          </dl>
          <p className="source-muted">
            The tenant, environment and a connected company are permanent for
            this source. A different company or tenant needs a new Business
            Central source.
          </p>
          <CleanupNotice connection={data} />
        </>
      )}
      {factsKnown && !appReady && needsCompany && (
        <p className="source-muted">
          The Business Central app isn’t configured, so companies can’t be
          discovered or connected.{" "}
          <a className="text-link" href="/settings">
            Check Business Central app settings
          </a>
          .
        </p>
      )}
      {factsKnown && backlog && needsCompany && (
        <p className="source-muted">
          Discovery and connecting stay unavailable until subscription cleanup
          succeeds.
        </p>
      )}
      {canDiscover && (
        <div className="admin-form">
          <p className="source-muted">
            Discovery reads the companies this environment exposes to your
            Entra app. It doesn’t connect or import anything. If it fails,
            check that a tenant administrator granted admin consent and added
            the app in Business Central.
          </p>
          <div className="admin-actions">
            <button
              type="button"
              className="button secondary"
              onClick={runDiscover}
              disabled={busy}
            >
              {discover.isPending
                ? "Discovering companies…"
                : discover.isSuccess || discover.isError
                  ? "Discover companies again"
                  : "Discover companies"}
            </button>
          </div>
          {options && options.length === 0 && (
            <div className="notice source-warning" role="status">
              <p>
                <strong>No companies were returned.</strong> Check the
                environment name and the app’s permissions in this tenant, then
                discover again.
              </p>
            </div>
          )}
          {options && options.length > 0 && (
            <div className="source-add-row">
              <div className="source-field">
                <label htmlFor={companyId}>Company to connect</label>
                <select
                  id={companyId}
                  value={selected}
                  required
                  disabled={busy}
                  aria-describedby={`${companyId}-help`}
                  onChange={(event) => setSelected(event.target.value)}
                >
                  <option value="">Choose a company</option>
                  {options.map((option) => (
                    <option key={option.id} value={option.id}>
                      {option.label}
                    </option>
                  ))}
                </select>
                <span id={`${companyId}-help`} className="field-help">
                  {selectedValid
                    ? `Company ID ${selected}. Connecting binds it to this source permanently and queues a full sync.`
                    : "Connecting binds the chosen company to this source permanently and queues a full sync."}
                </span>
              </div>
              <button
                type="button"
                className="button primary"
                onClick={connectSelected}
                disabled={busy || !selectedValid}
              >
                {connect.isPending ? "Connecting…" : "Connect company"}
              </button>
            </div>
          )}
        </div>
      )}
      {canReconnect && data && (
        <div className="admin-form">
          <p className="source-muted">
            Reconnect checks the same company (company ID {data.company_id}) in
            environment {data.bc_environment} and queues a full sync. To read a
            different company or tenant, add a new Business Central source.
          </p>
          <div className="admin-actions">
            <button
              type="button"
              className="button primary"
              onClick={() => {
                if (!data.company_id) return;
                clearNotices();
                connect.mutate(data.company_id);
              }}
              disabled={busy}
            >
              {connect.isPending ? "Reconnecting…" : "Reconnect"}
            </button>
          </div>
        </div>
      )}
      {(canSync || canDisconnect || canReconcile) && (
        <div className="admin-actions">
          {canSync && (
            <>
              <button
                type="button"
                className="button primary"
                onClick={() => {
                  clearNotices();
                  sync.mutate("incremental");
                }}
                disabled={busy}
              >
                Sync changes
              </button>
              <button
                type="button"
                className="button secondary"
                onClick={() => {
                  clearNotices();
                  sync.mutate("full");
                }}
                disabled={busy}
              >
                Full sync
              </button>
            </>
          )}
          {canReconcile && (
            <button
              type="button"
              className="button secondary"
              onClick={() => {
                clearNotices();
                reconcile.mutate();
              }}
              disabled={busy}
            >
              Reconcile subscriptions
            </button>
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
              {disconnectLabel}
            </button>
          )}
        </div>
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
          aria-label={confirmationLabel}
        >
          <p>
            <strong>
              {clearStoredAccess
                ? "Clear stored app access for this source?"
                : retryCleanup
                  ? "Retry removing this source’s subscriptions?"
                  : "Disconnect this Business Central company?"}
            </strong>
          </p>
          <p>
            {clearStoredAccess
              ? "This clears app access cached during company discovery without connecting a company or importing records."
              : "Scheduled polling stops immediately."}{" "}
            Parties, screening history and audit records from this source are kept.
            The server attempts to remove any change-notification subscriptions
            this source registered and shows the actual cleanup outcome here.
          </p>
          <p>
            This doesn’t remove admin consent or the Entra app from the tenant;
            a tenant administrator manages those in Microsoft Entra and
            Business Central.
          </p>
          <div className="admin-actions">
            <button
              type="button"
              className="button primary"
              onClick={() => disconnect.mutate({ retry: retryCleanup, accessOnly: clearStoredAccess })}
              disabled={busy}
            >
              {confirmationLabel}
            </button>
            <button
              type="button"
              className="button secondary"
              onClick={() => setConfirming(false)}
              disabled={disconnect.isPending}
            >
              {clearStoredAccess || retryCleanup ? "Cancel" : "Keep connected"}
            </button>
          </div>
        </div>
      )}
      {factsKnown && status === "connected" && data && health && (
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
            <span>Receive Business Central change notifications (webhooks)</span>
          </label>
          <span id={`${webhookId}-help`} className="field-help">
            Optional. Turning this on needs a connected company and a public
            HTTPS webhook address in Settings; the server checks both and
            queues subscription setup, which isn’t the same as notifications
            being active. The server supplies the notification URL and
            generates its private verification value, which is never shown.
            Scheduled polling continues either way.
          </span>
          {!data.webhooks_enabled &&
            settings.data?.webhook_base_url === null && (
              <p className="source-muted">
                No public webhook address is configured in Settings yet.
              </p>
            )}
          <p className="source-muted">{health.detail}</p>
          <SubscriptionTable subscriptions={subscriptions} />
        </div>
      )}
      {data && status !== "connected" && subscriptions.length > 0 && (
        <div className="admin-form">
          <SubscriptionTable subscriptions={subscriptions} />
        </div>
      )}
      <div role="status" aria-label="Business Central operation status">
        {webhook.isPending && (
          <p className="source-muted">Saving the notification setting…</p>
        )}
        {attempt && (
          <OperationProgress
            attempt={attempt}
            data={operation.data}
            uncertain={operation.isError}
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
            running on the server. Other Business Central actions are paused
            until its status is known.
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
      {discover.isError && (
        <div className="notice danger source-outcome" role="alert">
          <p>
            <strong>Companies couldn’t be discovered.</strong>{" "}
            {problem(discover.error, "Please try again.")}
          </p>
          <p>
            Discovery only reads; nothing was connected or imported, and this
            source’s tenant and environment are unchanged.
          </p>
        </div>
      )}
      {connect.isError && (
        <RequestError
          error={connect.error}
          refusedTitle="The company wasn’t connected."
          uncertainTitle="The connect request couldn’t be confirmed."
        />
      )}
      {sync.isError && (
        <RequestError
          error={sync.error}
          refusedTitle="The sync wasn’t queued."
          uncertainTitle="The sync request couldn’t be confirmed."
        />
      )}
      {reconcile.isError && (
        <RequestError
          error={reconcile.error}
          refusedTitle="The subscription reconcile wasn’t queued."
          uncertainTitle="The subscription reconcile request couldn’t be confirmed."
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
            <strong>
              {refused(webhook.error)
                ? "The notification setting wasn’t changed."
                : "The notification setting could not be confirmed."}
            </strong>{" "}
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
