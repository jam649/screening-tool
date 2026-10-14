# Milestone 4 backend verification

Backend implementation evidence, 2026-10-13. The complete milestone gate also requires the Reviewer keyboard workflow in the browser; this note does not claim that browser gate passed.

## Built

Authenticated current review queue, immutable hit detail, attributed dispositions, party filtering and files, run and audit listings, shared dashboard/report metrics, validated general/scoring settings, and administrator user management. Product naming reads the backend `display_name` setting with the existing single default constant.

A decision takes SQLite's writer lock, rechecks the current queue anchor and the current party/listing hashes, and inserts a separate immutable decision for every matching open observation. Each decision records its reviewer, note, and the immutable actor identity in an explicit audit event. A false positive creates one exact four-way suppression linked to the actual selected human decision. Historical superseded observations remain available; stale requests return a conflict without inventing a decision.

Party files include every completed run with explicit party membership, including runs that produced no candidate hits. Current record hashes are captured in run manifests by the engine. A party added afterward cannot appear in an earlier full run. Repeated suppressed observations appear in a collapsed group. Relevant record, listing, source, run, disposition, suppression, and settings audit events are retained in the file.

Dashboard counts use the canonical engine queue. Screening recency includes zero-hit participation and requires the current party hash. Latest-version coverage, source sync health, and auto-clear counts are derived from actual records. Auto-clear counts and the corresponding party filter share the `last_report_at` successful-report boundary.

Invitations use 32 cryptographically random bytes, store only a SHA-256 digest, expire after 72 hours, and allow one acceptance. Replacing an invitation revokes its previous token. Browser tokens travel in `/accept-invite#token=...` and request bodies, never query strings or URL paths. Acceptance creates an argon2id password and the existing Secure, HttpOnly, SameSite=Lax session with a CSRF token. Invitation secrets and digests are omitted from audit records. Deactivation revokes sessions; serialized user changes preserve at least one active Admin.

## Verified

- Full existing-plus-new backend suite: **150 passed** in 50.42 seconds before the final report-filter and invitation-concurrency additions.
- Final targeted review, party, user, and settings suite: **32 passed** in 4.91 seconds, including both final additions.
- Ruff passed for all changed backend modules, migration, and tests; mypy passed for the eight new/changed API, invitation, and metrics modules; `git diff --check` passed.
- Meaningful regressions cover reviewer access, required notes, CSRF, repeated observations, changed-hash reopening, immutable snapshots and prior actor identity, competing dispositions, last-Admin concurrency, invitation one-use concurrency/replacement/expiry, Unicode/transliterated searches, filters, zero-hit and later-party histories, current-hash staleness, report boundaries, and rejected unsupported/sensitive settings writes.

## Limits and failure modes

SMTP invitation delivery is deferred to milestone 5: the API provides a manual invitation link and explicitly reports pending delivery. Actual auto-clear execution, scheduling, retention application, and exports belong to milestone 5; no placeholder decisions were added here.

Legacy runs that recorded a null full-book scope cannot prove zero-hit membership. Their party participation is shown only where existing hit evidence proves it; a new completed run with the explicit manifest establishes current recency. Historical membership is not reconstructed from today's party list.

The application returns a conflict if another reviewer decides an item or a source record changes while it is being reviewed. A busy SQLite writer returns a retryable conflict. Public health checks execute a database read and return a sanitized unavailable response on database failure.

## Independent milestone 1–4 requirement audit

The specification audit and the coordinator's separate Opus review identified and corrected concrete backend gaps before the browser gate:

