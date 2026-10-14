import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { api } from "./api";

export type JsonRecord = Record<string, unknown>;
export type Bucket = "likely" | "review" | "low";
export type DecisionKind = "false_positive" | "confirmed_match" | "escalated";
export type Actor = { id: string; email: string; display_name: string };

export type Disposition = {
  id: string;
  hit_id: string;
  kind: DecisionKind | "auto_cleared";
  actor_user_id: string | null;
  actor: Actor | null;
  note: string;
  created_at: string;
  slider_value_at_time?: number | null;
};
export type FieldChange = { before: unknown; after: unknown };
export type WhatChanged = {
  party?: Record<string, FieldChange>;
  entry?: Record<string, FieldChange>;
};
export type PriorDisposition = Disposition & { what_changed: WhatChanged };

export type HitSummary = {
  id: string;
  run_id: string;
  party_id: string;
  csl_entry_id: string;
  party_hash: string;
  entry_hash: string;
  party_name: string | null;
  entry_name: string | null;
  source_list: string | null;
  det_score: number | null;
  model_prob: number | null;
  final_confidence: number;
  bucket: Bucket;
  status: string;
  created_at: string;
};
export type HitDetail = HitSummary & {
  party_snapshot: JsonRecord;
  entry_snapshot: JsonRecord;
  explanation: JsonRecord;
  source_url: string | null;
  dispositions: Disposition[];
  prior_disposition: PriorDisposition | null;
  suppression_disposition: Disposition | null;
  completed_disposition?: Disposition | null;
};
export type Paged<T> = { items: T[]; total: number };
export type DecisionInput = { hitId: string; kind: DecisionKind; note: string };
export type DecisionResult = {
  disposition: Disposition;
  observations_dispositioned: number;
  hit_id: string;
};

export type PartyStatus =
  | "clear"
  | "open_hit"
  | "confirmed_match"
  | "escalated"
  | "suppressed"
  | "deleted"
  | "unscreened";
export type SourceSummary = {
  id: string;
  kind: string;
  label: string;
  last_synced_at: string | null;
  record_count: number;
  last_error: string | null;
};
export type Party = {
  id: string;
  source_id: string;
  external_id: string | null;
  name: string;
  alt_names: string[] | null;
  party_type: string;
  role: "customer" | "vendor" | "both";
  address_lines: string[] | null;
  city: string | null;
  region: string | null;
  postal_code: string | null;
  country_iso2: string | null;
  record_hash: string;
  deleted_at: string | null;
  source: SourceSummary | null;
  status: PartyStatus;
  screened_current?: boolean;
  last_screened_at: string | null;
  suppressed_pairings: number;
  stale: boolean;
  auto_cleared: boolean;
};
export type Run = {
  id: string;
  trigger: string;
  csl_version_id: string | null;
  started_at: string;
  finished_at: string | null;
  parties_screened: number;
  hits_new: number;
  hits_suppressed: number;
  auto_cleared?: number | null;
  model_name: string | null;
  settings_snapshot: JsonRecord;
  model_fallbacks: unknown[];
};
export type PartyEvent = {
  run: Run;
  hits: HitDetail[];
  previously_cleared: { count: number; hits: HitDetail[] };
  zero_hits: boolean;
  party_hash: string | null;
  party_evidence?: { party_snapshot: JsonRecord; source_snapshot: JsonRecord; candidates_considered: number; hits_recorded: number; result_counts: JsonRecord } | null;
  purged_comparisons?: { certificate_id: string; certificate_sha256: string; hit: JsonRecord; dispositions: JsonRecord[] }[];
};
export type AuditEntry = {
  id: string;
  created_at: string;
  action: string;
  actor_user_id: string | null;
  actor: Actor | null;
  target_table: string;
  target_id: string | null;
  before: JsonRecord | null;
  after: JsonRecord | null;
};
export type PartyFileData = {
  party: Party;
  source: SourceSummary | null;
  events: PartyEvent[];
  audit_log: AuditEntry[];
};
export type SyncHealth = {
  id: string;
  label: string;
  last_synced_at: string | null;
  stale: boolean;
  last_error: string | null;
};
export type DashboardData = {
  open_items: number;
  likely_matches: number;
  auto_cleared_since_report: number;
  last_report_at: string | null;
  stale_parties: number;
  total_parties: number;
  parties_screened: number;
  coverage_latest_version: number | null;
  csl_version: {
    id: string;
    fetched_at: string;
    last_fetched_at?: string | null;
    source_list_stamps?: JsonRecord;
    entry_count: number;
    added: number;
    changed: number;
    removed: number;
  } | null;
  last_run: { id: string; finished_at: string | null } | null;
  sync_health: SyncHealth[];
};

