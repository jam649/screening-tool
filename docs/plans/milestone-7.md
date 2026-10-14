# Milestone 7 — Business Central

Implementation follows the locally verified QuickBooks checkpoint and its signed corrective commit `eb663cd4ce8130ffcd594a114fbb90e24bd18c59`. All six jobs in [milestone 6 CI](https://github.com/jam649/screening-tool/actions/runs/38525206949) passed on 2026-10-14; milestone 7 implementation, independent review and fresh packaged API/browser/restart gates have now passed. Signed publication and seven-job CI remain pending. This plan is not a completion claim. Live tenant/sandbox validation remains deferred by Jordan; responses and gates are explicitly synthetic.

## Ownership and supported authentication

- Core owner: `app/connectors/bc.py`, `tests/test_bc.py`, `fixtures/bc/`. Own fixed-vendor HTTP/token handling, trusted continuation chains, normalization, full/incremental ingestion and fenced apply. Do not edit shared scheduler/API/main/migrations.
- Security/API owner: `app/connectors/bc_common.py`, `bc_auth.py`, `bc_subscriptions.py`, `app/api/bc.py`, corresponding tests. Own encrypted app/token/subscription state, company discovery/activation, anonymous validation/notifications and lifecycle reconcile/cleanup.
- Coordinator: Source create schema/identity migration and guards; root router/registry/transport-loader integration; lease groups and transactional command queues; scheduler renewal eligibility; CLI compatibility; runtime and CI gates.
- Frontend owner: actual Opus 5.5 MEDIUM source-only authoring of native Settings/source controls, API hooks and focused tests; Sol integration into the existing 21st-grounded interface. Raw exchanges remain ignored; credentials/runtime/customer data are excluded from model input.

Use one encrypted self-hoster Entra app configuration with client ID/secret/generation and configured-only Admin public flags. One Source per company has explicit tenant UUID, actual environment name and an initially unbound company UUID. Separate source identity from app configuration. A source may discover companies after app setup, then bind permanently when an Admin connects the selected company. Same-identity secret replacement aligns encrypted token generations only while all affected leases are idle; replacing the app identity requires sources holding credentials/cleanup obligations to disconnect first.

Supported BC auth is documented application client credentials, not a user authorization redirect: `POST https://login.microsoftonline.com/{tenantId}/oauth2/v2.0/token`, form client ID/secret, `grant_type=client_credentials`, scope `https://api.businesscentral.dynamics.com/.default`. Standard APIs use Application `API.ReadWrite.All`, tenant consent and enabled BC Entra application with restricted object permissions; no `SUPER`, no invented app logout/revoke endpoint, no refresh token. Explicitly document this architecture adjustment from the design's generic OAuth redirect wording.

Tenant/company/entity UUID-only acceptance is this product's restriction; Entra also supports domain tenants. Environment ASCII names start with a letter, contain letters/digits/underscore/dash, and are fewer than 30 characters. Preserve routing case. All operations pin `https://api.businesscentral.dynamics.com/v2.0/{tenant}/{environment}/api/v2.0`; arbitrary API URLs are not configurable.

## Public routes and source identity

Prefix all routes `/api/v1`. Authenticated writes require active Admin/CSRF and late authority/fence checks; public metadata never returns app IDs, secrets, Bearer tokens or client state.

| Route | Contract |
| --- | --- |
| GET/PATCH `/settings/connectors/bc` | Configured app flags and webhook base URL; replacement client ID/secret or explicit clear, omitted values preserve secrets. |
| POST `/sources` | Add `kind:bc`, label, tenant UUID and `bc_environment`; no required company before discovery. CSV/QBO remain compatible. |
| GET `/sources/{id}/bc/connection` | Admin identity/status/credential flags and subscription health/actual expiries. |
| POST `/sources/{id}/bc/companies` | Empty body; leased vendor read returns only `{items:[{id,display_name}]}`. |
| POST `/sources/{id}/bc/connect` | `{company_id}`; verify selected company, bind it, save token metadata and atomically queue a full source sync; return connection plus standard queued attempt. |
| PATCH `/sources/{id}/bc/connection` | `{webhooks_enabled}`; require connected company and public HTTPS to enable; queue lifecycle reconcile. |
| POST `/sources/{id}/bc/subscriptions/reconcile` | Empty body to a durable queued source lifecycle operation. |
| POST `/sources/{id}/bc/disconnect` | Stop polling immediately, preserve parties/history, queue remote subscription cleanup and expose its actual outcome. No fabricated OAuth revocation. |
| POST `/webhooks/bc/{source_id}` | Anonymous bounded subscription challenge, or bounded client-state-verified notifications. |

Root owns immutable bound tenant/environment/company guards and a unique company identity index. Environment uniqueness is conservatively case-folded as a product restriction while routing preserves its configured case; this is not a claim that Microsoft routing is case-insensitive. Source labels remain editable. Company changes require a new source. Reviewer sees source health/environment/connection status without querying Admin connector metadata. Use an additional public `bc_environment` field so the existing QBO environment enum remains exact.

## Ingestion and concurrency

Agree shared `BCClient`, `BCConfig`, `token_credentials` and `sync_bc_source(db, config, source, *, mode='incremental', client=None, transport=None)` interfaces before edits. Core consumes security owner's encrypted app/source loaders; session-factory `bc_transport_loader` exists only in process memory. Production registry defaults preserve explicitly injected handlers.

Namespaced entity identities are `Customer:{canonical-guid}` and `Vendor:{canonical-guid}`. Customer Company/Person maps to company/individual; standard Vendor defaults company. Posting `blocked` flags never soft-delete. Preserve original fields and design §5.4 unchanged-hash no-write behavior. Full pages request `Prefer: odata.maxpagesize=300` without `$top=300`, follow actual `@odata.nextLink`, and validate both chains before complete reconciliation. Optional `@odata.count` must be nonnegative integer/no bool and agree with cumulative results; absent count is normal.

Continuation must resolve to HTTPS fixed origin and the exact configured tenant/environment/company/entity collection path. Reject userinfo/ports/fragments/control characters/backslashes/encoded path delimiters, changed paths, redirects, loops, duplicate identities and page/byte/record cap exhaustion before sending another Bearer request. Preserve an accepted opaque query unchanged; do not manufacture skip tokens.

Incremental filtering uses `lastModifiedDateTime gt` a five-minute-overlapped lower bound, with a conservative poll-start cursor. Same-timestamp rows and pagination ties must remain covered. Compare anomalous future modification timestamps to page-observed UTC plus bounded skew; legitimate concurrent changes may be later than poll start. BC timestamps are not a retained CDC log; never import QBO's 30-day rule. A daily successful full reconciliation is a product policy for hard deletions, plus full mode for missing/invalid/future cursors and deletion/collection notifications. Store `last_full_sync_at` only with a successful fenced complete apply.

No SQLite writer spans token, page, company or subscription HTTP. Source operations, configuration-sensitive discovery/activation, renewal and disconnect share the source lease. Cache access tokens under generation/identity/fence checks; no rotating refresh response obligation exists. Before party/cursor mutation, recheck active initiating Admin where present, connected status, bound identity, app/source generation and token/config ownership inside `apply_sync(before_apply=...)`. System scheduled work remains distinct from a revoked user's new activation authority.

## Subscription lifecycle and receipts

Register one subscription per customers/vendors resource. Use `POST B/subscriptions` JSON `{notificationUrl,resource,clientState}` and store actual `subscriptionId` (opaque string), `expirationDateTime`, exact ETag and private client state. Renew with `PATCH B/subscriptions('{escaped-string-key}')` and required `If-Match`; delete with the same quoted key, required `If-Match`, no body. Create/renew/delete success statuses are 201/200/204. Do not copy stale v1.0 examples from otherwise-current CRUD docs.

Create and renewal validate through anonymous **POST** `notificationUrl?validationToken=...`, possibly empty body and optional ClientState header. Echo the decoded bounded token as sole 200 text/plain UTF-8 body within five seconds, with no-store/no-referrer/nosniff; scrub the query before URL construction or error handling. Do not require JSON/body or assume that optional header authenticates a challenge. The challenge has no state-changing effect.

Actual notifications are `{value:[...]}` with subscription ID/clientState/resource/changeType/lastModifiedDateTime. Compare each secret in constant time to registered encrypted source state; enforce exact source/company/resource identity. Never fetch a supplied resource URL or apply payload data. Receipt digest and coalesced queued poll commit atomically; a running poll cannot absorb its later receipt. Deleted/collection signals request full reconciliation. Valid delivery storage failure returns retryable 5xx; invalid messages remain generic and never expose secret values.

Subscriptions expire after three days. Reconcile every 12 hours (product policy), retaining normal source polling. Missing/expired subscriptions, renewal 404/conflict/precondition and uncertain create timeout require bounded trusted-list reconciliation before recreate/retry; preserve ETags and avoid blind duplicate POST. BC retries only 408/429/5xx for up to 36 hours. Accepted subscriptions surviving issuer offboarding or disconnect become encrypted cleanup obligations without authorizing source activation or party mutation. Disconnect stops schedules immediately and retains cleanup credentials on remote failure.

## Gate and verified references

Verify actual packaged anonymous TCP challenge during create/renew, two resources, key quoting/ETags, renewal/expiry/missing/ambiguous/conflict recovery, invalid batched client states, duplicate/held-lease durable receipts, hard deletion/full reconciliation, multi-company path isolation, writer-free HTTP, late authority/fence checks, strict partial paging atomicity, type-aware scoped screening/original FP provenance, actual roles/UI/export privacy and retained-volume restart. Await original task completion; unchanged imports may finish older work but must create no new task or party write. Never reset a volume to manufacture a pass.

Primary sources newly checked in M7 planning:

- [Endpoint structure](https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/webservices/api-endpoint-structure), [environment constraints](https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/administration/administration-center-api_environments), [Entra flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow), [S2S permissions](https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/administration/automation-apis-using-s2s-authentication).
- [Customer](https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/api-reference/v2.0/resources/dynamics_customer), [vendor](https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/api-reference/v2.0/resources/dynamics_vendor), [paging](https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/webservices/server-driven-paging-in-odata-web-services), [OData continuation](https://learn.microsoft.com/en-us/odata/webapi-8/fundamentals/server-driven-paging).
- [Subscription lifecycle](https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/api-reference/v2.0/dynamics-subscriptions), [resource schema](https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/api-reference/v2.0/resources/dynamics_subscriptions), [create](https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/api-reference/v2.0/api/dynamics_subscriptions_create), [renew](https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/api-reference/v2.0/api/dynamics_subscriptions_update), [delete](https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/api-reference/v2.0/api/dynamics_subscriptions_delete), [BC-linked Microsoft handshake guideline §15.5](https://github.com/microsoft/api-guidelines/blob/vNext/graph/Guidelines-deprecated.md#155-verifying-subscriptions).

Live restricted permissions, continuation encoding/casing, timestamp precision/skew and subscription ETag/delivery variants remain deferred sandbox checks. M8/M9 and the v0.1.0 release remain required; this plan does not infer live readiness.
