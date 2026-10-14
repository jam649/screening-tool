# screening-tool design context

Self-hosted compliance workspace for reviewing customers and vendors against the U.S. Consolidated Screening List. React 18, Vite, TypeScript, TanStack Query, React Router and plain CSS.

## Visual direction

Calm, legible navy sidebar (#172d35), teal actions (#176b61), off-white canvas (#f5f6f3), white panels, muted text (#65777c), borders (#dce4df). System sans-serif; body 14px, titles 32px; compact but not cramped. Radius 5px controls / 8px panels; restrained shadows. No external fonts, illustrations, charts, decorative gradients, or gratuitous motion.

## Pages

First-run two-step account and verbatim ITA notice acknowledgment; sign-in; overview with four linked counts and a plain as-of statement; review queue with left list and right field comparison; parties searchable table; party audit file; source health and CSV column mapping; run audit table; Admin settings. Review queue is primary. F/M/E actions, J/K movement, V source.

## Behavior

Responsive and accessible, semantic native forms, labels, clear errors, visible keyboard focus, reduced motion. Never show fake customer data or counts in the implementation. First Admin must exist before entering workspace. All decisions retain audit records. Exact language comes from docs/design-v1.md.

## 21st reference

The compact checklist and derived progress count are informed by [ssychui / Onboarding Checklist](https://21st.dev/@ssychui/components/onboarding-checklist), retrieved through the 21st catalog. The app uses its own native semantic markup, real setup state, and project tokens; catalog animation code is not included. Hosted sketch generation was unavailable because fewer than three models were healthy. External source review was prevented by automatic approval review; local lint, build and browser checks cover the implementation.

## CSL setup and status

Milestone 2 adds a real CSL download action to the checklist and an Admin-only CSL settings panel. Progress appears only after the status endpoint loads; downloaded versions, fetch attempts, source stamps, queued screenings, and refresh outcomes come from API state. Version stamps stay separate from the latest fetch stamps, including unchanged downloads. Milestone 3 replaces the source setup placeholder with a working Sources link.

Claude Opus 5.5 authored the components, shared data hook, responsive CSS and contract tests at high effort through a no-tool structured source session; Sol integrated and verified them. Exact first-party model: `claude-opus-5-5`; session `daed6127-c517-4c72-a3f6-12fbfec7b81f`.

## Source imports

Sources cards use server-reported counts, import timestamps, next-sync information and health. Admins can add a CSV source, preview CSV/XLSX rows, reuse saved mappings, map multiple address and alias columns, and import a complete snapshot. Reviewers can inspect source health. Valid header-only snapshots can mark all source parties inactive while keeping history; rejection outcomes show exact server totals and detail caps, and uncertain failures do not claim that records were unchanged.

Claude Opus 5.5 authored this UI and its multipart contract tests at medium effort; Sol integrated routes, adapted the zero-row backend contract and verified the browser workflows. Exact first-party model: `claude-opus-5-5`; session `a8fe6519-6e0a-443d-94cf-89a8e6f9dd92`.

## Review and administration

Milestone 4 keeps the navy/teal tokens and native semantic forms. Review queue compares immutable snapshots with matched words, recorded scoring, prior decisions and field changes. Original false-positive and completed human decisions retain their actual actor and note without implying a new decision. Keyboard F/M/E/J/K/V respects text input focus. Party files distinguish current records from historical observations, exact zero-hit runs and collapsed previously cleared observations; unscreened and escalated states remain explicit. Overview counts and full-list coverage come from the backend. Admin settings, user roles, deactivation and invitation revocation are operational, with last-Admin protection and invitation tokens stripped from fragments before requests.

Actual Claude Opus 5.5 authored the eight core component/API/CSS/test files at high effort (`claude-opus-5-5`, session `3289afe0-3573-411e-80fe-e9a30ce42542`, 2339.9 seconds) and six Admin/invitation files at medium effort (session `22c7f5ae-b10d-4664-88f7-c141e969522d`, 633 seconds). Both used first-party, no-tool structured output on user-approved project source/design input; neither used fallback models. Sol integrated and verified final contracts, routes and browser behavior. No new private source was sent to 21st.


## Monitoring, delivery, exports and retention

Milestone 5 continues the established native forms and navy/teal tokens. Auto-clear uses a focused native dialog for the exact typed acknowledgment, applies only to future screening observations and keeps joint review/likely/slider bounds explicit. Monitoring shows real schedules and operation outcomes. SMTP settings preserve omitted encrypted credentials; delivery status distinguishes queued, SMTP accepted, failed and uncertain attempts. Party, run, current-filter CSV and complete ZIP downloads use server artifacts and honest download-started states. Admin retention exposes activity-based eligibility, attestations, holds and direct apply with permanent certificate evidence. Historical party participation and purged comparison snapshots remain separate from the current record.

All seven small authoring bundles used the actual first-party `claude-opus-5-5` model at medium effort with safe, tool-free structured output on user-approved project source/design. Sessions: auto-clear `855c5c14-12a8-4012-a7e6-ede68d0b0041` (156697 ms), native dialog refinement `0b4c62eb-1869-4d32-b3c8-d3ae94dbc2c6` (87710 ms), monitoring `91e49e05-9883-426d-b0ff-685018aa265a` (299060 ms), notifications `03264536-26bb-46ea-9ffd-635c9ecad046` (355242 ms), retention `08e1ed3d-7c3c-476f-9a2e-1bc5c133f330` (316220 ms), joint bounds `bbbb0a47-d858-46cf-a7f2-b9a6b2243619` (99742 ms), exports `2face31b-e03d-4611-8fcc-bb8cba070c63` (235240 ms). Each reported the canonical model without fallback or provider error. Sol integrated routes and backend contracts, corrected test fixtures/selectors and added permanent-evidence/fresh-attestation regressions. Raw exchanges remain in the ignored local workspace; credentials, environment files and customer data were excluded. No private source upload to 21st occurred. Browser gate outcomes are recorded separately in milestone documentation after actual runtime verification.


## QuickBooks Online

Milestone 6 adds native Admin app settings for independent sandbox and production credentials, one company per source, verified Intuit navigation and optional signed webhooks. Public source cards show server counts, actual schedules and connection health to Reviewers. Durable sync and disconnect controls distinguish queued/running operations, committed imports with deferred screening, completed task provenance and actual remote revocation outcomes. Callback feedback uses fixed messages without reflecting arbitrary query parameters. Saved app values remain encrypted on the server and appear only as configured flags; omitted fields preserve them, explicit clears are separate, and entered values are removed after a save attempt or environment change. Existing CSV mapping and upload behavior is preserved.

Both small authoring bundles used actual first-party `claude-opus-5-5` at medium effort with safe, tool-free structured output on user-approved project source/design. Settings/API/tests: session `c0e42de3-3025-4c05-936b-3674939cec4f` (231415 ms, two turns). Source controls/tests: session `20184a36-8434-4929-9190-af95998244ea` (482932 ms, two turns). Both reported only the canonical model and no provider errors. Sol integrated routes and public contracts, unified app cache and OAuth URL validation, corrected completion precedence and partial-failure presentation, and verified browser regressions and mobile layout. Raw exchanges remain ignored; credentials, environment files, customer/runtime data and raw messages were excluded. The established 21st catalog and navy/teal tokens continue without a new private upload. Fixture/browser verification is recorded separately; live Intuit sandbox validation remains deferred.


## Business Central

Milestone 7 continues the native forms and existing 21st catalog tokens. One global Entra app uses write-only credentials and configured flags, while each source keeps its tenant, case-sensitive environment and explicitly selected company. Company discovery does not activate a source. Durable sync, subscription changes and disconnect actions show accepted attempts separately from completed operations, actual imported counts and screening status. Source cards expose public connection health to Reviewers without querying private app or connection metadata. Subscription health uses server-listed resources, IDs and actual expiries; cleanup failures retain a visible retry path without claiming Entra consent was revoked.

Both small authoring bundles used actual first-party `claude-opus-5-5` at medium effort with safe, tool-free structured output on user-approved project source/design. Settings/API/tests: session `6ee520e4-761a-4c85-bc40-ac7947ac8c10` (198393 ms, two turns). Source controls/tests: session `63dd609f-bdab-4800-9548-6aedd2c5bf04` (439214 ms, two turns). Both reported only the canonical model and no provider errors. Sol integrated source/settings routes and cache invalidation, aligned ID-only credential preservation and added narrow-screen checks. Raw exchanges remain ignored; credentials, environment files, databases and runtime/customer data were excluded. No private source was uploaded to 21st. Fixture/runtime and live tenant validation are recorded separately; live tenant validation remains deferred.