export const PARTY_FILTER_KEYS = [
  "q",
  "role",
  "source_id",
  "country",
  "status",
  "stale",
  "auto_cleared",
  "include_deleted",
] as const;
export type PartyFilterKey = (typeof PARTY_FILTER_KEYS)[number];
export type PartyFilters = Partial<Record<PartyFilterKey, string>> & {
  offset: number;
};
export type AuditFilters = {
  action?: string;
  target_table?: string;
  target_id?: string;
  offset: number;
};

export const PAGE_SIZE = 100;

// Prefixes match the logout cleanup and cross-page invalidation.
export const reviewKeys = {
  all: ["review"] as const,
  queue: ["review", "queue"] as const,
  hit: (id: string) => ["review", "hit", id] as const,
};
export const partyKeys = {
  all: ["parties"] as const,
  list: (filters: PartyFilters) => ["parties", "list", filters] as const,
  file: (id: string) => ["parties", "file", id] as const,
};
export const runKeys = {
  all: ["runs"] as const,
  list: (offset: number) => ["runs", "list", offset] as const,
};
export const auditKeys = {
  all: ["audit"] as const,
  list: (filters: AuditFilters) => ["audit", "list", filters] as const,
};
export const dashboardKeys = { all: ["dashboard"] as const };

export function toQuery(params: Record<string, string | number | undefined>) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params))
    if (value !== undefined && value !== "") search.set(key, String(value));
  const text = search.toString();
  return text ? `?${text}` : "";
}

const segment = encodeURIComponent;
export const fetchQueue = () => api<Paged<HitSummary>>("/queue");
export const fetchHit = (id: string) => api<HitDetail>(`/hits/${segment(id)}`);
export const postDecision = ({ hitId, kind, note }: DecisionInput) =>
  api<DecisionResult>(`/hits/${segment(hitId)}/dispositions`, {
    method: "POST",
    body: JSON.stringify({ kind, note }),
  });
export const fetchParties = (filters: PartyFilters) =>
  api<Paged<Party>>(`/parties${toQuery({ ...filters, limit: PAGE_SIZE })}`);
export const fetchPartyFile = (id: string) =>
  api<PartyFileData>(`/parties/${segment(id)}`);
export const fetchRuns = (offset: number) =>
  api<Paged<Run>>(`/runs${toQuery({ offset, limit: PAGE_SIZE })}`);
export const fetchAuditLog = (filters: AuditFilters) =>
  api<Paged<AuditEntry>>(
    `/audit-log${toQuery({ ...filters, limit: PAGE_SIZE })}`,
  );
export const fetchDashboard = () => api<DashboardData>("/dashboard");

