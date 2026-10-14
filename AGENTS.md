# screening-tool

Read `README.md` and the applicable public research and connector contracts under `docs/research/`. When present locally, the maintainer's `docs/design-v1.md` and `docs/plans/implementation.md` supply additional working context. Follow local shared agent instructions when provided by the maintainer.

- Python 3.12, FastAPI, SQLite, SQLAlchemy/Alembic; React 18, Vite, TypeScript.
- Deterministic matching works without AI. Jev and local Laya are optional adjudicators, never candidate generators.
- CSV, QuickBooks Online, and Business Central are the only v1 connectors. One Compose service and one data volume.
- Multi-user Admin/Reviewer, encrypted secrets, immutable audit trail, soft-deleted parties, retention never below five years and disabled by default.
- MIT license. Every commit must include DCO sign-off (`git commit -s`).
- Keep the display name configurable in one backend setting.
- Run the relevant tests before claiming a milestone complete. Record build, verification, deviations, and failure modes in `docs/milestones/<n>.md`.
- Never invent vendor endpoints. Verify official vendor documentation, record URLs in `docs/research/vendor-contracts.md` and the relevant commit message.
- Never weaken audit or privacy protection to pass tests. When the specification is silent, prefer recording more audit context.
- Use ordinary plans under `docs/plans/`. Retired Claude skills, `/pm`, and Superpowers workflows do not apply.
- Explicit user direction: use 21st for UI design; balance Claude and Codex agents; work autonomously and ship to GitHub. On 2026-10-13 the user deferred live QuickBooks/BC sandbox checks and requested dummy CSV data now. Implement and validate connector contracts against fixtures; label live validation as pending rather than blocking later build milestones.
- Preserve real historical party/entry snapshots in audit exports. Do not reconstruct past decisions from mutable present records.
- Never commit credentials, local databases, generated customer data, or runtime logs.
