import { useEffect, useRef } from "react";
import {
  useIsMutating,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { ApiError, api, type Session } from "./api";

export type CSLSourceStamps = Record<string, unknown>;

export type CSLVersion = {
  id: string;
  fetched_at: string;
  file_sha256: string;
  semantic_sha256: string;
  entry_count: number;
  added: number;
  changed: number;
  removed: number;
  source_list_stamps: CSLSourceStamps;
};

export type CSLFetchStatus = "fetching" | "success" | "failed";

export type CSLFetch = {
  id: string;
  fetched_at: string;
  completed_at: string | null;
  status: CSLFetchStatus;
  version_id: string | null;
  file_sha256: string | null;
  error: string | null;
  source_list_stamps: CSLSourceStamps;
  warnings: string[];
};

export type CSLStatus = {
  current_version: CSLVersion | null;
  last_fetch: CSLFetch | null;
  pending_screenings: number;
};

export type CSLRefreshResult = CSLStatus & {
  fetch_id: string;
  changed: boolean;
  pending_task_id: string | null;
  closed_hits: number;
};

export const CSL_VERSION_LIMIT = 20;
export const CSL_POLL_INTERVAL_MS = 2000;

export const cslKeys = {
  all: ["csl"] as const,
  status: ["csl", "status"] as const,
  versions: ["csl", "versions", CSL_VERSION_LIMIT] as const,
  refresh: ["csl", "refresh"] as const,
};

export function isAdminSession(session: Session | null | undefined): boolean {
  return session?.user.role.toLowerCase() === "admin";
}

export const fetchCSLStatus = () => api<CSLStatus>("/csl/status");
export const fetchCSLVersions = () =>
  api<CSLVersion[]>(`/csl/versions?limit=${CSL_VERSION_LIMIT}`);
// No body: the api helper adds the CSRF header for non-GET requests.
export const refreshCSL = () =>
  api<CSLRefreshResult>("/csl/refresh", { method: "POST" });

function statusFrom(result: CSLRefreshResult): CSLStatus {
  return {
    current_version: result.current_version,
    last_fetch: result.last_fetch,
    pending_screenings: result.pending_screenings,
  };
}

export function useCSL({
  enabled,
  withVersions = false,
}: {
  enabled: boolean;
  withVersions?: boolean;
}) {
  const client = useQueryClient();
  // Shared across components so a refresh started on one page keeps others busy.
  const refreshing = useIsMutating({ mutationKey: cslKeys.refresh }) > 0;
  const status = useQuery({
    queryKey: cslKeys.status,
    queryFn: fetchCSLStatus,
    enabled,
    retry: false,
    refetchInterval: (query) =>
      enabled &&
      (refreshing || query.state.data?.last_fetch?.status === "fetching")
        ? CSL_POLL_INTERVAL_MS
        : false,
  });
  const versions = useQuery({
    queryKey: cslKeys.versions,
    queryFn: fetchCSLVersions,
    enabled: enabled && withVersions,
    retry: false,
  });
  const refresh = useMutation({
    mutationKey: cslKeys.refresh,
    mutationFn: refreshCSL,
    retry: false,
    onSuccess: (result) => {
      client.setQueryData<CSLStatus>(cslKeys.status, statusFrom(result));
    },
    // Failed attempts are persisted server-side, so reload after either outcome.
    onSettled: () => {
      for (const family of ["dashboard", "review", "parties", "runs", "audit"])
        void client.invalidateQueries({ queryKey: [family] });
      return client.invalidateQueries({ queryKey: cslKeys.all });
    },
  });

  // A fetch started elsewhere may have saved a version once polling sees it finish.
  const lastFetchStatus = status.data?.last_fetch?.status;
  const previousFetchStatus = useRef(lastFetchStatus);
  useEffect(() => {
    if (
      previousFetchStatus.current === "fetching" &&
      lastFetchStatus !== "fetching"
    )
      void client.invalidateQueries({ queryKey: cslKeys.versions });
    previousFetchStatus.current = lastFetchStatus;
  }, [client, lastFetchStatus]);

  const busy =
    refreshing || refresh.isPending || lastFetchStatus === "fetching";
  return { status, versions, refresh, busy };
}

export function describeRefreshError(error: unknown): {
  title: string;
  detail: string;
} {
  const detail =
    error instanceof Error
      ? error.message
      : "Something went wrong. Please try again.";
  if (error instanceof ApiError) {
    if (error.status === 409)
      return { title: "Another list download is already running.", detail };
    if (error.status === 422)
      return { title: "The downloaded list was not accepted.", detail };
    if (error.status === 502)
      return { title: "The list source could not be reached.", detail };
  }
  return { title: "The list could not be refreshed.", detail };
}

export function formatCount(value: number): string {
  return value.toLocaleString();
}

export function describeChanges(version: CSLVersion): string {
  return `+${formatCount(version.added)} added, ~${formatCount(version.changed)} changed, −${formatCount(version.removed)} removed`;
}

export type SourceStamp = {
  key: string;
  source: string | null;
  lastImported: string | null;
  sourceLastUpdated: string | null;
  importRate: string | null;
};

export type ParsedSourceStamps = {
  lists: SourceStamp[];
  // null when the stamps have no sources_api key at all.
  api: SourceStamp[] | null;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function text(value: unknown): string | null {
  if (typeof value === "string") return value.trim() || null;
  if (typeof value === "number" || typeof value === "boolean")
    return String(value);
  return null;
}

function toStamp(key: string, value: unknown): SourceStamp {
  const record = isRecord(value) ? value : {};
  return {
    key,
    source: text(record.source),
    lastImported: text(record.last_imported),
    sourceLastUpdated: text(record.source_last_updated),
    importRate: text(record.import_rate),
  };
}

export function parseSourceStamps(stamps: unknown): ParsedSourceStamps {
  if (!isRecord(stamps)) return { lists: [], api: null };
  const lists = Object.entries(stamps)
    .filter(([key]) => key !== "sources_api")
    .map(([key, value]) => toStamp(key, value));
  if (!("sources_api" in stamps)) return { lists, api: null };
  const nested = stamps.sources_api;
  return {
    lists,
    api: isRecord(nested)
      ? Object.entries(nested).map(([key, value]) => toStamp(key, value))
      : [],
  };
}