export function useQueue() {
  return useQuery({ queryKey: reviewKeys.queue, queryFn: fetchQueue, retry: false });
}
export function useHit(id: string | null) {
  return useQuery({
    queryKey: reviewKeys.hit(id ?? ""),
    queryFn: () => fetchHit(id ?? ""),
    enabled: Boolean(id),
    retry: false,
  });
}
export function useDecision() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: postDecision,
    retry: false,
    // Not returned: the decision settles without waiting for every refetch.
    onSettled: () => {
      for (const queryKey of [
        reviewKeys.all,
        partyKeys.all,
        runKeys.all,
        auditKeys.all,
        dashboardKeys.all,
      ])
        void client.invalidateQueries({ queryKey });
    },
  });
}
export function useParties(filters: PartyFilters) {
  return useQuery({
    queryKey: partyKeys.list(filters),
    queryFn: () => fetchParties(filters),
    placeholderData: keepPreviousData,
    retry: false,
  });
}
export function usePartyFile(id: string) {
  return useQuery({
    queryKey: partyKeys.file(id),
    queryFn: () => fetchPartyFile(id),
    retry: false,
  });
}
export function useRuns(offset: number) {
  return useQuery({
    queryKey: runKeys.list(offset),
    queryFn: () => fetchRuns(offset),
    placeholderData: keepPreviousData,
    retry: false,
  });
}
export function useAuditLog(filters: AuditFilters) {
  return useQuery({
    queryKey: auditKeys.list(filters),
    queryFn: () => fetchAuditLog(filters),
    placeholderData: keepPreviousData,
    retry: false,
  });
}
export function useDashboard() {
  return useQuery({
    queryKey: dashboardKeys.all,
    queryFn: fetchDashboard,
    retry: false,
  });
}

export function asRecord(value: unknown): JsonRecord {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : {};
}
export function strings(value: unknown): string[] {
  if (typeof value === "string") return value.trim() ? [value] : [];
  if (typeof value === "number") return [String(value)];
  return Array.isArray(value) ? value.flatMap(strings) : [];
}
const unique = (values: string[]) => [...new Set(values)];
const num = (value: unknown) =>
  typeof value === "number" && Number.isFinite(value) ? value : null;

export function formatScore(value: number | null | undefined) {
  return value == null
    ? "Not recorded"
    : value.toLocaleString(undefined, { maximumFractionDigits: 1 });
}
export function formatProbability(value: number | null) {
  return value == null
    ? "Not used"
    : `${(value * 100).toLocaleString(undefined, { maximumFractionDigits: 1 })}%`;
}

const KIND_LABELS: Record<string, string> = {
  false_positive: "Cleared as false positive",
  confirmed_match: "Confirmed match",
  escalated: "Escalated",
  auto_cleared: "Auto-cleared",
};
const BUCKET_LABELS: Record<string, string> = {
  likely: "Likely match",
  review: "Needs review",
  low: "Low",
};
const FIELD_LABELS: Record<string, string> = {
  alt_names: "Alternate names",
  address_lines: "Address",
  country_iso2: "Country",
  postal_code: "Postal code",
  party_type: "Party type",
  entity_type: "Listing type",
  external_id: "External ID",
  record_hash: "Record hash",
  entry_hash: "Listing hash",
};
export function humanize(key: string) {
  const label = FIELD_LABELS[key] ?? key.replace(/[_.]+/g, " ");
  return label.charAt(0).toUpperCase() + label.slice(1);
}
export const kindLabel = (kind: string) => KIND_LABELS[kind] ?? humanize(kind);
export const bucketLabel = (bucket: string) =>
  BUCKET_LABELS[bucket] ?? humanize(bucket);

export function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "Empty";
  if (Array.isArray(value))
    return value.length ? value.map(displayValue).join("; ") : "Empty";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

// Only absolute http(s) links without embedded credentials are ever opened.
export function safeHttpUrl(value: unknown): string | null {
  if (typeof value !== "string") return null;
  try {
    const url = new URL(value);
    return (url.protocol === "http:" || url.protocol === "https:") &&
      !url.username &&
      !url.password
      ? url.href
      : null;
  } catch {
    return null;
  }
}

export const normalizeToken = (value: string) =>
  value.normalize("NFKD").replace(/\p{M}/gu, "").toLowerCase();

