import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";
import { api, type Session } from "./api";
import { canAdminister } from "./adminApi";

export type HoldKind = "government" | "other";
/** The server's actor snapshot; null means the identity was not recorded. */
export type ActorSnapshot =
  | string
  | { display_name?: string; email?: string }
  | null;
export type Hold = {
  id: string;
  party_id: string | null;
  kind: HoldKind;
  reason: string;
  actor: ActorSnapshot;
  created_at?: string;
};
export type HoldRelease = {
  id: string;
  hold_id: string;
  reason: string;
  authorization_reference: string | null;
  actor: ActorSnapshot;
  created_at: string;
};
export type Certificate = {
  id: string;
  party_id: string;
  sha256: string;
  created_at?: string;
};
export type RetentionStatus = {
  enabled: boolean;
  years: number | null;
  minimum_years: number;
  inactive_parties: number;
  active_holds: Hold[];
  certificates: Certificate[];
  permanently_retained: string[];
};
export type EligibilityInput = {
  event_date: string;
  external_dates_considered: boolean;
  government_holds_excluded: boolean;
};
export type Attestation = EligibilityInput & {
  id: string;
  actor: ActorSnapshot;
  created_at: string;
};
export type Eligibility = {
  party_id: string;
  eligible: boolean;
  reasons: string[];
  years: number | null;
  basis_at: string | null;
  eligible_on: string | null;
  eligible_at: string | null;
  observed_activity_at?: string | null;
  latest_attested_event_date?: string | null;
  attestation: Attestation | null;
  holds: Hold[];
};
export type HoldInput = { party_id: string | null; kind: HoldKind; reason: string };
export type ReleaseInput = { reason: string; authorization_reference?: string };
export type RetentionPlan = {
  enabled: boolean;
  years: number | null;
  as_of: string;
  bundles: {
    party_id: string;
    attestation_id: string;
    basis_at: string;
    eligible_on: string;
    eligible_at: string;
    hit_ids: string[];
    disposition_ids: string[];
  }[];
  blocked: { party_id: string; hit_id?: string; reasons: string[] }[];
  hits: number;
  dispositions: number;
};
export type RetentionResult = RetentionPlan & {
  applied: true;
  certificates: Certificate[];
};

export const retentionKeys = {
  all: ["retention"] as const,
  status: ["retention", "status"] as const,
  eligibility: (partyId: string) => ["retention", "eligibility", partyId] as const,
};

const own = (table: Record<string, string>, key: string) =>
  Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined;

const REASONS: Record<string, string> = {
  retention_disabled: "Retention is off for this workspace, so nothing is purged.",
  active_party: "The party is still active; active parties are protected.",
  external_event_date_unproven:
    "The latest external event dates have not been attested as considered.",
  eligibility_not_attested: "No eligibility attestation has been recorded for this party.",
  active_hold: "An active hold protects this party.",
  attestation_predates_activity: "A new attestation is required after the latest party activity.",
  within_retention_period:
    "The retention period has not yet passed since the latest event or party activity.",
};
export const describeReason = (code: string) =>
  own(REASONS, code) ?? `Unrecognized reason "${code}"; the evidence stays protected.`;

const RETAINED: Record<string, string> = {
  audit_log: "The audit log of every recorded action",
  parties: "Party records",
  screening_runs: "Screening run records",
  run_party_evidence: "Per-run party evidence",
  csl_entry_revisions: "Screening list entry revisions",
  retention_attestations: "Retention attestations",
  retention_holds: "Retention holds",
  retention_hold_releases: "Hold releases",
  retention_purge_certificates: "Purge certificates",
};
export const describeRetained = (table: string) => own(RETAINED, table) ?? table;

// Query families (owned by other packages) that reflect holds, attestations or purges.
const CHANGED_BY_HOLDS = new Set(["retention", "party", "parties", "audit"]);
const CHANGED_BY_APPLY = new Set([...CHANGED_BY_HOLDS, "runs", "dashboard"]);
const refresh = (client: QueryClient, families: Set<string>) =>
  client.invalidateQueries({
    predicate: (query) => families.has(String(query.queryKey[0])),
  });

export function useRetentionStatus(session: Session) {
  return useQuery({
    queryKey: retentionKeys.status,
    queryFn: () => api<RetentionStatus>("/retention/status"),
    enabled: canAdminister(session),
    retry: false,
  });
}

export function useEligibility(session: Session, partyId: string) {
  return useQuery({
    queryKey: retentionKeys.eligibility(partyId),
    queryFn: () =>
      api<Eligibility>(`/retention/parties/${encodeURIComponent(partyId)}/eligibility`),
    enabled: canAdminister(session) && Boolean(partyId),
    retry: false,
  });
}

export function useAttest(partyId: string) {
  const client = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: (values: EligibilityInput) =>
      api<Eligibility>(`/retention/parties/${encodeURIComponent(partyId)}/eligibility`, {
        method: "PATCH",
        body: JSON.stringify(values),
      }),
    onSuccess: (saved) => {
      client.setQueryData(retentionKeys.eligibility(partyId), saved);
      void refresh(client, CHANGED_BY_HOLDS);
    },
  });
}

export function usePlaceHold() {
  const client = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: (values: HoldInput) =>
      api<Hold>("/retention/holds", { method: "POST", body: JSON.stringify(values) }),
    onSuccess: () => refresh(client, CHANGED_BY_HOLDS),
  });
}

export function useReleaseHold() {
  const client = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: ({ holdId, values }: { holdId: string; values: ReleaseInput }) =>
      api<HoldRelease>(`/retention/holds/${encodeURIComponent(holdId)}/release`, {
        method: "POST",
        body: JSON.stringify(values),
      }),
    onSuccess: () => refresh(client, CHANGED_BY_HOLDS),
  });
}

export function usePreviewRetention() {
  return useMutation({
    retry: false,
    mutationFn: () => api<RetentionPlan>("/retention/preview", { method: "POST" }),
  });
}

// The outcome of a failed apply is unknown, so dependent views refresh either way.
export function useApplyRetention() {
  const client = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: () => api<RetentionResult>("/retention/apply", { method: "POST" }),
    onSettled: () => refresh(client, CHANGED_BY_APPLY),
  });
}
