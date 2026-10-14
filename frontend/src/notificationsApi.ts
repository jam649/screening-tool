import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";
import { api, type Session } from "./api";
import { canAdminister } from "./adminApi";

export const TLS_MODES = ["starttls", "ssl", "none"] as const;
export type SmtpTls = (typeof TLS_MODES)[number];
export type NotificationConfig = {
  smtp_host: string;
  smtp_port: number;
  smtp_tls: SmtpTls;
  smtp_from: string;
  public_base_url: string;
  report_recipients: string[];
  report_time: string;
  likely_alerts_enabled: boolean;
  smtp_username_configured: boolean;
  smtp_password_configured: boolean;
};
export type PublicNotificationField = Exclude<
  keyof NotificationConfig,
  "smtp_username_configured" | "smtp_password_configured"
>;
export type PublicNotificationConfig = Pick<NotificationConfig, PublicNotificationField>;
// Credentials travel only as explicit replacements or a clear request, never as placeholders.
export type NotificationChanges = Partial<PublicNotificationConfig> & {
  smtp_username?: string;
  smtp_password?: string;
  clear_credentials?: true;
};
const PUBLIC_FIELDS: PublicNotificationField[] = [
  "smtp_host",
  "smtp_port",
  "smtp_tls",
  "smtp_from",
  "public_base_url",
  "report_recipients",
  "report_time",
  "likely_alerts_enabled",
];

export type DeliveryStatus = "pending" | "sending" | "sent" | "failed" | "uncertain" | "cancelled";
export type Delivery = {
  recipient: string;
  status: DeliveryStatus;
  attempts: number;
  error_code: string | null;
};
export type Batch = { batch_id: string; status: DeliveryStatus; deliveries: Delivery[] };
export type HealthKind =
  | "smtp_not_configured"
  | "delivery_failed"
  | "delivery_uncertain"
  | "operation_failed";
export type HealthItem = { kind: HealthKind | (string & {}); message: string };
export type NotificationStatus = { batches: Batch[]; health: HealthItem[] };
export type ReportStatus = NotificationStatus & { last_report_at: string | null };

export const notificationKeys = {
  status: ["notifications", "status"] as const,
  report: ["reports", "morning", "status"] as const,
  settings: ["adminNotifications", "settings"] as const,
};

// Query families whose results can change once a send or retry actually runs.
const AFTER_SEND = new Set(["notifications", "reports", "dashboard", "audit"]);
const refreshAfterSend = (client: QueryClient) =>
  client.invalidateQueries({
    predicate: (query) => AFTER_SEND.has(String(query.queryKey[0])),
  });

export const POLL_MS = 1500;
const ACTIVE = new Set<string>(["pending", "sending"]);
export const isActive = (batch: Batch) =>
  ACTIVE.has(batch.status) || batch.deliveries.some((item) => ACTIVE.has(item.status));
// Poll only while something is in flight, and stop once a read fails.
const pollWhileActive = (data: NotificationStatus | undefined, error: unknown) =>
  !error && data?.batches.some(isActive) ? POLL_MS : false;

export function useNotificationStatus(session: Session) {
  return useQuery({
    queryKey: notificationKeys.status,
    queryFn: () => api<NotificationStatus>("/notifications/status"),
    enabled: session.user.active,
    refetchInterval: (query) => pollWhileActive(query.state.data, query.state.error),
  });
}

export function useReportStatus(session: Session) {
  return useQuery({
    queryKey: notificationKeys.report,
    queryFn: () => api<ReportStatus>("/reports/morning/status"),
    enabled: session.user.active,
    refetchInterval: (query) => pollWhileActive(query.state.data, query.state.error),
  });
}

export function useNotificationConfig(session: Session) {
  return useQuery({
    queryKey: notificationKeys.settings,
    queryFn: () => api<NotificationConfig>("/settings/notifications"),
    enabled: canAdminister(session),
  });
}

// Variables may hold SMTP credentials: never retried, never kept after the form settles.
export function useSaveNotificationConfig() {
  const client = useQueryClient();
  return useMutation({
    gcTime: 0,
    retry: false,
    mutationFn: (changes: NotificationChanges) =>
      api<NotificationConfig>("/settings/notifications", {
        method: "PATCH",
        body: JSON.stringify(changes),
      }),
    onSuccess: (saved, changes) => {
      client.setQueryData(notificationKeys.settings, saved);
      void client.invalidateQueries({ queryKey: notificationKeys.status });
      if (changes.public_base_url !== undefined) {
        void client.invalidateQueries({ queryKey: ["adminConnectors"] });
        void client.invalidateQueries({ queryKey: ["sources"] });
      }
    },
    onError: () => client.invalidateQueries({ queryKey: notificationKeys.settings }),
  });
}

export function useSendTestEmail() {
  const client = useQueryClient();
  return useMutation({
    gcTime: 0,
    retry: false,
    mutationFn: (body: { recipient: string }) =>
      api<Batch>("/notifications/test", { method: "POST", body: JSON.stringify(body) }),
    onSettled: () => refreshAfterSend(client),
  });
}

export function useSendMorningReport() {
  const client = useQueryClient();
  return useMutation({
    gcTime: 0,
    retry: false,
    mutationFn: () => api<Batch>("/reports/morning/send", { method: "POST" }),
    onSettled: () => refreshAfterSend(client),
  });
}

// retry_uncertain is true only when a person explicitly asks for it.
export function useRetryBatch() {
  const client = useQueryClient();
  return useMutation({
    gcTime: 0,
    retry: false,
    mutationFn: ({ batchId, retryUncertain }: { batchId: string; retryUncertain: boolean }) =>
      api<Batch>(`/notifications/batches/${encodeURIComponent(batchId)}/retry`, {
        method: "POST",
        body: JSON.stringify({ retry_uncertain: retryUncertain }),
      }),
    onSettled: () => refreshAfterSend(client),
  });
}

const LOOPBACK = new Set(["localhost", "127.0.0.1", "[::1]", "::1"]);
export const isLoopback = (host: string) => LOOPBACK.has(host.trim().toLowerCase());
const EMAIL = /^[^\s@<>,;]+@[^\s@<>,;]+\.[^\s@<>,;]+$/;
export const validEmail = (value: string) => EMAIL.test(value);
export const validReportTime = (value: string) => /^([01]\d|2[0-3]):[0-5]\d$/.test(value);

/** HTTPS origin, or HTTP on loopback only; no path, query, fragment or credentials. */
export function baseUrlError(text: string): string | undefined {
  if (!text) return undefined;
  let url: URL;
  try {
    url = new URL(text);
  } catch {
    return "Enter an origin such as https://screening.example.com.";
  }
  if (url.username || url.password) return "Remove the embedded user name or password.";
  if (url.pathname !== "/" || /[?#]/.test(text))
    return "Enter only the scheme, host and optional port, with no path, query or fragment.";
  if (url.protocol === "https:") return undefined;
  if (url.protocol === "http:" && isLoopback(url.hostname)) return undefined;
  return "Use HTTPS, or plain HTTP only for localhost, 127.0.0.1 or [::1].";
}

/** Only public fields that differ from the loaded server settings. */
export function changedNotificationConfig(
  current: NotificationConfig,
  next: PublicNotificationConfig,
): NotificationChanges {
  const changes: Record<string, unknown> = {};
  for (const key of PUBLIC_FIELDS)
    if (JSON.stringify(next[key]) !== JSON.stringify(current[key])) changes[key] = next[key];
  return changes as NotificationChanges;
}
