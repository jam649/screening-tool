import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type Session } from "./api";
import { canAdminister } from "./adminApi";

export const MODEL_PROVIDERS = ["none", "jev", "laya"] as const;
export type ModelProvider = (typeof MODEL_PROVIDERS)[number];

/** Public facts only: the server never returns either API key. */
export type ModelConfig = {
  provider: ModelProvider;
  generation: number;
  w_det: number;
  w_model: number;
  jev_model: string;
  laya_base_url: string;
  laya_model: string;
  laya_max_tokens: number;
  jev_api_key_configured: boolean;
  laya_api_key_configured: boolean;
  hosted_acknowledgment_valid: boolean;
  privacy_notice: string;
};
/** PATCH response: the saved facts plus the full re-screen it queued, if any. */
export type SavedModelConfig = ModelConfig & { pending_screening_task_id: string | null };
// Omitted fields preserve; keys travel only as explicit replacements or clears.
export type ModelSettingsChanges = {
  expected_generation: number;
  provider?: ModelProvider;
  w_det?: number;
  w_model?: number;
  laya_base_url?: string;
  jev_api_key?: string;
  laya_api_key?: string;
  clear_jev_api_key?: true;
  clear_laya_api_key?: true;
  hosted_acknowledgment?: string;
};
export type ModelTestResult = {
  status: "connected" | "failed";
  provider: string;
  generation: number;
  requested_model: string;
  returned_model: string | null;
  model_prob: number | null;
  usage: { input_tokens: number | null; output_tokens: number | null };
  attempts: number;
  elapsed_ms: number;
  error_code: string | null;
};

export const modelKeys = {
  settings: ["adminModelSettings"] as const,
};

export const MODEL_KEY_MAX = 8192;
// Weights are sent exactly as entered; this only absorbs binary floating-point error in the sum.
export const WEIGHT_SUM_TOLERANCE = 1e-9;

const isControl = (code: number) => code <= 0x1f || (code >= 0x7f && code <= 0x9f);

/** Why a nonempty key would be refused; the key itself is never trimmed or rewritten. */
export function keyError(key: string): string | undefined {
  const points = Array.from(key);
  if (points.some((char) => isControl(char.codePointAt(0) ?? 0)))
    return "The key contains a control character, such as a tab or line break. Remove it and enter the key again.";
  if (points.some((char) => (char.codePointAt(0) ?? 0) > 0x7e))
    return "Key must use printable ASCII characters";
  if (points.length > MODEL_KEY_MAX)
    return `Enter a key of at most ${MODEL_KEY_MAX.toLocaleString("en-US")} characters.`;
  return undefined;
}

const ENDPOINT = /^https?:\/\/(\[[^\]]*\]|[^/:?#@[\]]+)(?::([^/?#@]*))?\/?$/i;
const LOOPBACK_V4_PART = /^(0|[1-9]\d{0,2})$/;

function isCanonicalLoopbackV4(host: string) {
  const parts = host.split(".");
  return (
    parts.length === 4 &&
    parts[0] === "127" &&
    parts.every((part) => LOOPBACK_V4_PART.test(part) && Number(part) <= 255)
  );
}

function isLocalHost(host: string) {
  const lower = host.toLowerCase();
  if (lower === "localhost" || lower === "host.docker.internal" || isCanonicalLoopbackV4(host)) return true;
  // Normalize only for comparison, never the request text; the backend also
  // accepts valid literal IPv6 representations of the loopback address.
  if (!host.startsWith("[") || !host.endsWith("]") || host.includes("%")) return false;
  try { return new URL(`http://${host}`).hostname === "[::1]"; }
  catch { return false; }
}

/**
 * Early feedback mirroring the server's local-only rule; the server stays authoritative.
 * The text is checked as entered and never normalized.
 */
export function localEndpointError(text: string): string | undefined {
  const problem =
    "Enter a local endpoint such as http://127.0.0.1:8009 or http://host.docker.internal:8009: http or https, then localhost, a canonical loopback address (127.x.x.x or [::1]) or host.docker.internal, and optionally a port from 1 to 65535. Paths, queries, fragments, sign-in details and other hosts aren’t accepted.";
  const match = ENDPOINT.exec(text);
  if (!match) return problem;
  const [, host, port] = match;
  if (!isLocalHost(host)) return problem;
  if (port !== undefined && !(/^\d+$/.test(port) && Number(port) >= 1 && Number(port) <= 65535)) return problem;
  return undefined;
}

// Query families (owned by other packages) whose results depend on the model configuration.
const MODEL_DEPENDENT = new Set([
  "settings",
  "dashboard",
  "queue",
  "review",
  "parties",
  "party",
  "runs",
  "audit",
  "sources",
  "monitoring",
]);
const isDependent = (key: readonly unknown[]) =>
  MODEL_DEPENDENT.has(String(key[0])) || (key[0] === "admin" && key[1] === "settings");

export function useModelSettings(session: Session) {
  return useQuery({
    queryKey: modelKeys.settings,
    queryFn: () => api<ModelConfig>("/settings/model"),
    enabled: canAdminister(session),
    retry: false,
  });
}

// Variables can hold API keys: never retried, never kept once the form settles.
export function useSaveModelSettings() {
  const client = useQueryClient();
  return useMutation({
    gcTime: 0,
    retry: false,
    mutationFn: (changes: ModelSettingsChanges) =>
      api<SavedModelConfig>("/settings/model", {
        method: "PATCH",
        body: JSON.stringify(changes),
      }),
    onSuccess: (saved) => {
      client.setQueryData(modelKeys.settings, saved);
      void client.invalidateQueries({ predicate: (query) => isDependent(query.queryKey) });
    },
    // A 409, 422 or lost response may mean the saved configuration differs from this form.
    onError: () => client.invalidateQueries({ queryKey: modelKeys.settings }),
  });
}

// Tests only the saved configuration at one generation; the browser never retries it.
export function useTestModelConnection() {
  const client = useQueryClient();
  return useMutation({
    gcTime: 0,
    retry: false,
    mutationFn: (body: { expected_generation: number }) =>
      api<ModelTestResult>("/settings/model/test", {
        method: "POST",
        body: JSON.stringify({ expected_generation: body.expected_generation }),
      }),
    onError: (error) => {
      if (error instanceof ApiError && (error.status === 409 || error.status === 422))
        return client.invalidateQueries({ queryKey: modelKeys.settings });
      return undefined;
    },
  });
}
