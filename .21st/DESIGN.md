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
