# Milestone 5 screening policy and monitoring contracts

Status: implementation plan only. Start milestone 5 implementation after the coordinating agent records the Reviewer browser gate for milestone 4 as passed. The independent milestone 4 engine corrections are separate work; this plan does not authorize beginning auto-clear, scheduling or SMTP implementation early.

Source: design sections [7.4–7.8](../design-v1.md#7-matching-and-adjudication) and [9](../design-v1.md#9-scheduling-and-continuous-monitoring). Coordinate with the general milestone 5 plan and its API/scheduler/report owners.

## Policy settings and administrator acknowledgment

Use one canonical pair of fields: `auto_clear_enabled: bool = false` and `auto_clear_threshold: int = 65`. Avoid separate slider/threshold aliases. Validate the combined configuration, rejecting booleans used as integers, fractional values and nonfinite numbers. Require `review_threshold <= auto_clear_threshold <= likely_threshold`; fractional score thresholds still require an integer slider within those bounds. Threshold changes that invalidate an existing slider must include a valid slider in the same atomic request or fail without changing anything.

Extend the existing Admin/CSRF-protected settings update flow with these two writable fields and an ephemeral `auto_clear_acknowledgment` request field. A transition from off to on requires exactly `I understand`, checked by the server. A previous enable acknowledgment does not authorize a later off-to-on transition. Slider changes while already enabled and turning the switch off do not require another typed acknowledgment, but every change is audited. The request phrase is not a normal Setting value and cannot authorize an operation by merely being present in storage.

Create an explicit immutable acknowledgment audit event with the Admin ID, recorded identity, accepted wording, chosen threshold, prior/new enabled state and UTC time. Store an internal read-only reference to that event. Clients cannot write or replace this reference through generic settings PATCH. Revalidate the active Admin under the writer lock. Enabling, the acknowledgment, the reference and settings changes commit together. Reject enabled configurations without a valid recorded acknowledgment instead of silently auto-clearing.

The UI explains that decisions apply to future screenings and that existing queue items stay until screened again. An invalid or incomplete acknowledgment leaves settings and audit state unchanged. GET settings returns the current enabled state and threshold; acknowledgment provenance is read-only.

## Immutable scoring and decision contexts

Freeze validated settings before the run starts. Every run snapshot includes auto-clear enabled/threshold values and the acknowledgment audit reference, as well as hash-rule version and actual party/entry hash manifests. Each system decision records the exact policy used by its run; later settings cannot reinterpret it.

Keep `explanation.queue_context` limited to scoring/candidate/model configuration. Slider-only changes must not create a different queue anchor for a pair whose unresolved score and inputs are unchanged. Add a separate decision-context fingerprint covering the scoring context and the auto-clear policy/reference. Snapshot both contexts before inserting the Hit. The per-field comparison, best aliases, matched tokens and original snapshots remain immutable.

Scoring settings changes—thresholds, weights, legal suffixes, blocking stopwords, enabled lists, and later model scoring configuration—enqueue a durable full `manual` screening in the same transaction as the settings audit. Create no task for an unchanged effective setting. General display/timezone changes and slider-only enable/disable/value changes do not enqueue a full run. When a request changes both scoring and slider policy, the scoring change causes a future run under the jointly validated new policy. No existing Hit or Disposition changes inside the settings endpoint.

## Order of operations for one comparison

1. Generate candidates and explicitly include any current-hash unresolved prior pair in a covering run. Compute deterministic fields and score; do not store a pair below `candidate_min`.
2. Resolve latest human history before exact suppression. A newer confirmed/escalated decision invalidates an older remembered false positive; an applicable newer exact human false positive can restore the memory. Same-input completed human confirmations/escalations remain completed on repeat through an observation pointing at the decided anchor. Changed hashes reopen with the latest human decision and actual changes linked.
3. Apply the optional adjudication hook when eligible, then finalize confidence and bucket. A deterministic score at/above `likely_threshold` always has bucket `likely`, even if a model reduces confidence. Milestone 5 uses deterministic scoring; model adapters belong to milestone 8.
4. Auto-clear only an unsuppressed, unresolved new observation whose final bucket is **review**, with the switch enabled and `final_confidence < auto_clear_threshold`. A low observation is recorded without a disposition. A likely observation, an exact human suppression and a completed same-input human decision are never auto-cleared. Equality with the slider remains in the queue.
5. Insert the immutable Hit initially as open (or suppressed for actual human memory). For auto-clear, flush the Hit, insert/flush a system Disposition, then change the Hit status to dispositioned. This order respects the persisted evidence/status guards. Scores, snapshots and explanations are complete before the first insertion.
6. Finish the run, counters and durable task in one transaction. Insert any alert outbox intent in that same transaction, then commit before network delivery. A crash before commit leaves no partial Hit, decision or completed task.

The system Disposition has `kind=auto_cleared`, `actor_user_id=null`, `slider_value_at_time=N` and a note such as: `Auto-clear in run <id>; threshold=<N>; model=<name or none>; det_score=<value>; model_prob=<value or none>; final_confidence=<value>.` Preserve the per-comparison model/fallback meaning. Audit the decision as a system action even when an Admin requested the run; the run can separately retain its initiating actor. Temporarily clear the session actor while flushing the system decision/audit, restoring it afterward.

Never create a Suppression for an auto-clear. Suppression is remembered human false-positive evidence, and a slider is not a human finding about identity. The system decision and acknowledgment proof appear in the party file/export; `run.auto_cleared` counts new system decision rows from that run. The morning report counts those immutable rows since its previous successful reporting boundary.

## Repeated observations and old queue items

| New observation | Evidence and queue behavior |
| --- | --- |
| Exact remembered human false positive | Suppressed Hit linked to its effective human decision; no auto-clear and no queue item. |
| Same-input confirmed/escalated human decision | Open historical observation points at its dispositioned anchor; no fabricated new decision and no new queue item. |
| Unresolved review/likely, not auto-eligible | Reuse the current unresolved anchor when inputs/scoring context match; retain a new immutable observation. |
| Newly auto-eligible review observation | New dispositioned Hit and new system decision; do not link it to an unresolved queue anchor or create false-positive memory. |
| Later disabling/lowering slider makes a new observation ineligible | It can become a new unresolved queue anchor; the old system decision remains unchanged and linked as prior history. |

A future auto-cleared observation supersedes an older unresolved anchor for the active queue through the latest covered observation. Do not backdate a system decision onto that older Hit or alter its immutable evidence. Its historical open status is retained, with the newer observation explaining the current outcome. Settings writes alone never remove it. Re-screening an eligible auto-cleared pair produces another run-specific system decision; report counts are observations/decisions, not unique identities. A completed durable task is never re-executed, preventing retry duplication.

Continue using `queue_hits` and scoped `queue_hit_is_current` for queue/report/action eligibility. Raw `status=open` counts include historical observations and cannot represent current work. Preserve actual hash manifests for zero-hit coverage. Independent task failure must not starve later tasks: keep successful per-task commits, then return/raise sanitized aggregate diagnostics. An applied CSV import with partial screening remains an applied import and exposes pending/error fields to the UI.

## Scheduler, morning report and immediate alert handoff

Daily list refresh and incremental source sync enqueue only their actual deltas. The durable worker screens changed entries against active parties and changed parties against enabled entries. Full runs are first load, explicit manual runs, and explicitly audited scoring-configuration changes. A semantic no-change refresh must not discard an older still-current pending delta. Removed listings close current unresolved observations through recorded system decisions; original snapshots and historical hashes stay intact.

The scheduler owner provides a durable overlap lease/lock and restart recovery. Keep the deterministic run/task transaction atomic. The current write lock spans scoring; do not improve responsiveness by committing half a run. SMTP runs after commit. Milestone 8 must revisit holding this lock across retries/model network calls: any staged alternative must freeze inputs/settings, prevent concurrent duplicate work, verify current hash relevance, and insert all final evidence atomically without updating an existing Hit's scores.

Morning open/likely numbers use the canonical current queue. New work uses newly created queue anchors; auto-clear totals use immutable system decision rows. List changes, source health, as-of provenance and the exact ITA disclaimer follow section 9.3. The report still sends when empty. Delivery failure remains visible and does not advance the successful-report boundary.

Section 9.4 says one immediate email per run listing **all likely observations written by that run**, rather than only `hits_new` or new queue anchors. This includes repeated observations and suppressed likely observations; label their status/prior human result explicitly. Alerts are off by default. If the product chooses a narrower suppression exception, record it as an explicit specification decision before implementation; do not silently filter it out. Auto-clear never produces a likely alert candidate.

Persist an alert intent keyed by `(kind, run_id, recipient)` in the run transaction. Delivery uses original snapshots/safe URLs and frozen run settings, with a stable Message-ID. Drain it after commit through the scheduler/report owner; a completed run does not create another intent on retry. SMTP acceptance followed by a process crash before marking delivery can still produce a retry duplicate because ordinary SMTP has no exactly-once delivery transaction. Document this limitation; retain attempt/health evidence and never claim exactly-once external delivery.

## Concrete acceptance tests

| Case | Required assertion |
| --- | --- |
| Default/disabled policy | Review pairs queue; no system decision or suppression appears. |
| Acknowledgment | Reviewer denied; wrong/missing phrase rejected; off-to-on has an immutable Admin acknowledgment; re-enable requires a fresh acknowledgment. |
| Integer bounds | Reject booleans/fractions/out-of-range values; accept review/likely boundary integers; invalid joint score/slider update rolls back everything. |
| Strict comparison | Review confidence 74 at slider 75 auto-clears; confidence 75 stays queued. |
| Bucket rails | Low remains disposition-free; likely remains queued; deterministic likely with low model confidence never auto-clears. |
| Exact human suppression | Suppressed observation, effective human link, zero auto-clears; newer confirmed/escalated human history defeats old memory. |
| Completed human repeat | Same hashes preserve completed confirmation/escalation, no new system decision or queue item; changed hashes reopen. |
| Policy evidence | System actor null, slider/run/model/both scores in note, acknowledgment/context in immutable run snapshot and party history. |
| Future-only slider update | No task or existing Hit/status/Disposition mutation; a later real/manual screening applies the new policy. |
| Repeated auto-clear | One system decision per qualifying new observation; no Suppression; correct per-run/report counts and stable historical evidence. |
| Scoring settings change | One durable full-run task committed with the setting audit; slider-only/general changes enqueue none; combined update is coherent. |
| Unresolved repeat | Same scoring context reuses its anchor despite policy-only changes when still ineligible; explicit re-score keeps prior delta-found candidates covered. |
| Transaction/restart | Fault between Hit/Disposition/status/run writes rolls back; retry creates one complete event; completed tasks create no duplicate decision/outbox. |
| Independent task failure | Failed old task remains retriable, later successful task remains committed; upload truthfully reports applied import plus sanitized screening errors. |
| Alert scope/idempotence | All likely rows from one committed run appear, including repeats/suppressed rows; one intent per run/recipient; no SMTP before commit. |
| Morning report | Canonical open/likely totals; auto-clear rows since successful boundary; empty report, as-of provenance, source health, safe links and exact disclaimer. |

Record actual implementation evidence, deviations, failure modes and fixture/report/browser results in `docs/milestones/5.md` after the gate. Keep live ERP validation pending under the user's existing decision; no credentials or customer data belong in fixtures, logs, commits or this plan.
