# screening-tool

Self-hosted customer and vendor screening against the U.S. Consolidated Screening List (CSL). Python/FastAPI/SQLite and React, in one container. MIT licensed.

**Implementation is underway.** The secure setup wizard, CSL pipeline, CSV/XLSX import, deterministic matching, keyboard review, party history, users, persistent monitoring, SMTP reports, PDF/CSV/ZIP exports and bounded retention have passed the real Docker and browser gates through milestone 5. QuickBooks OAuth, full/CDC sync, signed webhooks and revocation have passed the packaged fixture, browser and restart gates in milestone 6. Business Central company discovery, full/incremental sync, validated subscriptions and cleanup have passed the fresh packaged API, browser and retained-volume restart gates in milestone 7, with all seven CI jobs passing. Optional model settings, human training-pair export and the real local Laya application gate have passed milestone 8’s local API, browser and restart checks; signed publication and CI are being checked. Release hardening remains in [the implementation plan](docs/plans/implementation.md). Live QuickBooks and Business Central sandbox validation is deferred by the project owner; fixture checks do not establish production integration readiness.

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

## QuickBooks Online

Live Intuit sandbox validation is deferred; the connector is tested against synthetic responses derived from [official contracts](docs/research/vendor-contracts.md#quickbooks-online) and the real packaged application. Use your own Intuit developer app and validate it in a sandbox before production. Production credentials require Intuit's App Assessment; marketplace listing is a separate process.

1. Set the application's public base URL in **Settings → Email notifications**. Use public HTTPS for a remotely accessible deployment. Loopback HTTP is supported for a local OAuth callback.
2. Open **Settings → QuickBooks app settings**, choose Sandbox or Production, and save that environment's client ID and secret. Register the exact displayed redirect URI in Intuit. Saved credentials are encrypted and displayed as configured flags.
3. In **Sources**, add a QuickBooks source for each company, choose the same environment, and authorize it through Intuit while signed in as the initiating Admin. An app's configured keys and a company's authorized connection are separate states. Each source keeps its historical company identity after disconnect.
4. Connected sources poll customers and vendors on the monitoring schedule. **Sync changes** reads CDC; **Full sync** reconciles the whole book, including inactive records. Operations remain queued while a source is busy; their eventual counts and screening result appear after completion.
5. Webhooks are optional. Configure the environment's verifier token and displayed webhook URL in Intuit, then enable notifications on the source. This requires a connected company and a public HTTPS base URL. Signed notifications request an earlier poll; scheduled polling remains enabled.

Disconnect stops polling and preserves parties, reviews and history. Its status reports whether the remote grant was revoked, failed, or did not require revocation. A failed revoke retains encrypted retry material and exposes **Retry Intuit revocation**. Reconnect is required when refresh authorization expires or is withdrawn.

For an operator full reconciliation:

```bash
docker compose exec screening-tool screening-tool sync SOURCE_UUID --full
```

The [milestone 6 evidence record](docs/milestones/6.md) distinguishes synthetic contract checks, actual application checks and pending live sandbox checks. HTTP access logs are disabled; configure your reverse proxy to omit OAuth query strings as well. Callback responses use `Referrer-Policy: no-referrer`.

## Business Central

Live tenant validation remains deferred. The connector uses Microsoft's documented [service-to-service app authentication](https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/administration/automation-apis-using-s2s-authentication); company selection takes place inside this application.

1. Register your Entra app, create a client secret and grant the Business Central application permission `API.ReadWrite.All` with tenant consent. In Business Central's **Microsoft Entra applications** page, enable that application and assign the object permissions needed for company/customer/vendor reads and subscription management. Microsoft prohibits assigning `SUPER` to applications. The precise restricted permission set must be checked in your tenant before live use.
2. In **Settings → Business Central app settings**, save the application UUID and secret. One encrypted app configuration serves the sources. Saved values appear as configured flags; omitted fields preserve them.
3. In **Sources**, add a Business Central source with its tenant UUID and actual environment name, discover accessible companies, and explicitly connect the selected company. One source serves one company. The bound company identity remains permanent after disconnect; another company needs another source.
4. Connected sources poll changes using an overlapping timestamp filter. A daily full reconciliation detects hard deletions; **Full sync** requests one immediately. Posting restrictions such as `blocked` leave a party active for screening.
5. For optional webhooks, configure a public HTTPS base URL in **Settings → Email notifications** and enable them on the connected source. The app registers customer/vendor subscriptions, validates each challenge and verifies each delivery's private client state. Actual subscription expiries and failed cleanup are visible. A persistent 12-hour reconciliation job renews subscriptions when due; polling continues.

Disconnect stops polling immediately and preserves records and review evidence. It removes the app's remote webhook subscriptions and reports failures with an encrypted retry obligation. Entra app consent remains under your tenant administrator's control. Configure your reverse proxy to omit webhook validation query strings; application HTTP access logging is disabled.

The [milestone 7 record](docs/milestones/7.md) tracks synthetic contracts, application gates and deferred live checks separately.

## Optional models and training pairs

Deterministic screening works without an API key. In **Settings → Model settings**, select None, local Laya or hosted Jev. Defaults are `w_det=1`, `w_model=0`: the measured base Laya checkpoint did not discriminate these screening identities reliably. An enabled model can record its probability while scores remain deterministic. Use a positive model weight only after checking representative held-out human decisions. A strong deterministic match always stays in the likely bucket; unavailable or invalid inference keeps the deterministic score and records a fallback.

For Laya, run its server separately on this machine with the verified multilingual checkpoint and 1024-token limit; see [the pinned runtime and quality report](docs/research/laya-validation.md) and [serving contracts](docs/research/model-adapters.md#local-laya). In the Compose app, use `http://host.docker.internal:8009` for a host server on port 8009; a noncontainer development backend can use `http://127.0.0.1:8009`. The endpoint accepts only this host's loopback or explicit Docker host bridge. The application includes the exact offline tokenizer, not model weights or a Laya server. Keep the local server private; an optional Laya key is write-only. Save the configuration before using **Test saved configuration**, which sends synthetic identity fields and reports actual model/usage or a fixed failure code. Changing a draft does not test or save it.

For Jev, obtain your own TypeSafe account/key; entitlement and billing are not verified by this project. Before saving the key, acknowledge the displayed sentence: “Enabling Jev sends your customer and vendor names and addresses to TypeSafe AI for scoring. Laya keeps all data on this server.” The adapter uses the fixed `jev-1.13.0` contract. Keys remain encrypted and display only configured flags; explicit clear controls remove them. Provider and weight changes queue a full screening and retain original settings on previous runs.

Export retained human labels for a separate fine-tuning workflow:

```bash
docker compose exec screening-tool screening-tool export training-pairs --output /data/training-pairs.csv
docker compose cp screening-tool:/data/training-pairs.csv ./training-pairs.csv
```

This file contains commercially sensitive identity evidence. Keep it private. Select rows with `row_type=training_pair`; the `manifest` row contains counts and the ITA notice, not a label. Only actual human false-positive and confirmed-match decisions are included, with original snapshots, actor and fanout provenance. Missing or purged evidence is counted as unavailable. Running a fine-tune is outside v1. [Milestone 8](docs/milestones/8.md) distinguishes synthetic-provider gates from actual local inference and unverified hosted access.

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

This tool is one control in a compliance program, not the program. False negatives are possible. CSL data does not provide government endorsement of a result; users must conduct due diligence and make their own risk-based determinations. The complete ITA notice appears in setup and accompanies every export and report. HTTP access logs are disabled in the container so OAuth callback query credentials and party-search terms are not written to ordinary server logs; configure any reverse proxy to log paths without query strings.

Database upgrades preserve existing records. Destructive schema downgrades deliberately refuse to erase the audit trail. A stored encryption-key fingerprint makes a changed key fail at startup instead of silently making old credentials unreadable.
