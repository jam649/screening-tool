import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Session } from "./api";
import { canAdminister } from "./adminApi";

/** Public flags only: the server never returns the client ID or secret. */
export type BcAppSettings = {
  client_id_configured: boolean;
  client_secret_configured: boolean;
  configured: boolean;
  webhook_base_url: string | null;
};
// Credentials travel only as explicit replacements or a clear request; omitted keys preserve them.
export type BcSettingsChanges = {
  client_id?: string;
  client_secret?: string;
  clear_credentials?: true;
};

export type BcConnectionStatus =
  | "configured"
  | "connected"
  | "reconnect_required"
  | "disconnecting"
  | "disconnected";
export type BcSubscriptionHealth = "disabled" | "pending" | "active" | "degraded" | "cleanup_pending";
export type BcCleanupStatus = "not_required" | "pending" | "completed" | "failed";
export type BcResource = "customers" | "vendors";
export type BcSubscriptionState = "pending" | "active" | "expired" | "failed";
export type BcSubscription = {
  resource: BcResource;
  subscription_id: string | null;
  expiration_date_time: string | null;
  status: BcSubscriptionState;
  last_error_code: string | null;
};
export type BcConnection = {
  source_id: string;
  tenant_id: string;
  bc_environment: string;
  company_id: string | null;
  status: BcConnectionStatus;
  access_token_configured: boolean;
  access_expires_at: string | null;
  webhooks_enabled: boolean;
  webhook_url: string | null;
  subscription_status: BcSubscriptionHealth;
  cleanup_status: BcCleanupStatus;
  cleanup_error_code: string | null;
  subscriptions: BcSubscription[];
};
export type BcConnectionChanges = { webhooks_enabled: boolean };
export type BcCompany = { id: string; display_name: string };
export type BcCompanies = { items: BcCompany[] };
export type BcOperation = "source_sync" | "bc_subscriptions" | "source_disconnect";
export type BcAcceptedAttempt = {
  attempt_id: string;
  operation: BcOperation;
  status: "queued";
  connection?: BcConnection;
};

export const bcKeys = {
  settings: ["adminConnectors", "bc"] as const,
};

export const BC_SECRET_MAX = 8192;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export const isUuid = (text: string) => UUID.test(text);

/** The Entra application ID in canonical form (trimmed, lowercase), or null when it is not a UUID. */
export function canonicalClientId(text: string): string | null {
  const trimmed = text.trim();
  return isUuid(trimmed) ? trimmed.toLowerCase() : null;
}

const isControl = (code: number) => code <= 0x1f || (code >= 0x7f && code <= 0x9f);

/** Why a nonempty secret would be refused; the secret itself is never trimmed or rewritten. */
export function clientSecretError(secret: string): string | undefined {
  const points = Array.from(secret);
  if (points.some((char) => isControl(char.codePointAt(0) ?? 0)))
    return "The secret contains a control character, such as a tab or line break. Remove it and enter the secret again.";
  if (points.length > BC_SECRET_MAX)
    return `Enter a secret of at most ${BC_SECRET_MAX.toLocaleString("en-US")} characters.`;
  return undefined;
}

// Query families whose results can depend on which Entra app is configured.
const APP_DEPENDENT = new Set(["sources", "monitoring", "audit"]);

export function useBcSettings(session: Session) {
  return useQuery({
    queryKey: bcKeys.settings,
    queryFn: () => api<BcAppSettings>("/settings/connectors/bc"),
    enabled: canAdminister(session),
    retry: false,
  });
}

// Variables hold the app secret: never retried, never kept once the form settles.
export function useSaveBcSettings() {
  const client = useQueryClient();
  return useMutation({
    gcTime: 0,
    retry: false,
    mutationFn: (changes: BcSettingsChanges) =>
      api<BcAppSettings>("/settings/connectors/bc", {
        method: "PATCH",
        body: JSON.stringify(changes),
      }),
    onSuccess: (saved) => {
      client.setQueryData(bcKeys.settings, saved);
      void client.invalidateQueries({
        predicate: (query) => APP_DEPENDENT.has(String(query.queryKey[0])),
      });
    },
    // A 409, 422 or lost response may mean the saved state differs from this form.
    onError: () => client.invalidateQueries({ queryKey: bcKeys.settings }),
  });
}
