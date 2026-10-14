import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Session, type User } from "./api";

export const NAME_WEIGHTS = ["token_set", "token_sort", "partial"] as const;
export const ADJUSTMENT_WEIGHTS = [
  "country_match",
  "country_mismatch",
  "city_match",
  "address_max",
  "entity_mismatch",
  "short_name",
] as const;
export const WEIGHT_KEYS = [...NAME_WEIGHTS, ...ADJUSTMENT_WEIGHTS] as const;
export type WeightKey = (typeof WEIGHT_KEYS)[number];
export const MIN_RETENTION_YEARS = 5;

export type WorkspaceSettings = {
  display_name: string;
  timezone: string;
  retention_years: number | null;
  candidate_min: number;
  review_threshold: number;
  likely_threshold: number;
  score_weights: Record<WeightKey, number>;
  legal_suffixes: string[];
  candidate_stopwords: string[];
  enabled_source_lists: string[] | null;
  disclaimer: string;
  disclaimer_url: string;
};
export type WritableSetting = Exclude<
  keyof WorkspaceSettings,
  "disclaimer" | "disclaimer_url"
>;
export type SettingsChanges = Partial<Pick<WorkspaceSettings, WritableSetting>>;

export type Role = User["role"];
export type UserChanges = { role?: Role; active?: boolean };
export type Invitation = {
  id: string;
  email: string;
  role: Role;
  expires_at: string;
  created_at: string;
  used_at: string | null;
  revoked_at: string | null;
};
export type UsersResponse = { items: User[]; invitations: Invitation[] };
export type CreatedInvitation = Invitation & {
  invite_url: string;
  delivery: "manual";
  delivery_pending: boolean;
  delivery_message: string;
};
export type InvitationPreview = { email: string; role: Role; expires_at: string };

export const adminKeys = {
  all: ["admin"] as const,
  settings: ["admin", "settings"] as const,
  users: ["admin", "users"] as const,
};

// Query families (owned by other packages) whose results depend on screening settings.
const SETTINGS_DEPENDENT = new Set([
  "dashboard",
  "overview",
  "parties",
  "party",
  "queue",
  "review",
  "runs",
  "audit",
]);

export const canAdminister = (session: Session | null | undefined) =>
  Boolean(session?.user.active && session.user.role.toLowerCase() === "admin");

export const messageOf = (error: unknown, fallback: string) =>
  error instanceof Error ? error.message : fallback;

/** A finite number from a nonblank draft, otherwise null (never NaN or Infinity). */
export function parseNumber(text: string): number | null {
  const trimmed = text.trim();
  if (!trimmed) return null;
  const value = Number(trimmed);
  return Number.isFinite(value) ? value : null;
}

/** Trimmed, nonempty, de-duplicated entries in their original order. */
export function parseList(text: string, separators: RegExp = /\n/): string[] {
  return [
    ...new Set(
      text
        .split(separators)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  ];
}

export function validTimezone(zone: string): boolean {
  if (!zone) return false;
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: zone });
    return true;
  } catch {
    return false;
  }
}

const stable = (value: unknown) =>
  JSON.stringify(value, (_key, item: unknown) =>
    item && typeof item === "object" && !Array.isArray(item)
      ? Object.fromEntries(
          Object.entries(item).sort(([a], [b]) => a.localeCompare(b)),
        )
      : item,
  );

/** Only the keys whose values differ from the loaded server settings. */
export function changedSettings(
  current: WorkspaceSettings,
  next: SettingsChanges,
): SettingsChanges {
  const changes: Record<string, unknown> = {};
  for (const key of Object.keys(next) as WritableSetting[])
    if (stable(next[key]) !== stable(current[key])) changes[key] = next[key];
  return changes as SettingsChanges;
}

export function useSettings(session: Session) {
  return useQuery({
    queryKey: adminKeys.settings,
    queryFn: () => api<WorkspaceSettings>("/settings"),
    enabled: canAdminister(session),
  });
}

export function useSaveSettings() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (changes: SettingsChanges) =>
      api<WorkspaceSettings>("/settings", {
        method: "PATCH",
        body: JSON.stringify(changes),
      }),
    onSuccess: (saved) => {
      client.setQueryData(adminKeys.settings, saved);
      // The product name comes from /setup; derived views may use new thresholds.
      void client.invalidateQueries({ queryKey: ["setup"] });
      void client.invalidateQueries({
        predicate: (query) =>
          SETTINGS_DEPENDENT.has(String(query.queryKey[0])),
      });
    },
  });
}

export function useUsers(session: Session) {
  return useQuery({
    queryKey: adminKeys.users,
    queryFn: () => api<UsersResponse>("/users"),
    enabled: canAdminister(session),
  });
}

// The response carries a one-use secret; gcTime 0 drops it once the form closes.
export function useCreateInvitation() {
  const client = useQueryClient();
  return useMutation({
    gcTime: 0,
    mutationFn: (body: { email: string; role: Role }) =>
      api<CreatedInvitation>("/users/invitations", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: adminKeys.users });
    },
  });
}

export function useRevokeInvitation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      api<Invitation>(`/users/invitations/${encodeURIComponent(id)}/revoke`, {
        method: "POST",
      }),
    onSettled: () => client.invalidateQueries({ queryKey: adminKeys.users }),
  });
}

export function useUpdateUser() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, changes }: { id: string; changes: UserChanges }) =>
      api<User>(`/users/${encodeURIComponent(id)}`, {
        method: "PATCH",
        body: JSON.stringify(changes),
      }),
    onSuccess: async (updated) => {
      client.setQueryData<UsersResponse>(
        adminKeys.users,
        (prev) =>
          prev && {
            ...prev,
            items: prev.items.map((item) =>
              item.id === updated.id ? updated : item,
            ),
          },
      );
      const self =
        client.getQueryData<Session | null>(["session"])?.user.id ===
        updated.id;
      if (self) {
        // Confirm your own access with the server; a 401 returns to sign-in.
        await client.invalidateQueries({ queryKey: ["session"] });
        if (!canAdminister(client.getQueryData<Session | null>(["session"]))) {
          client.removeQueries({ queryKey: adminKeys.all });
          return;
        }
      }
      await client.invalidateQueries({ queryKey: adminKeys.users });
    },
    onError: () => client.invalidateQueries({ queryKey: adminKeys.users }),
  });
}

const TOKEN = "[A-Za-z0-9_-]{40,100}";
const INVITE_LINK = new RegExp(`^/accept-invite#token=(${TOKEN})$`);
export const INVITE_FRAGMENT = new RegExp(`^#token=(${TOKEN})$`);

/** Builds the shareable link only from the exact server path shape; anything else is refused. */
export function absoluteInviteUrl(path: string): string | null {
  const match = INVITE_LINK.exec(path);
  return match
    ? `${window.location.origin}/accept-invite#token=${match[1]}`
    : null;
}

// The token travels only in request bodies, never in a URL or query key.
export const validateInvitation = (token: string) =>
  api<InvitationPreview>("/auth/invite/validate", {
    method: "POST",
    body: JSON.stringify({ token }),
  });

export function useAcceptInvitation() {
  return useMutation({
    gcTime: 0,
    retry: false,
    mutationFn: (body: { token: string; display_name: string; password: string }) =>
      api<Session>("/auth/invite/accept", {
        method: "POST",
        body: JSON.stringify(body),
      }),
  });
}
