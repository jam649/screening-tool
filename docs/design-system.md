# Interface design system

The review queue is the primary workflow: one customer/vendor record compared with one public listing, original evidence and human decisions visible, and keyboard controls that pause while typing. Overview, party files, sources, audit and Admin-only settings use the same native semantic controls.

| Token | Value |
| --- | --- |
| Sidebar / primary ink | `#172d35` |
| Actions | `#176b61` |
| Canvas | `#f5f6f3` |
| Panels | `#ffffff` |
| Muted text | `#65777c` |
| Borders | `#dce4df` |
| Body / page title | 14px / 32px, system sans-serif |
| Control / panel radius | 5px / 8px |
| Spacing unit | 4px |

Use compact legible tables, visible focus, explicit pending/error states, restrained shadows and reduced-motion support. Business changes and actual prior actor/note remain visible; bounded raw evidence disclosures are closed by default and preserve exact escaped before/after values. Reviewers retain human authority; product language does not label a party government-approved or compliant. No external fonts or telemetry are required.

The public [21st onboarding checklist](https://21st.dev/@ssychui/components/onboarding-checklist) informed first-run progress presentation; the project uses its own native implementation and did not copy catalog code or add animation dependencies. Maintainer milestone/review notes remain local. Tool-specific `.21st` configuration and authoring notes remain local and ignored.

[Runtime screenshots and installation](../README.md)
