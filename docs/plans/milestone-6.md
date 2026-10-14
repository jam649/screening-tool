# Milestone 6 — QuickBooks Online connector

Authorized after milestone 5's real CSV/monitoring/restart/Admin browser gates and all five CI jobs passed: signed commit `3e527553af598ef09784f978168b0f9ee67d8bab`, [CI run](https://github.com/jam649/screening-tool/actions/runs/38514600102). Local implementation and gates are complete; [milestone 6](../milestones/6.md) records verification and separate publication/CI status. Live ERP sandbox work remains deferred by Jordan; use clearly labeled synthetic responses derived from verified official contracts, never describe them as live recordings.

## Ownership

- Connector owner: `app/connectors/qbo.py`, HTTP/normalization/full/CDC/rotation tests and `fixtures/qbo/` contract responses. Consume shared encrypted app configuration; no API/main/migration/shared sync edits.
- API/security owner: `app/connectors/qbo_common.py`, `app/connectors/qbo_auth.py`, `app/api/qbo.py`, OAuth/webhook/security tests. Define shared typed internal configuration and public flags; coordinate exact callable interfaces with connector owner.
- Coordinator: OAuth state model/migration/SQL immutability, model/router registration, source API extensions and truthful schedule health, shared source leases/pending follow-up behavior, scheduler eligibility, CLI integration and Compose origin/host environment wiring.
- Frontend owner: actual Opus 5.5 MEDIUM authoring for QBO settings and source controls/API/test bundles, Sol integration into existing Sources/Settings. Exclude credentials, environment files, customer/runtime data and raw messages from model input; raw exchanges remain ignored. Existing 21st visual grounding applies.

## Public API contracts

All routes start with `/api/v1`. Authenticated writes require active Admin and CSRF. GET callback requires the initiating live session, valid single-use state and current Admin authority. Anonymous webhook routes authenticate only through their exact raw-body vendor signature.

| Route | Contract |
| --- | --- |
| GET `/settings/connectors/qbo` | Environment credential/verifier configured flags; exact redirect URI and per-environment webhook URLs; no secret values. |
| PATCH `/settings/connectors/qbo` | `environment`, optional replacement `client_id`, `client_secret`, `verifier_token`, explicit `clear_credentials`/`clear_verifier_token`; omitted values preserve secrets. |
| POST `/sources` | Extend CSV compatibility with `kind:qbo`, label and sandbox/production environment. |
| POST `/sources/{id}/qbo/oauth/start` | Empty body to authorization URL and expiry. |
| GET `/connectors/qbo/oauth/callback` | Verified code/state/realm or vendor error; 303 to local Sources with only nonsensitive outcome/source identifiers. |
| GET/PATCH `/sources/{id}/qbo/connection` | Admin connection metadata; PATCH enables/disables optional webhooks, requiring verifier and public HTTPS origin to enable. |
| POST `/sources/{id}/sync` | Mode incremental/full to durable operation ID and queued status. |
| POST `/sources/{id}/qbo/disconnect` | Stop polling and preserve parties/history; verify official revocation contract before any remote revocation, and expose its actual outcome. |
| POST `/webhooks/qbo/{environment}` | Bounded raw signed CloudEvents array; validate before parsing, bind configured realms/entities, deduplicate and atomically enqueue polling; never apply payload as party data. |

Use one encrypted Intuit app configuration per sandbox/production environment and one Source per company. Tokens/expiry stay in encrypted Source credentials. Replacing the app client identity while connected companies remain must be rejected; ordinary same-app secret replacement remains possible. New company means new source rather than silently rebinding historical identity. Callback query credentials must never appear in access/application logs or referrer output. Document reverse-proxy equivalent.

## Durable operations and ingestion

The actual registry is `scheduler.service.register_source_handler`. A source handler currently receives a Session and Source: copy primitive metadata/secrets, roll back before HTTP, and reacquire short writer/fence/authority/generation checks only for token persistence and `apply_sync`. Do not hold SQLite's writer lock across token or data requests. Returned rotating refresh tokens must persist atomically before further requests; invalid grant marks reconnect required. Coordinate generation/concurrency rules so an accepted rotating token is not lost to an avoidable app-config race.

Full query pages enumerate Customer and Vendor, explicitly including Active true/false; namespace external IDs by entity. Preserve complete raw records and document name/type/address policy. `PartyInput.deleted` supports inactive/tombstone identity without inventing replacement party fields. Preserve section5.4 unchanged-hash no-write semantics and existing v2 hashes/frozen history.

CDC is an incremental incomplete-book batch with overlap and a conservative poll-start watermark. Beyond30 days, at1000 response saturation or unverifiable completeness, perform validated full reconciliation. Any failed/partial/malformed response preserves the original parties and cursor. Normal sync enqueues only changed/new parties; complete missing/inactive identities soft-delete. Keep periodic polling even when webhooks are enabled.

Valid signed events arriving while a poll holds its lease need a durable follow-up; busy dispatch must leave it queued for recovery. Dedup receipts and enqueue must be atomic. Expose actual next scheduled source run, not an invented last-sync-plus15-minute estimate. Poll only connected eligible sources; disconnect removes its schedule while retaining records.

Reuse encrypted Settings/Source credentials, Source config/generation/cursor and an OAuthState row (digest/source/user/session/app generation/expiry/consumed time). Coordinator owns append-only/frozen state/index guards and any webhook receipt index. Never weaken existing audit, retention or snapshot guards.

## Gate and limits

Verify actual application OAuth routes against an injected fixture HTTP transport, full pagination, token rotation, CDC edit/inactivation/reactivation, stale/capped fallback and failure atomicity, signed duplicate/multi-realm CloudEvents, active-poll follow-up, scoped screening, false-positive memory and original evidence. Check all API roles/CSRF and exclusion of credentials/code/state from logs/audit/exports. Frontend must distinguish queued sync from complete and configured credentials from a connected company.

Use only official verified endpoints in docs/research/vendor-contracts.md; record newly consulted official URLs there and in the signed commit. Production credentials require the self-hoster's Intuit app assessment; fixture validation does not establish live readiness. M7 begins only after the M6 fixture/runtime gate passes. M8/M9 and the final v0.1.0 release remain required.
