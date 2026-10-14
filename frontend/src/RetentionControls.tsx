import { useId, useState, type FormEvent, type ReactNode } from "react";
import type { Session } from "./api";
import { canAdminister, messageOf } from "./adminApi";
import { Field } from "./SettingsControls";
import { Hash, LocalTime } from "./CSLPanel";
import { Link } from "react-router-dom";
import {
  describeReason,
  describeRetained,
  useApplyRetention,
  useAttest,
  useEligibility,
  usePlaceHold,
  usePreviewRetention,
  useReleaseHold,
  useRetentionStatus,
  type ActorSnapshot,
  type Certificate,
  type Eligibility,
  type EligibilityInput,
  type Hold,
  type HoldKind,
  type RetentionPlan,
  type RetentionResult,
} from "./retentionApi";

const KIND_LABEL: Record<HoldKind, string> = {
  government: "Government hold",
  other: "Ordinary hold",
};
const BLANK: EligibilityInput = {
  event_date: "",
  external_dates_considered: false,
  government_holds_excluded: false,
};

function actorText(actor: ActorSnapshot) {
  if (typeof actor === "string" && actor.trim()) return actor;
  if (actor && typeof actor === "object")
    return actor.display_name || actor.email || "identity not recorded";
  return "identity not recorded";
}

