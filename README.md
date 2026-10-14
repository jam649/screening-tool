# screening-tool

Self-hosted customer and vendor screening against the U.S. Consolidated Screening List (CSL). Python/FastAPI/SQLite and React, in one container. MIT licensed.

**Implementation is underway.** The secure setup wizard, CSL pipeline, CSV/XLSX import, deterministic matching, keyboard review, party history, users, settings, and audit views are working and have passed the real Docker browser gates through milestone 4. Scheduling, reporting, audit exports, ERP adapters, optional models, and release hardening are being completed in order, as tracked in [the implementation plan](docs/plans/implementation.md). Live QuickBooks and Business Central sandbox validation is deferred by the project owner; fixture checks do not establish production integration readiness.

## Start locally

Install Docker with Compose, then:

```bash
git clone https://github.com/Jam649/screening-tool.git
cd screening-tool
# Generate an encryption key without installing Python on the host.
docker run --rm python:3.12-slim-bookworm python -c 'import base64,secrets; print("SCREENING_TOOL_SECRET_KEY="+base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())' > .env
chmod 600 .env
docker compose up --build -d --wait
```

Open **http://localhost:8000**, create the first administrator (password at least 12 characters), and acknowledge the ITA notice. The service binds to loopback by default. Use a TLS reverse proxy for access from other machines: session cookies are always Secure, HttpOnly, and SameSite=Lax. Chromium permits Secure cookies on localhost; remote plain HTTP cannot carry the login session.

Keep `.env` private and back up its key with the data volume. The key encrypts connector/model/SMTP credentials, not the SQLite database itself. Put the Docker data volume on an encrypted disk. Losing the key makes stored credentials unreadable; generate it once per deployment and keep it across upgrades.

Set `SCREENING_TOOL_ALLOWED_HOSTS` to a JSON array of your public hostnames and `SCREENING_TOOL_ALLOWED_ORIGINS` to the exact HTTPS origins (also JSON). The default only accepts localhost/loopback hosts.

A production deployment should forward `X-Forwarded-Proto` through a trusted TLS proxy and restrict direct access to the internal HTTP port. Run only one API worker because the scheduler is inside the process. Do not expose the initial wizard until you are ready to create the Admin.

## Development checks

Use Python 3.12 and `uv` 0.11.31 or newer, Node 22.12+ and npm:

```bash
cd backend
uv sync --frozen --extra dev
uv run ruff check .
uv run mypy app
uv run pytest
uv run pip-audit
cd ../frontend
npm ci
npm run lint
npm run typecheck
npm run build
npx playwright install chromium
npm test
npm audit
```

The real first-run browser gate requires an isolated fresh database, then `PLAYWRIGHT_BASE_URL=http://localhost:8000 npm test -- tests/setup.spec.ts` from `frontend/`. The standard browser suite uses mocked API contracts; the real test is skipped unless a deployment URL is provided.

[Design specification](docs/design-v1.md) · [Implementation plan](docs/plans/implementation.md) · [Vendor verification](docs/research/vendor-contracts.md) · [Contributing and DCO](CONTRIBUTING.md)

This tool is one control in a compliance program, not the program. False negatives are possible. CSL data does not provide government endorsement of a result; users must conduct due diligence and make their own risk-based determinations. The complete ITA notice appears in setup and will accompany every export. HTTP access logs are disabled in the container so OAuth callback query credentials and party-search terms are not written to ordinary server logs; configure any reverse proxy to log paths without query strings.

Database upgrades preserve existing records. Destructive schema downgrades deliberately refuse to erase the audit trail. A stored encryption-key fingerprint makes a changed key fail at startup instead of silently making old credentials unreadable.
