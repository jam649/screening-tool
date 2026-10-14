import { useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Session } from "./api";
import { canAdminister } from "./adminApi";

export const WEEKDAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"] as const;
export type Weekday = (typeof WEEKDAYS)[number];

export type MonitoringSettings = {
  csl_fetch_time: string;
  source_poll_minutes: number;
  retention_weekday: Weekday;
  retention_time: string;
};
export type MonitoringSchedule = {
  id: string;
  operation: string;
  next_run_at: string | null;
};
export type MonitoringOperation = {
  id: string;
  operation: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  error_code: string | null;
  outcome: unknown;
};
export type HealthItem = {
  kind: string;
  message: string;
  healthy?: boolean;
  [key: string]: unknown;
};
export type MonitoringStatus = {
  enabled: boolean;
  schedules: MonitoringSchedule[];
  operations: MonitoringOperation[];
  health: HealthItem[];
};
export type CycleAttempt = {
  attempt_id: string;
  status: "queued";
  operation: "monitor_cycle";
};

export const monitoringKeys = {
  all: ["monitoring"] as const,
  status: ["monitoring", "status"] as const,
  settings: ["monitoring", "settings"] as const,
  operation: (id: string) => ["monitoring", "operation", id] as const,
};

// Query families (owned by other packages) whose figures a monitoring cycle can change.
const CYCLE_DEPENDENT = new Set([
  "dashboard",
  "overview",
  "review",
  "queue",
  "parties",
  "party",
  "runs",
  "audit",
  "sources",
  "csl",
]);

export const POLL_MS = 1500;
export const isActive = (status: string | undefined) =>
  status === "queued" || status === "running";
/** Only the literal "success" status is success; any other settled status needs attention. */
export const needsAttention = (status: string) =>
  !isActive(status) && status !== "success" && status !== "skipped";
/** Health items are problems unless the server explicitly marks them healthy. */
export const isProblem = (item: HealthItem) => item.healthy !== true;

/** Most recent operation of each kind; unknown times keep the first listed. */
export function latestByOperation(operations: MonitoringOperation[]) {
  const when = (op: MonitoringOperation) =>
    Date.parse(op.started_at ?? op.finished_at ?? "");
  const latest = new Map<string, MonitoringOperation>();
  for (const op of operations) {
    const seen = latest.get(op.operation);
    if (!seen || when(op) > when(seen)) latest.set(op.operation, op);
  }
  return [...latest.values()];
}

/** Only the keys whose values differ from the loaded server settings. */
export function changedMonitoring(
  current: MonitoringSettings,
  next: MonitoringSettings,
): Partial<MonitoringSettings> {
  const changes: Record<string, unknown> = {};
  for (const key of Object.keys(next) as (keyof MonitoringSettings)[])
    if (next[key] !== current[key]) changes[key] = next[key];
  return changes as Partial<MonitoringSettings>;
}

export function useMonitoringStatus(session: Session) {
  return useQuery({
    queryKey: monitoringKeys.status,
    queryFn: () => api<MonitoringStatus>("/monitoring/status"),
    enabled: session.user.active,
    retry: false,
  });
}

export function useMonitoringSettings(session: Session) {
  return useQuery({
    queryKey: monitoringKeys.settings,
    queryFn: () => api<MonitoringSettings>("/settings/monitoring"),
    enabled: canAdminister(session),
    retry: false,
  });
}

export function useSaveMonitoringSettings() {
  const client = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: (changes: Partial<MonitoringSettings>) =>
      api<MonitoringSettings>("/settings/monitoring", {
        method: "PATCH",
        body: JSON.stringify(changes),
      }),
    onSuccess: (saved) => {
      client.setQueryData(monitoringKeys.settings, saved);
      // Next run times derive from the saved schedule.
      void client.invalidateQueries({ queryKey: monitoringKeys.status });
    },
  });
}

export function useStartCycle() {
  const client = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: () => api<CycleAttempt>("/monitoring/cycle", { method: "POST" }),
    // An overlap or failure may already be recorded; show the server's real state.
    onError: () => client.invalidateQueries({ queryKey: monitoringKeys.status }),
  });
}

/** Polls an attempt until it settles; a read error stops polling until refetch. */
export function useCycleAttempt(id: string | null) {
  const client = useQueryClient();
  const query = useQuery({
    queryKey: monitoringKeys.operation(id ?? ""),
    queryFn: () =>
      api<MonitoringOperation>(`/monitoring/operations/${encodeURIComponent(id ?? "")}`),
    enabled: id !== null,
    retry: false,
    refetchInterval: (q) =>
      q.state.status !== "error" && (!q.state.data || isActive(q.state.data.status))
        ? POLL_MS
        : false,
  });
  const settled = query.data !== undefined && !isActive(query.data.status);
  const uncertain = query.isError;
  useEffect(() => {
    if (id === null || !(settled || uncertain)) return;
    void client.invalidateQueries({ queryKey: monitoringKeys.status });
    void client.invalidateQueries({
      predicate: (q) => CYCLE_DEPENDENT.has(String(q.queryKey[0])),
    });
  }, [client, id, settled, uncertain]);
  return query;
}