export function matchedTokens(explanation: JsonRecord) {
  const raw = asRecord(explanation.matched_tokens);
  const normalized = asRecord(explanation.normalized);
  const shared = strings(raw.name);
  const side = (key: "party" | "entry") => {
    const tokens = new Set([...strings(raw[key]), ...shared].map(normalizeToken));
    const evidence = asRecord(normalized[key]);
    const native = typeof evidence.unicode === "string" ? evidence.unicode.split(/\s+/).filter(Boolean) : [];
    const ascii = typeof evidence.ascii === "string" ? evidence.ascii.split(/\s+/).filter(Boolean) : [];
    // Align only the engine's recorded representations; never transliterate or score here.
    if (native.length === ascii.length) {
      native.forEach((word, index) => {
        if (tokens.has(normalizeToken(ascii[index]))) tokens.add(normalizeToken(word));
      });
    } else if (native.length === 1 && ascii.length && ascii.every(word => tokens.has(normalizeToken(word)))) {
      tokens.add(normalizeToken(native[0]));
    }
    return tokens;
  };
  return { party: side("party"), entry: side("entry"), address: new Set(strings(raw.address).map(normalizeToken)) };
}

export type ComparisonRow = {
  key: string;
  label: string;
  party: string[];
  entry: string[];
  names?: boolean;
};
export function comparisonRows(party: JsonRecord, entry: JsonRecord): ComparisonRow[] {
  const addresses = Array.isArray(entry.addresses) ? entry.addresses.map(asRecord) : [];
  const listed = (...keys: string[]) =>
    unique(addresses.flatMap((address) => keys.flatMap((key) => strings(address[key]))));
  return [
    { key: "name", label: "Name", party: strings(party.name), entry: strings(entry.name), names: true },
    {
      key: "alt_names",
      label: "Alternate names",
      party: strings(party.alt_names),
      entry: strings(entry.alt_names),
      names: true,
    },
    { key: "address", label: "Address", party: strings(party.address_lines), entry: listed("address") },
    { key: "city", label: "City", party: strings(party.city), entry: listed("city") },
    { key: "region", label: "Region", party: strings(party.region), entry: listed("state", "region") },
    { key: "postal", label: "Postal code", party: strings(party.postal_code), entry: listed("postal_code") },
    {
      key: "country",
      label: "Country",
      party: strings(party.country_iso2),
      entry: unique([...strings(entry.countries), ...listed("country")]),
    },
    { key: "type", label: "Type", party: strings(party.party_type), entry: strings(entry.entity_type) },
  ];
}

const ADJUSTMENTS: Record<string, string> = {
  country: "Country adjustment", city: "City adjustment", address: "Address adjustment",
  entity_type: "Entity type adjustment", short_name: "Short name adjustment",
};
export function reasons(explanation: JsonRecord): string[] {
  const components = asRecord(explanation.components);
  const aliases = asRecord(explanation.best_aliases);
  const result: string[] = [];
  const name = num(components.name);
  if (name !== null)
    result.push(
      typeof aliases.party === "string" && typeof aliases.entry === "string"
        ? `Names compare at ${formatScore(name)} of 100 (“${aliases.party}” with “${aliases.entry}”).`
        : `Names compare at ${formatScore(name)} of 100.`,
    );
  const adjustments = Object.entries(ADJUSTMENTS)
    .map(([key, labels]) => ({ labels, value: num(components[key]) ?? 0 }))
    .filter((item) => item.value !== 0)
    .sort((a, b) => Math.abs(b.value) - Math.abs(a.value))
    .slice(0, 2);
  for (const { labels, value } of adjustments)
    result.push(
      `${labels} (${value > 0 ? "+" : "−"}${formatScore(Math.abs(value))}).`,
    );
  if (!adjustments.length)
    result.push("No country, city, address, or type adjustment was applied.");
  return result;
}

export function age(value: string, now = Date.now()) {
  const time = Date.parse(value);
  if (Number.isNaN(time)) return value;
  const format = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  const minutes = Math.round((now - time) / 60000);
  if (Math.abs(minutes) < 60) return format.format(-minutes, "minute");
  const hours = Math.round(minutes / 60);
  if (Math.abs(hours) < 48) return format.format(-hours, "hour");
  return format.format(-Math.round(hours / 24), "day");
}

export function duration(start: string, end: string | null) {
  if (!end) return null;
  const ms = Date.parse(end) - Date.parse(start);
  if (!Number.isFinite(ms) || ms < 0) return null;
  if (ms < 1000) return `${ms} ms`;
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  return minutes < 60
    ? `${minutes} min ${seconds % 60} s`
    : `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}