- A complete legitimate CSL contraction now applies all removals and system closures, with a warning and audit counts. An incomplete envelope still fails atomically; the supplemental test proves both cases.
- Source links now prefer a safe Federal Register notice before the source-list URL, as section 6.5 requires. Historical links remain those captured by the specific observation.
- Latest-list coverage unions actual run input manifests for each current party hash and scoring context. A one-entry delta cannot make a new party appear fully screened. A metadata-only list version does not erase coverage of unchanged matching inputs. The latest successful download timestamp is exposed separately from initial version creation.
- Parties without proven full current coverage are labeled `unscreened`. Escalated parties are labeled `escalated`, never no-match/clear. Current status and suppression counts follow effective latest human decisions, including a newer positive overriding old exact false-positive memory.
- Audit/display actor identity comes from real recorded user or explicit disposition snapshots, rather than today's mutable user record. Relevant list versions, fetches and completed task provenance appear in party audit files. Exact party IDs recorded in a task request also select its pending/failed audit events; a failed attempt never invents a completed run.
- An issuing Admin's demotion/deactivation revokes their pending invitations. Validation and serialized acceptance independently reject an issuer who is no longer an active Admin, including legacy unrevoked tokens. Explicit pending invitation withdrawal is Admin/CSRF protected and races safely with acceptance.
- Run lists and screening audit presentation project scope counts; a party timeline includes only its own recorded hash. Full manifests stay immutable in the database and dedicated evidence/export paths. Regression assertions compare stored data before and after display requests.
- Slider auto-clear counts require an actual recorded slider; removed-entry closures are a separate metric. Both retain their real immutable decisions.
- Human/system decision rows are inserted and flushed before the corresponding hit status transition, matching the coordinator's SQL/ORM transition guards. Identical confirmed/escalated repeats expose an explicit original completion reference without inserting a fake new decision.
- Disposition validation now uses the matching engine's single-pair queue check, avoiding a full-book queue scan while holding the writer lock.

The final owned API/removal-policy regression suite passed **51 tests in 7.56 seconds**; the final explicitly scoped failed-task history regression passed separately in **0.25 seconds**. Owned-module Ruff and mypy checks passed. The combined backend run passed **197 tests**, with one fixture-regeneration provenance-metadata failure assigned to the matching/fixture owner after the hash-policy upgrade; the frozen government rows and sixty expected outcomes were not changed. The coordinator owns the final combined-suite and browser gate results.

The milestone 1–3 backend review found the prescribed auth/session/CSRF/rate limits, encryption-key binding, append-only audit and immutable comparison snapshots, soft deletion, strict partial-file rejection, unchanged-hash no-write behavior, canonical queue/history distinction, and frozen deterministic fixture contract in their existing implementations/tests. Matching hash normalization, candidate recall, latest human-decision precedence, and completed-run/status SQL guards were separately assigned to the matching/coordinator owners; their final suite evidence is not invented here.

Remaining gate assumptions are explicit: milestone 4 does not yet run a persistent monitor, SMTP, slider execution, retention purge or audit export. Source/list changes leave durable work for a manual screen until milestone 5 connects the worker. The real Reviewer browser gate must pass before milestone 5 starts. Zero-hit manifests prove participation and hashes; full zero-hit historical field exports must use real recorded audit snapshots or new immutable run-party evidence, as specified in the [milestone 5 plan](../plans/milestone-5.md), never today's party fields. Live ITA-key access and live ERP sandboxes remain unverified/deferred as already documented.


## Final checkpoint verification

The coordinator's final combined backend run passed **200 tests in 60.69 seconds**, with whole-backend Ruff and mypy passing (42 application modules). The historical fixture-provenance regeneration failure described above was resolved by reproducing the original version 1 capture fingerprint; frozen government rows, metadata, planted cases, and expected outcomes remain unchanged. Runtime matching uses the explicitly approved type-aware version 2 hashes. Immutable historical version 1 evidence stays unchanged.

The complete milestone 4 browser gate subsequently passed on a fresh Docker volume: 2,060-party CSV import, 300 listings, 77 keyboard-reviewed queue items, every action attributed in the audit and party file, unchanged full rescreen with zero queue items, retained confirmed/escalated decisions, and a stable-ID name/type change reopening the original pairing with its real earlier decision. Full frontend and gate evidence is recorded in [4.md](4.md). Scheduling, SMTP, auto-clear execution, exports and retention remain milestone 5 work.
