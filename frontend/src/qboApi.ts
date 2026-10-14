import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Session } from "./api";
import { canAdminister } from "./adminApi";

export const QBO_ENVIRONMENTS = ["sandbox", "production"] as const;
export type QboEnvironment = (typeof QBO_ENVIRONMENTS)[number];

/** Public flags only: the server never returns client IDs, secrets or verifier tokens. */
export type QboAppFlags = {
  client_id_configured: boolean;
  client_secret_configured: boolean;
  verifier_token_configured: boolean;
  configured: boolean;
};
export type QboAppSettings = Record<QboEnvironment, QboAppFlags> & {
  redirect_uri: string | null;
  webhook_urls: Record<QboEnvironment, string | null>;
};
// Secrets travel only as explicit replacements or clear requests; omitted keys preserve them.
export type QboSettingsChanges = {
  environment: QboEnvironment;
  client_id?: string;
  client_secret?: string;
  verifier_token?: string;
  clear_credentials?: true;
  clear_verifier_token?: true;
};

export type QboConnectionStatus =
  | "configured"
  | "connected"
  | "reconnect_required"
  | "disconnecting"
  | "disconnected";
export type QboRevocationStatus = "pending" | "revoked" | "failed" | "not_required";
export type QboConnection = {
  source_id: string;
  environment: QboEnvironment;
  status: QboConnectionStatus;
  realm_id: string | null;
  access_expires_at: string | null;
  refresh_expires_at: string | null;
  webhooks_enabled: boolean;
  revocation_status: QboRevocationStatus | null;
  revocation_error_code: string | null;
};
export type QboConnectionChanges = { webhooks_enabled: boolean };
export type QboAuthorizationStart = { authorization_url: string; expires_at: string };
export type QboDisconnectAccepted = {
  attempt_id: string;
  operation: "source_disconnect";
  status: "queued";
  connection: QboConnection;
};

export const qboKeys = {
  settings: ["adminConnectors", "qbo"] as const,
};

// Query families whose results can depend on which Intuit app is configured.
const APP_DEPENDENT = new Set(["sources", "monitoring", "audit"]);

export function useQboSettings(session: Session) {
  return useQuery({
    queryKey: qboKeys.settings,
    queryFn: () => api<QboAppSettings>("/settings/connectors/qbo"),
    enabled: canAdminister(session),
    retry: false,
  });
}

// Variables hold app secrets: never retried, never kept once the form settles.
export function useSaveQboSettings() {
  const client = useQueryClient();
  return useMutation({
    gcTime: 0,
    retry: false,
    mutationFn: (changes: QboSettingsChanges) =>
      api<QboAppSettings>("/settings/connectors/qbo", {
        method: "PATCH",
        body: JSON.stringify(changes),
      }),
    onSuccess: (saved) => {
      client.setQueryData(qboKeys.settings, saved);
      void client.invalidateQueries({
        predicate: (query) => APP_DEPENDENT.has(String(query.queryKey[0])),
      });
    },
    // A 409 or 422 may mean the saved state moved underneath this form.
    onError: () => client.invalidateQueries({ queryKey: qboKeys.settings }),
  });
}

const INTUIT_AUTHORIZE = "https://appcenter.intuit.com/connect/oauth2";

/** The server's authorization URL only when it is Intuit's HTTPS OAuth 2.0 endpoint with a code request and state. */
export function verifiedAuthorizationUrl(text: string): string | null {
  let url: URL;
  try {
    url = new URL(text);
  } catch {
    return null;
  }
  if (url.username || url.password || url.port || url.hash) return null;
  if (`${url.origin}${url.pathname}` !== INTUIT_AUTHORIZE) return null;
  const params = url.searchParams;
  if (params.get("response_type") !== "code") return null;
  if (!params.get("state") || !params.get("client_id") || !params.get("redirect_uri"))
    return null;
  return url.toString();
}
