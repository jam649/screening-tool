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

Milestone 2 adds a real CSL download action to the checklist and an Admin-only CSL settings panel. Progress appears only after the status endpoint loads; downloaded versions, fetch attempts, source stamps, queued screenings, and refresh outcomes come from API state. Version stamps stay separate from the latest fetch stamps, including unchanged downloads. Source setup is marked coming next without a dead action.

Claude Opus 5.5 authored the components, shared data hook, responsive CSS and contract tests at high effort through a no-tool structured source session; Sol integrated and verified them. Exact first-party model: `claude-opus-5-5`; session `daed6127-c517-4c72-a3f6-12fbfec7b81f`.
