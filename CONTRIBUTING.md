# Contributing

Read [the README](README.md), [vendor contracts](docs/research/vendor-contracts.md) and [agent guidance](AGENTS.md). Keep changes focused, add regression coverage for behavioral changes, and run the checks described in the README.

Every commit requires a Developer Certificate of Origin (DCO) sign-off. By adding `Signed-off-by: Your Name <your@email.example>` with `git commit -s`, you certify the [Developer Certificate of Origin 1.1](https://developercertificate.org/). Use the identity you are entitled to contribute under. Automated CI verifies every commit in a pull request.

Use synthetic party data in issues and tests. Never upload customer books, passwords, API keys, OAuth tokens, databases, or session cookies. Report a suspected security issue privately through GitHub's security reporting tools where available.

Keep local agent/tool context, credentials, caches, generated test output and one-off experiments out of commits. This repository intentionally does not publish the maintainer's `.gitignore`. Configure your own `.gitignore` or `.git/info/exclude`; a starting policy is:

```gitignore
.env
.env.*
!.env.example
.gitignore
.21st/
.claude/
.codex/
.workspace/
context/
scratch/
artifacts/
transcripts/
screenshots/
docs/milestones/
docs/plans/
docs/reviews/
docs/design-v1.md
scripts/gates/
.venv/
node_modules/
__pycache__/
.pytest_cache/
.ruff_cache/
.mypy_cache/
*.db
*.db-shm
*.db-wal
*.sqlite*
*.log
.coverage
htmlcov/
frontend/dist/
frontend/test-results/
frontend/playwright-report/
.DS_Store
```

Maintained tests under `backend/tests`, `frontend/tests` and `tests/integration`, reusable research tools, frozen public/synthetic fixtures and README images belong in version control. Temporary context and captures belong in an ignored local workspace or outside the repository. The versioned `.dockerignore` separately excludes private and development-only files from application images.

Changes to matching behavior must preserve the published fixture bucket expectations; document any deliberate contract change and its rationale. Human dispositions and model outputs must always retain their original provenance.
