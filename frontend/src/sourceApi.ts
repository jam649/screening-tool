import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./api";
import { cslKeys } from "./csl";

export type SourceHealth = "not_synced" | "healthy" | "stale" | "error";
export type ColumnMapping = Record<string, string | string[]>;

export type Source = {
  id: string;
  kind: string;
  label: string;
  record_count: number;
  last_synced_at: string | null;
  next_sync_at: string | null;
  health: SourceHealth;
  last_error: string | null;
  column_mapping: ColumnMapping;
};

export type SourcePreview = {
  headers: string[];
  rows: Record<string, string>[];
  total_rows: number;
  stable_id_warning: string;
  column_mapping: ColumnMapping;
  source_id: string | null;
};

export type RejectedRow = { row_number: number; reason: string };

export type UploadResult = {
  source_id: string;
  added: number;
  changed: number;
  unchanged: number;
  deleted: number;
  rejected: RejectedRow[];
  rejected_count: number;
  omitted_rejection_details: number;
  task_ids: string[];
  applied: boolean;
  source: Source;
  screening_run_ids: string[];
  screening_pending?: boolean;
  screening_errors?: { task_id: string; attempts: number; error: string }[];
};

export type UploadInput = {
  sourceId: string;
  file: File;
  mapping: ColumnMapping;
};

export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
export const DEMO_CSV_URL = "/api/v1/sources/demo.csv";

export const sourceKeys = {
  all: ["sources"] as const,
  list: ["sources", "list"] as const,
};

export const fetchSources = () => api<Source[]>("/sources");
export const createSource = (label: string) =>
  api<Source>("/sources", {
    method: "POST",
    body: JSON.stringify({ kind: "csv", label }),
  });

// FormData bodies: the api helper leaves Content-Type unset so the browser
// supplies the multipart boundary, and still adds the CSRF header.
export function previewSourceFile({
  file,
  sourceId,
}: {
  file: File;
  sourceId: string;
}) {
  const body = new FormData();
  body.append("file", file);
  body.append("source_id", sourceId);
  return api<SourcePreview>("/sources/preview", { method: "POST", body });
}

export function uploadSourceFile({ sourceId, file, mapping }: UploadInput) {
  const body = new FormData();
  body.append("file", file);
  body.append("mapping", JSON.stringify(mapping));
  return api<UploadResult>(
    `/sources/${encodeURIComponent(sourceId)}/upload`,
    { method: "POST", body },
  );
}

export function validateUploadFile(file: File): string | null {
  const extension = /\.[^.]+$/.exec(file.name.toLowerCase())?.[0];
  if (extension !== ".csv" && extension !== ".xlsx")
    return "Choose a .csv or .xlsx file.";
  if (file.size === 0)
    return "This file is empty. Choose a file with a header row.";
  if (file.size > MAX_UPLOAD_BYTES)
    return "This file is larger than 10 MiB. Remove unused columns or split it, then try again.";
  return null;
}

export function useSources({ enabled }: { enabled: boolean }) {
  return useQuery({
    queryKey: sourceKeys.list,
    queryFn: fetchSources,
    enabled,
    retry: false,
  });
}

function withSource(list: Source[] | undefined, source: Source) {
  if (!list) return list;
  return list.some((item) => item.id === source.id)
    ? list.map((item) => (item.id === source.id ? source : item))
    : [...list, source];
}

export function useCreateSource() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: createSource,
    retry: false,
    onSuccess: (source) => {
      client.setQueryData<Source[]>(sourceKeys.list, (list) =>
        withSource(list, source),
      );
    },
    // Not returned: the mutation should settle without waiting for the refetch.
    onSettled: () => {
      void client.invalidateQueries({ queryKey: sourceKeys.all });
    },
  });
}

export function usePreviewSource() {
  return useMutation({ mutationFn: previewSourceFile, retry: false });
}

export function useUploadSource() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: uploadSourceFile,
    retry: false,
    onSuccess: (result) => {
      // The response carries the server's view of the source, applied or not,
      // so cards update even before the list refetch returns.
      client.setQueryData<Source[]>(sourceKeys.list, (list) =>
        withSource(list, result.source),
      );
      // Applied imports can queue screening, which changes CSL pending counts.
      if (result.applied) {
        void client.invalidateQueries({ queryKey: cslKeys.all });
        for (const family of ["dashboard", "review", "parties", "runs", "audit"])
          void client.invalidateQueries({ queryKey: [family] });
      }
    },
    onSettled: () => {
      void client.invalidateQueries({ queryKey: sourceKeys.all });
    },
  });
}

