# v1 implementation and verification

Source: [Design Document v1](../design-v1.md), supplied 2026-10-13. Build all nine milestones and ship to `https://github.com/Jam649/screening-tool`. Preserve the original scope, optional AI, audit guarantees, and fixture contract.

## User decisions during implementation

- Work unattended; balance Claude and Codex implementation and independent review.
- Use 21st for UI design, replacing Superdesign.
- Build now with synthetic CSV data; live QuickBooks and Business Central sandbox checks are deferred until credentials/tenants are supplied. Recorded-contract tests gate connector implementation for now. Never report these as live sandbox passes.
- Use `AGENTS.md` instead of the obsolete `CLAUDE.md` workflow entry.
- On 2026-10-13 explicitly include party type and CSL entity type in both matching hashes (hash rule version 2); type-only edits must re-screen and invalidate remembered four-hash false positives. CSL hash text also follows section 4.1 normalization with Unicode/ASCII forms and unordered alias/address/country/program sets. Preserve immutable version 1 evidence. Cached records transition through explicit `screening-tool hashes upgrade` and `screening-tool screen --changed` commands; the upgrade queues a full screening and records the policy change. An automatic startup rewrite was rejected by approval review and is not wired into application startup. New completed run manifests include actual party and entry hashes so coverage does not reinterpret a historical v1 revision hash as v2.

## Ordered milestones

1. Scaffold, migrations for all tables, secure setup/auth, React shell, single-service Docker, license/DCO/CI. Gate: Compose serves wizard and creates first Admin.
2. Verified bulk CSL parser, file hashes, semantic entry diff, source metadata, CLI refresh. Gate: duplicate input has one version; fixture v2 has exactly five adds/changes/removals.
3. CSV/XLSX preview/mapping/rejection/full-sync, hashes, normalization/blocking/scoring, suppression, CLI screening. Gate: frozen 300-entry CSL sample and 2,060-party dataset meet all 60 planted outcomes; repeats do not create duplicate open queue items.
4. Home, queue with keyboard and historical comparison, parties/files, disposition workflow, users and audit. Gate: browser Reviewer works queue and provenance survives record changes.
5. Persistent scheduling/locks/scoped jobs, remembered false positives, acknowledged auto-clear, SMTP reports/alerts, PDF/CSV/ZIP exports and retention. Gate: fixture cycle, changed-hash reopening, report+exports, bounded slider/audit.
6. QBO OAuth/full+CDC polling/validated webhook using recorded HTTP fixtures; document production app prerequisites. Live sandbox pending by explicit user direction.
7. BC OAuth/full+incremental/validated webhook+subscription renewal and deletion reconciliation using fixtures. Live sandbox pending by explicit user direction.
8. Optional Jev/Laya verified adapters, fixed-order truncation, retries/fallback, privacy ack, immutable training-pair export. Gate: model probabilities and deterministic likely rail; local-unreachable fallback; document local-model quality evidence.
9. Hardening, dependency audits, CI and browser tests, performance/recall checks, README install/screenshot/operator instructions, signed commits, GitHub push, `v0.1.0` release with any explicitly deferred live checks stated.

Each milestone note records authoritative evidence, limitations, and deviations. Final audit checks every section against code/tests/runtime; green narrow tests alone are not completion evidence.

## Cross-cutting requirements to verify

- Historical snapshots, canonical hashes, soft deletion, removed-entry closure, current four-way suppression, append-only audit at ORM and SQL levels.
- Secure sessions/CSRF/login limits/admin authorization, first-admin race protection, encrypted credentials, privacy acknowledgments, secrets excluded from outputs.
- Full source record normalization and unchanged-hash behavior; all original fields retained.
- Deterministic components/candidate fallback/aliases/types/countries; no AI candidate generation or likely demotion; low items recorded and review-only auto-clear.
- All specified pages and CLI commands operational; proper errors/loading/empty states; keyboard/focus/mobile read support.
- UTC timestamps/configurable timezone, durable schedules/overlap protection, report health banners, report and alert semantics.
- Full audit exports include immutable evidence, relevant audit rows, exact ITA disclaimer, safe CSV text and safe source URLs.
- Pinned dependencies, clean runtime image, one-command deployment, encrypted-volume guidance, no telemetry or extra list/ownership scope.
