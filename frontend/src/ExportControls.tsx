import { useRef } from "react";
import { useMutation } from "@tanstack/react-query";
import { ApiError } from "./api";
import {
  exportFullAudit,
  exportParty,
  exportPartyView,
  exportRun,
  type ExportResult,
} from "./exportApi";
import type { PartyFilters } from "./reviewApi";
import "./admin.css";

type ExportAction = { label: string; run: () => Promise<ExportResult> };

function failureMessage(error: unknown) {
  return error instanceof ApiError
    ? error.message
    : "The export could not be prepared. Try again.";
}

function ExportGroup({
  label,
  actions,
  note,
}: {
  label: string;
  actions: ExportAction[];
  note?: string;
}) {
  const mutation = useMutation({
    mutationFn: (action: ExportAction) => action.run(),
    retry: false,
  });
  // Guards against a second click landing before the disabled state renders.
  const inFlight = useRef(false);
  const last = mutation.variables;

  function start(action: ExportAction) {
    if (inFlight.current) return;
    inFlight.current = true;
    mutation.mutate(action, {
      onSettled: () => {
        inFlight.current = false;
      },
    });
  }

  return (
    <div role="group" aria-label={label}>
      <div className="admin-actions">
        {actions.map((action) => (
          <button
            key={action.label}
            type="button"
            className="button secondary admin-compact"
            disabled={mutation.isPending}
            aria-busy={mutation.isPending && last?.label === action.label}
            onClick={() => start(action)}
          >
            {action.label}
          </button>
        ))}
      </div>
      {note && <p className="admin-muted">{note}</p>}
      <p className="admin-status" role="status">
        {mutation.isPending
          ? "Preparing download…"
          : mutation.isSuccess
            ? `Download started: ${mutation.data.filename}`
            : ""}
      </p>
      {mutation.isError && (
        <div className="admin-warning" role="alert">
          <p>
            {last ? `${last.label} could not be downloaded. ` : ""}
            {failureMessage(mutation.error)}
          </p>
          <button
            type="button"
            className="button secondary admin-compact"
            onClick={() => last && start(last)}
          >
            Try again
          </button>
        </div>
      )}
      {mutation.isSuccess && mutation.data.serverSha256 && (
        <details className="admin-details">
          <summary>Server-reported SHA-256</summary>
          <p className="admin-muted">
            Reported by the server for this file; not verified in this browser.
          </p>
          <code>{mutation.data.serverSha256}</code>
        </details>
      )}
    </div>
  );
}

export function PartyExports({ id }: { id: string }) {
  return (
    <ExportGroup
      key={id}
      label="Party exports"
      actions={[
        { label: "Export party PDF", run: () => exportParty(id, "pdf") },
        { label: "Export party CSV", run: () => exportParty(id, "csv") },
      ]}
    />
  );
}

export function RunExports({ id }: { id: string }) {
  return (
    <ExportGroup
      key={id}
      label="Run exports"
      actions={[
        { label: "Export run PDF", run: () => exportRun(id, "pdf") },
        { label: "Export run CSV", run: () => exportRun(id, "csv") },
      ]}
    />
  );
}

export function PartyViewExport({ filters }: { filters: Partial<PartyFilters> }) {
  return (
    <ExportGroup
      label="Current view export"
      note="Includes every party matching the current filters, not only the rows on this page."
      actions={[
        { label: "Export current view CSV", run: () => exportPartyView(filters) },
      ]}
    />
  );
}

export function FullAuditExport() {
  return (
    <ExportGroup
      label="Full audit export"
      note="Includes inactive parties as CSV and JSON, every run manifest, the audit log and checksum indexes."
      actions={[{ label: "Full audit ZIP", run: exportFullAudit }]}
    />
  );
}