function todayLocal() {
  const now = new Date();
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

const yesNo = (value: boolean) => (value ? "yes" : "no");

function Local({ value }: { value: string }) {
  return (
    <>
      <LocalTime value={value} /> (local time)
    </>
  );
}

function PartyLink({ id }: { id: string }) {
  return <Link to={`/parties/${encodeURIComponent(id)}`}>{id}</Link>;
}

function Region({ title, level = 4, children }: { title: string; level?: 3 | 4; children: ReactNode }) {
  const id = useId();
  const Heading = level === 3 ? "h3" : "h4";
  return (
    <section className="admin-subsection" aria-labelledby={id}>
      <Heading id={id}>{title}</Heading>
      {children}
    </section>
  );
}

function Failure({ error, fallback }: { error: unknown; fallback: string }) {
  return (
    <p className="admin-field-error admin-group-error" role="alert">
      {messageOf(error, fallback)}
    </p>
  );
}

function Reasons({ codes }: { codes: string[] }) {
  return (
    <ul className="admin-muted">
      {codes.map((code) => (
        <li key={code}>{describeReason(code)}</li>
      ))}
    </ul>
  );
}

function HoldForm({ partyId, legend, action }: { partyId: string | null; legend: string; action: string }) {
  const place = usePlaceHold();
  const [kind, setKind] = useState<HoldKind>("other");
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string>();
  const [done, setDone] = useState("");
  function submit(event: FormEvent) {
    event.preventDefault();
    setDone("");
    if (!reason.trim()) return setError("Enter the reason for the hold.");
    setError(undefined);
    place.mutate(
      { party_id: partyId, kind, reason: reason.trim() },
      {
        onSuccess: (hold) => {
          setReason("");
          setKind("other");
          setDone(`${KIND_LABEL[hold.kind] ?? "Hold"} placed.`);
        },
      },
    );
  }
  return (
    <form className="admin-form" aria-label={legend} onSubmit={submit} noValidate>
      <fieldset className="admin-group" disabled={place.isPending}>
        <legend>{legend}</legend>
        <div className="admin-grid admin-grid-tight">
          <Field label="Hold type">
            {(props) => (
              <select {...props} value={kind} onChange={(e) => setKind(e.target.value as HoldKind)}>
                <option value="other">{KIND_LABEL.other}</option>
                <option value="government">{KIND_LABEL.government}</option>
              </select>
            )}
          </Field>
          <Field label="Reason for hold" error={error}>
            {(props) => (
              <textarea {...props} rows={3} value={reason} onChange={(e) => setReason(e.target.value)} />
            )}
          </Field>
        </div>
        <div className="admin-actions">
          <button className="button primary" type="submit">
            {action}
          </button>
        </div>
        {place.isError && <Failure error={place.error} fallback="The hold could not be placed." />}
        <p className="admin-status" role="status">
          {done}
        </p>
      </fieldset>
    </form>
  );
}

function ReleaseForm({ hold }: { hold: Hold }) {
  const release = useReleaseHold();
  const government = hold.kind === "government";
  const [reason, setReason] = useState("");
  const [reference, setReference] = useState("");
  const [errors, setErrors] = useState<{ reason?: string; reference?: string }>({});
  function submit(event: FormEvent) {
    event.preventDefault();
    const next = {
      reason: reason.trim() ? undefined : "Enter the reason for releasing this hold.",
      reference:
        government && !reference.trim()
          ? "A government hold needs the written authorization reference."
          : undefined,
    };
    setErrors(next);
    if (next.reason || next.reference) return;
    release.mutate({
      holdId: hold.id,
      values: {
        reason: reason.trim(),
        ...(reference.trim() ? { authorization_reference: reference.trim() } : {}),
      },
    });
  }
  return (
    <form aria-label={`Release ${KIND_LABEL[hold.kind] ?? "hold"}`} onSubmit={submit} noValidate>
      <fieldset className="admin-fieldset" disabled={release.isPending}>
        <div className="admin-grid">
          <Field label="Release reason" error={errors.reason}>
            {(props) => <input {...props} value={reason} onChange={(e) => setReason(e.target.value)} />}
          </Field>
          <Field
            label={government ? "Written authorization reference" : "Authorization reference (optional)"}
            error={errors.reference}
          >
            {(props) => <input {...props} value={reference} onChange={(e) => setReference(e.target.value)} />}
          </Field>
        </div>
        <div className="admin-actions">
          <button className="button secondary admin-compact" type="submit">
            Release hold
          </button>
        </div>
        {release.isError && <Failure error={release.error} fallback="The hold could not be released." />}
      </fieldset>
    </form>
  );
}

function HoldList({ holds, empty }: { holds: Hold[]; empty: string }) {
  if (!holds.length) return <p className="admin-muted">{empty}</p>;
  return (
    <ul className="admin-user-list">
      {holds.map((hold) => (
        <li key={hold.id}>
          <div className="admin-user-identity">
            <strong>
              {KIND_LABEL[hold.kind] ?? hold.kind} ·{" "}
              {hold.party_id ? <>Party <PartyLink id={hold.party_id} /></> : "All parties"}
            </strong>
            <span>{hold.reason}</span>
            <span>
              Placed by {actorText(hold.actor)}
              {hold.created_at && <> · <Local value={hold.created_at} /></>}
            </span>
          </div>
          <ReleaseForm hold={hold} />
        </li>
      ))}
    </ul>
  );
}

function EligibilityView({ data }: { data: Eligibility }) {
  const { attestation } = data;
  return (
    <>
      <p className={data.eligible ? "admin-warning" : "admin-status"}>
        {data.eligible
          ? "The server reports this party's hit and disposition evidence as eligible. The next retention apply re-checks it and may purge it."
          : "This party's evidence is protected and will not be purged."}
      </p>
      {data.reasons.length > 0 && <Reasons codes={data.reasons} />}
      <dl className="admin-muted">
        <dt>Retention period</dt>
        <dd>{data.years === null ? "Off" : `${data.years} years`}</dd>
        <dt>Basis (the later of the attested date and party activity)</dt>
        <dd>{data.basis_at ? <Local value={data.basis_at} /> : "Not determined"}</dd>
        <dt>Latest observed party activity</dt>
        <dd>{data.observed_activity_at ? <Local value={data.observed_activity_at} /> : "Not recorded"}</dd>
        <dt>Latest attested event date retained</dt>
        <dd>{data.latest_attested_event_date ?? "Not recorded"}</dd>
        <dt>Eligible on</dt>
        <dd>
          {data.eligible_on ?? "Not determined"}
          {data.eligible_at && <> (<Local value={data.eligible_at} />)</>}
        </dd>
        <dt>Latest attestation</dt>
        <dd>
          {attestation ? (
            <>
              Event date {attestation.event_date}; external dates considered:{" "}
              {yesNo(attestation.external_dates_considered)}; known government holds excluded:{" "}
              {yesNo(attestation.government_holds_excluded)}; recorded by {actorText(attestation.actor)},{" "}
              <Local value={attestation.created_at} />
            </>
          ) : (
            "None recorded"
          )}
        </dd>
      </dl>
    </>
  );
}

function AttestationForm({ partyId }: { partyId: string }) {
  const attest = useAttest(partyId);
  const [draft, setDraft] = useState<EligibilityInput>(BLANK);
  const [error, setError] = useState<string>();
  const [saved, setSaved] = useState("");
  const max = todayLocal();
  function submit(event: FormEvent) {
    event.preventDefault();
    setSaved("");
    const problem = !draft.event_date
      ? "Enter the latest applicable external event date."
      : draft.event_date > max
        ? "The event date cannot be in the future."
        : undefined;
    setError(problem);
    if (problem) return;
    attest.mutate(draft, {
      onSuccess: ({ attestation }) => {
        if (attestation)
          setDraft({
            event_date: attestation.event_date,
            external_dates_considered: attestation.external_dates_considered,
            government_holds_excluded: attestation.government_holds_excluded,
          });
        setSaved("Attestation recorded. The eligibility shown above is the server's current result.");
      },
    });
  }
  return (
    <form className="admin-form" aria-label="Eligibility attestation" onSubmit={submit} noValidate>
      <fieldset className="admin-group" disabled={attest.isPending}>
        <legend>Eligibility attestation</legend>
        <p className="admin-intro">
          Eligibility runs from the later of this date and any later party activity, screening run or
          disposition. An attestation alone never makes a party eligible: retention must be on, the party
          inactive, no hold active and the retention period passed. A government hold cannot be overridden
          by an attestation. An earlier date cannot lower a previously attested event date. New party activity requires a new attestation. Leaving a box unchecked records that you could not confirm it, and the party
          stays protected.
        </p>
        <div className="admin-grid admin-grid-tight">
          <Field label="Latest applicable external event date" help="Future dates are not accepted." error={error}>
            {(props) => (
              <input
                {...props}
                type="date"
                max={max}
                value={draft.event_date}
                onChange={(e) => setDraft((prev) => ({ ...prev, event_date: e.target.value }))}
              />
            )}
          </Field>
        </div>
        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={draft.external_dates_considered}
            onChange={(e) => setDraft((prev) => ({ ...prev, external_dates_considered: e.target.checked }))}
          />
          I attest that all latest applicable external event dates for this party were considered.
        </label>
        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={draft.government_holds_excluded}
            onChange={(e) => setDraft((prev) => ({ ...prev, government_holds_excluded: e.target.checked }))}
          />
          I attest that known government holds were checked and none applies to this party.
        </label>
        <div className="admin-actions">
          <button className="button primary" type="submit">
            Save attestation
          </button>
        </div>
        {attest.isError && <Failure error={attest.error} fallback="The attestation could not be saved." />}
        <p className="admin-status" role="status">
          {saved}
        </p>
      </fieldset>
    </form>
  );
}

/** Admin-only retention panel for one party file; renders nothing and requests nothing otherwise. */
export function PartyRetentionControls({ session, partyId }: { session: Session; partyId: string }) {
  const eligibility = useEligibility(session, partyId);
  if (!canAdminister(session)) return null;
  return (
    <Region title="Evidence retention" level={3}>
      {eligibility.isPending && <p className="admin-muted">Loading retention eligibility…</p>}
      {eligibility.isError && (
        <Failure error={eligibility.error} fallback="Retention eligibility is unavailable." />
      )}
      {eligibility.data && (
        <>
          <EligibilityView data={eligibility.data} />
          <HoldList holds={eligibility.data.holds} empty="No active holds on this party." />
        </>
      )}
      <HoldForm partyId={partyId} legend="Hold for this party" action="Place party hold" />
      <AttestationForm partyId={partyId} />
    </Region>
  );
}

function CertificateList({ certificates }: { certificates: Certificate[] }) {
  if (!certificates.length) return <p className="admin-muted">No purge certificates yet.</p>;
  return (
    <ul className="admin-user-list">
      {certificates.map((certificate) => (
        <li key={certificate.id}>
          Party <PartyLink id={certificate.party_id} />
          {certificate.created_at && <> · <Local value={certificate.created_at} /></>}
          <details>
            <summary>Checksum</summary>
            <Hash value={certificate.sha256} />
          </details>
        </li>
      ))}
    </ul>
  );
}

function PlanView({ plan }: { plan: RetentionPlan | RetentionResult }) {
  const applied = "applied" in plan && plan.applied;
  return (
    <div className="admin-form">
      <p className="admin-status" role="status">
        {applied ? "Purged" : "Eligible now"}: {plan.hits} hit records, {plan.dispositions} disposition
        records, {plan.bundles.length} parties.
      </p>
      <p className="admin-muted">
        {applied ? "Applied" : "Previewed"} as of <Local value={plan.as_of} />; retention{" "}
        {plan.enabled ? `${plan.years} years` : "off"}.
        {!applied && " Apply re-checks every party on the server, so its result can differ."}
      </p>
      {plan.bundles.length === 0 ? (
        <p className="admin-muted">No parties are eligible{applied ? ", so nothing was purged." : " right now."}</p>
      ) : (
        <ul className="admin-muted">
          {plan.bundles.map((bundle) => (
            <li key={bundle.party_id}>
              <PartyLink id={bundle.party_id} /> — eligible on {bundle.eligible_on};{" "}
              {bundle.hit_ids.length} hit records, {bundle.disposition_ids.length} disposition records
            </li>
          ))}
        </ul>
      )}
      {plan.blocked.length > 0 && (
        <ul className="admin-muted">
          {plan.blocked.map((block) => (
            <li key={`${block.party_id}:${block.hit_id ?? ""}`}>
              Protected: <PartyLink id={block.party_id} />
              {block.hit_id && ` (hit ${block.hit_id})`}
              <Reasons codes={block.reasons} />
            </li>
          ))}
        </ul>
      )}
      {applied && <CertificateList certificates={plan.certificates} />}
    </div>
  );
}

/** Admin Settings retention panel; the retention period itself is edited in the General form. */
export function RetentionControls({ session }: { session: Session }) {
  const status = useRetentionStatus(session);
  const preview = usePreviewRetention();
  const apply = useApplyRetention();
  const [unknownOutcome, setUnknownOutcome] = useState(false);
  if (!canAdminister(session)) return null;
  const data = status.data;
  const busy = preview.isPending || apply.isPending;
  const plan = apply.data ?? preview.data;

  async function refreshStatus() {
    const result = await status.refetch();
    if (result.isSuccess) setUnknownOutcome(false);
  }
  function runPreview() {
    apply.reset();
    preview.mutate();
  }
  function runApply() {
    preview.reset();
    setUnknownOutcome(false);
    apply.mutate(undefined, { onError: () => setUnknownOutcome(true) });
  }

  return (
    <section className="panel settings-panel" aria-label="Evidence retention">
      <h2>Evidence retention</h2>
      {status.isPending && <p className="admin-muted">Loading retention status…</p>}
      {status.isError && <Failure error={status.error} fallback="Retention status is unavailable." />}
      {data && (
        <>
          <p className="admin-intro">
            {data.enabled
              ? `Retention is on: evidence for inactive parties becomes eligible ${data.years} years after the later of the attested external event date and the party's latest activity, screening run or disposition (minimum ${data.minimum_years} years). Change the period in General settings.`
              : `Retention is off. Nothing is purged. To turn it on, set a retention period of at least ${data.minimum_years} years in General settings.`}
          </p>
          <p className="admin-muted">Inactive parties: {data.inactive_parties}</p>
          <p className="admin-muted">
            Retention removes eligible comparison and decision records from the working tables. Their
            original evidence remains in permanent certificates. It is not full erasure: these records
            are kept permanently.
          </p>
          <ul className="admin-muted">
            {data.permanently_retained.map((table) => (
              <li key={table}>{describeRetained(table)}</li>
            ))}
          </ul>
          <Region title="Holds">
            <HoldList holds={data.active_holds} empty="No active holds." />
            <HoldForm partyId={null} legend="Workspace hold (all parties)" action="Place workspace hold" />
          </Region>
        </>
      )}
      <Region title="Purge evidence">
        <p className="admin-muted">
          A preview is optional. Applying re-checks every party on the server and purges only what is
          eligible at that moment.
        </p>
        <div className="admin-actions">
          <button className="button primary" type="button" disabled={busy} onClick={runPreview}>
            Preview retention
          </button>
          <button
            className="button primary"
            type="button"
            disabled={!data?.enabled || busy || unknownOutcome}
            onClick={runApply}
          >
            Apply retention now
          </button>
        </div>
        {data && !data.enabled && <p className="admin-muted">Retention is off, so applying is disabled.</p>}
        {preview.isError && <Failure error={preview.error} fallback="The preview could not be produced." />}
        {unknownOutcome && (
          <div className="admin-warning" role="alert">
            <p>
              {messageOf(apply.error, "The apply request failed.")} The outcome is unknown: evidence may or
              may not have been purged. Refresh status and check the certificates before trying again.
            </p>
            <button className="button secondary admin-compact" type="button" onClick={() => void refreshStatus()}>
              Refresh status
            </button>
          </div>
        )}
        {plan && <PlanView plan={plan} />}
      </Region>
      {data && (
        <Region title="Purge certificates">
          <CertificateList certificates={data.certificates} />
        </Region>
      )}
    </section>
  );
}
