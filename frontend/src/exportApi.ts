import { ApiError } from "./api";
import { PARTY_FILTER_KEYS, type PartyFilters } from "./reviewApi";

export type ExportFormat = "pdf" | "csv";
type ExportKind = ExportFormat | "zip";
export type ExportResult = {
  filename: string;
  bytes: number;
  serverSha256: string | null;
};

const MIME: Record<ExportKind, string> = {
  pdf: "application/pdf",
  csv: "text/csv",
  zip: "application/zip",
};
// Long enough for the browser to start reading the blob after the click.
const REVOKE_AFTER_MS = 30_000;

export function partyExportPath(id: string, format: ExportFormat) {
  return `/parties/${encodeURIComponent(id)}/export?format=${format}`;
}

export function runExportPath(id: string, format: ExportFormat) {
  return `/runs/${encodeURIComponent(id)}/export?format=${format}`;
}

// Only list filter keys are sent; offset and limit are never part of an export.
export function partyViewExportPath(filters: Partial<PartyFilters>) {
  const params = new URLSearchParams();
  for (const key of PARTY_FILTER_KEYS) {
    const value = filters[key];
    if (typeof value === "string" && value !== "") params.set(key, value);
  }
  const query = params.toString();
  return `/exports/parties.csv${query ? `?${query}` : ""}`;
}

export const FULL_AUDIT_EXPORT_PATH = "/exports/full.zip";

function idPart(id: string) {
  return id.replace(/[^A-Za-z0-9_-]/g, "").slice(0, 64) || "record";
}

function headerFilename(header: string | null): string | null {
  if (!header) return null;
  const extended = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(header);
  if (extended) {
    try {
      return decodeURIComponent(extended[1].trim().replace(/^"|"$/g, ""));
    } catch {
      // Malformed encoding; fall back to the plain parameter.
    }
  }
  const plain = /filename\s*=\s*("((?:[^"\\]|\\.)*)"|[^;]+)/i.exec(header);
  if (plain) return (plain[2] ?? plain[1]).replace(/\\(.)/g, "$1").trim();
  return null;
}

export function safeFilename(header: string | null, ext: ExportKind, fallback: string) {
  const cleaned = (headerFilename(header) ?? "")
    .replace(/[\u0000-\u001f\u007f-\u009f]/g, "")
    .replace(/[\\/<>:"|?*]/g, "_")
    .replace(/^[\s.]+|[\s.]+$/g, "")
    .slice(0, 150);
  if (!cleaned) return fallback;
  return cleaned.toLowerCase().endsWith(`.${ext}`) ? cleaned : `${cleaned}.${ext}`;
}

async function errorMessage(response: Response) {
  const payload = await response.json().catch(() => null);
  const detail = payload?.detail;
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail))
    return detail
      .map((item: { msg?: string }) => item?.msg ?? "Invalid value")
      .join(". ");
  return `The export could not be prepared (${response.status}).`;
}

function startDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.hidden = true;
  document.body.append(anchor);
  try {
    anchor.click();
  } finally {
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), REVOKE_AFTER_MS);
  }
}

async function downloadExport(
  path: string,
  kind: ExportKind,
  fallback: string,
): Promise<ExportResult> {
  let response: Response;
  try {
    response = await fetch(`/api/v1${path}`, {
      method: "GET",
      credentials: "same-origin",
      cache: "no-store",
      headers: { Accept: `${MIME[kind]}, application/json` },
    });
  } catch {
    throw new ApiError(0, "Unable to reach the server. Check your connection and try again.");
  }
  if (!response.ok) throw new ApiError(response.status, await errorMessage(response));
  const type = (response.headers.get("Content-Type") ?? "").split(";")[0].trim().toLowerCase();
  if (type !== MIME[kind])
    throw new ApiError(
      response.status,
      "The server returned an unexpected file type, so no download was started.",
    );
  let data: Blob;
  try {
    data = await response.blob();
  } catch {
    throw new ApiError(0, "The file was interrupted before it was received. Try again.");
  }
  if (data.size === 0)
    throw new ApiError(response.status, "The server returned an empty file, so no download was started.");
  const filename = safeFilename(response.headers.get("Content-Disposition"), kind, fallback);
  startDownload(new Blob([data], { type: MIME[kind] }), filename);
  const digest = response.headers.get("X-Export-SHA256")?.trim().toLowerCase() ?? "";
  return {
    filename,
    bytes: data.size,
    serverSha256: /^[0-9a-f]{64}$/.test(digest) ? digest : null,
  };
}

export const exportParty = (id: string, format: ExportFormat) =>
  downloadExport(partyExportPath(id, format), format, `party-${idPart(id)}.${format}`);

export const exportRun = (id: string, format: ExportFormat) =>
  downloadExport(runExportPath(id, format), format, `run-${idPart(id)}.${format}`);

export const exportPartyView = (filters: Partial<PartyFilters>) =>
  downloadExport(partyViewExportPath(filters), "csv", "parties-current-view.csv");

export const exportFullAudit = () =>
  downloadExport(FULL_AUDIT_EXPORT_PATH, "zip", "full-audit.zip");