export type SingleField =
  | "name"
  | "external_id"
  | "party_type"
  | "role"
  | "city"
  | "region"
  | "postal_code"
  | "country";
export type MultiField = "address_lines" | "alt_names";
export type MappingDraft = Record<SingleField, string> &
  Record<MultiField, string[]>;

export const SINGLE_FIELDS: {
  field: SingleField;
  label: string;
  help?: string;
}[] = [
  {
    field: "name",
    label: "Name",
    help: "Required. The customer or vendor name used for screening.",
  },
  {
    field: "external_id",
    label: "External ID",
    help: "Recommended. A customer or vendor number that stays the same between uploads.",
  },
  {
    field: "party_type",
    label: "Party type",
    help: "If not mapped, every row is a company. Accepted values: company or individual (entity and person also work).",
  },
  {
    field: "role",
    label: "Role",
    help: "If not mapped, every row is a customer. Accepted values: customer, vendor, or both.",
  },
  { field: "city", label: "City" },
  { field: "region", label: "Region or state" },
  { field: "postal_code", label: "Postal code" },
  {
    field: "country",
    label: "Country",
    help: "Country names and codes are interpreted by the server during import.",
  },
];

const MULTI_FIELDS: MultiField[] = ["address_lines", "alt_names"];

// Compared after lowercasing and removing spaces, underscores, hyphens and dots.
const SINGLE_GUESSES: Record<SingleField, string[]> = {
  name: [
    "name",
    "partyname",
    "legalname",
    "companyname",
    "customername",
    "vendorname",
    "businessname",
    "displayname",
    "fullname",
  ],
  external_id: [
    "externalid",
    "id",
    "customerid",
    "vendorid",
    "partyid",
    "accountid",
    "accountnumber",
    "customernumber",
    "vendornumber",
  ],
  party_type: ["partytype", "type", "entitytype"],
  role: ["role", "partyrole", "relationship"],
  city: ["city", "town"],
  region: ["region", "state", "province", "stateprovince"],
  postal_code: ["postalcode", "postcode", "zip", "zipcode"],
  country: ["country", "countrycode", "countryname"],
};
const MULTI_GUESSES: Record<MultiField, RegExp> = {
  address_lines: /^(address(?:lines?)?|street(?:address)?)\d?$/,
  alt_names: /^(altnames?|alias(es)?|aka|alternatenames?|dba)$/,
};

const compact = (value: string) => value.toLowerCase().replace(/[\s_.-]+/g, "");

export function emptyMapping(): MappingDraft {
  return {
    name: "",
    external_id: "",
    party_type: "",
    role: "",
    city: "",
    region: "",
    postal_code: "",
    country: "",
    address_lines: [],
    alt_names: [],
  };
}

export function initialMapping(
  headers: string[],
  saved: ColumnMapping | null | undefined,
): MappingDraft {
  const draft = emptyMapping();
  const present = new Set(headers);
  const used = new Set<string>();
  const keep = (column: string) => {
    used.add(column);
    return column;
  };
  // A saved mapping wins wherever its columns still exist in this file.
  for (const { field } of SINGLE_FIELDS) {
    const value = saved?.[field];
    if (typeof value === "string" && present.has(value))
      draft[field] = keep(value);
  }
  for (const field of MULTI_FIELDS) {
    const value = saved?.[field];
    const columns =
      typeof value === "string" ? [value] : Array.isArray(value) ? value : [];
    draft[field] = [...new Set(columns)]
      .filter((column) => typeof column === "string" && present.has(column))
      .map(keep);
  }
  for (const { field } of SINGLE_FIELDS) {
    if (draft[field]) continue;
    for (const candidate of SINGLE_GUESSES[field]) {
      const match = headers.find(
        (header) => !used.has(header) && compact(header) === candidate,
      );
      if (match) {
        draft[field] = keep(match);
        break;
      }
    }
  }
  for (const field of MULTI_FIELDS) {
    if (draft[field].length) continue;
    draft[field] = headers
      .filter(
        (header) =>
          !used.has(header) && MULTI_GUESSES[field].test(compact(header)),
      )
      .map(keep);
  }
  return draft;
}

export function toColumnMapping(draft: MappingDraft): ColumnMapping {
  const mapping: ColumnMapping = {};
  for (const [field, value] of Object.entries(draft))
    if (value.length) mapping[field] = value;
  return mapping;
}
