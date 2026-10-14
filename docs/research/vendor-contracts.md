# Vendor contracts verified for screening-tool

Verified 2026-10-13 against primary public documentation and the public CSL download. This is implementation research, not a claim that any credential-dependent integration passed. Jordan explicitly deferred live ERP sandboxes and requested a dummy CSV path first. No secrets were retrieved or printed. Subsequent authorized implementation calls verified the signed-in Claude CLI and exact first-party Opus 5.5 model; see the milestone provenance notes.

## CSL bulk and source metadata

The [official CSL page](https://www.trade.gov/consolidated-screening-list) links the [public bulk JSON](https://data.trade.gov/downloadable_consolidated_screening_list/v1/consolidated.json). A TLS-verified system `curl` read returned 33,844,180 bytes, SHA-256 `f9807c77aa5e2f90b9566e043859baba98189e2b48ef87637007c8020dd71ba2`, and 26,128 results. Python's default local trust store failed certificate-chain verification; TLS verification was preserved by using the system curl trust store.

The envelope is an object containing `results`, `search_performed_at`, `sources_used`, `offset`, and `total`. Parse `results`, not the top-level object as a record array. `sources_used` already supplies 13 metadata objects with `source`, `last_imported`, `source_last_updated`, and `import_rate`; the observed timestamps include explicit UTC offsets and import rate is `Hourly`. FSE metadata exists even though no current FSE rows were present. Preserve stamps from the bulk independently of the optional API.

Observed row contract:

| Published field | Meaning for the app |
| --- | --- |
| `id` | Published listing identifier; preserve exactly. IDs overlap across sources: identity is `(source_list, id)`. |
| `source` | Verbose source name, including abbreviation; use an explicit abbreviation mapping. |
| `name` | String. |
| `alt_names` | List or null; absent/null becomes an empty list. |
| `programs` | List. |
| `addresses` | Optional list of objects: `address`, `city`, `state`, `postal_code`, `country`; values can be null. |
| `country` | Optional scalar residency country string; also use address countries. Preserve nationality/citizenship as distinct raw facts. |
| `type` | Optional string; not every source supplies it. |
| `remarks` | Optional string or null. |
| `federal_register_notice` | Text citation, not necessarily a URL; observed `Vol. 59, No. 44, 03/07/1994`. |
| `source_list_url`, `source_information_url` | Published links; preserve both and allow only valid http(s) links in UI/export. |

The full-file parser smoke found **96 published IDs shared across different sources**. For example, id `17014` occurs in NS-MBS and SSI; id `9647` occurs in PLC and SDN. Keep all 26,128 rows, use source plus published id as identity, and reject duplicates only within that composite key. This corrects the design's global-ID uniqueness assumption.

Other observed fields include `ids`, `entity_number`, dates/places of birth, title, license policy/requirement, dates, standard order, and vessel fields. Retain the complete raw row for future evidence. Current result sources: CAP, CMIC, DPL, DTC, EL, ACN, NS-MBS, MEU, PLC, SDN, SSI, UVL. Do not require every metadata source to have a result.

**Version identity:** `search_performed_at` and `last_imported` are volatile envelope metadata. Whole-file hashing is required for fetch provenance, but can create versions without entry changes. Retain the raw file hash and additionally compare a canonical record-set hash for version reuse; audit successful repeated fetches. This is an implementation implication of the observed envelope, not an ITA guarantee.

[Sources API candidate](https://data.trade.gov/consolidated_screening_list/v1/sources) was read without a credential and returned HTTP 401 with a missing-subscription-key message. The [official portal onboarding](https://developer.trade.gov/) verifies subscriptions and primary/secondary keys. Sending the explicitly nonsecret placeholder `subscription-key: schema-verification-invalid` changed the response from missing to **invalid subscription key**, verifying that this exact header is recognized. Successful authenticated access and response shape remain **unverified**: the official portal's API details widget is JavaScript-rendered and did not expose operation schemas through the text browser. Confirm the successful response using its Try it tool and a user-owned key. Do not call the old `api.trade.gov` route or fabricate source dates. Bulk metadata is the verified fallback.

## Exact ITA notice

The following government text is copied verbatim from [ITA Terms of Service, Consolidated Screening List Scoring](https://developer.trade.gov/terms-of-service). Use it for the first-run acknowledgment and export/report footers; link the source. The preceding two sentences on the page explain the hosted API's fuzzy-name search, which this application does not use.

> The International Trade Administration does not provide endorsements or recommendations with regard to any specific CSL search result or score. The CSL is one tool offered to assist users in identifying potentially sanctioned companies, entities, or persons and is not a substitute for undertaking appropriate due diligence. In the event that a company, entity, or person is identified by a CSL search result or score, users should conduct additional due diligence and make their own risk-based determinations before proceeding. Use of the CSL does not limit any criminal or civil liability for any act undertaken as a result of, or in reliance on, such use.

## EAR retention

[15 CFR 762.6](https://www.ecfr.gov/current/title-15/subtitle-B/chapter-VII/subchapter-C/part-762/section-762.6), displayed current through 2026-10-08, confirms five years for EAR-required records from the **latest applicable transaction event**, including export/services, known reexport/transfer/transshipment/diversion, transaction termination, or the specified boycott request. A formal or informal government request prevents destruction without agency written authorization, even beyond five years.

Keep indefinite retention as the default. A five-year configuration floor is a product safeguard; screening-date age alone does not prove compliance with transaction-based deadlines. Any future purge requires dependency preservation and a legal-hold mechanism or must remain disabled when a hold cannot be excluded. The tool does not observe all transaction events.

## QuickBooks Online

The [official OAuth documentation, static rendering](https://static.developer.intuit.com/output_html/qbo/docs/develop/authentication-and-authorization/oauth-2.0.html) was read with TLS-verified curl because the interactive developer site returned a JavaScript shell. Authorization: `https://appcenter.intuit.com/connect/oauth2`; token exchange/refresh: `https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer`; scope: `com.intuit.quickbooks.accounting`. Authorization code flow uses a registered matching redirect URI, random checked `state`, and callback `realmId`. Token endpoint uses form encoding and Basic client authentication. Access tokens last 3,600 seconds; refresh expiry rolls over 100 days. Atomically store each returned refresh token and expiry metadata, and serialize concurrent refreshes. The current documentation also exposes optional hard-expiry metadata; consume actual response fields instead of assuming perpetual refresh.

[Query contract](https://static.developer.intuit.com/output_html/qbo/docs/learn/explore-the-quickbooks-online-api/data-queries.html), read with curl, permits `GET /v3/company/{realmId}/query?query=...`, with `STARTPOSITION` and `MAXRESULTS`. Default size is 100, maximum 1,000. Enumerate each entity until exhausted. Active/inactive list objects require explicit `WHERE Active IN (true, false)` when reconciling all records. Namespaces must include entity type because Customer and Vendor IDs can overlap. Map inactive objects to local soft deletion; map reactivation back to active.

[CDC documentation](https://static.developer.intuit.com/output_html/qbo/docs/learn/explore-the-quickbooks-online-api/change-data-capture.html) and [Intuit's official CDC article](https://medium.com/intuitdev/building-smarter-with-intuit-stay-in-sync-with-cdc-171f8094dd37) confirm `GET /v3/company/{realmId}/cdc?entities=customer,vendor&changedSince={timestamp}`, a 30-day lookback, full changed-entity payloads, and a 1,000-object response cap. Shape: `CDCResponse[] -> QueryResponse[] -> Customer[] / Vendor[]`; deleted entities can carry `status: Deleted`. Do not advance the cursor on incomplete or capped results. Use full reconciliation after a gap beyond the window, and fail/reconcile on saturation rather than silently skipping changes. No pagination contract for CDC was verified; query pagination belongs to full enumeration.

[Webhook configuration and signatures](https://static.developer.intuit.com/output_html/qbo/docs/develop/webhooks/configure-webhooks.html): compare `intuit-signature` with Base64(HMAC-SHA256(verifier-token, **raw request body bytes**)) using constant-time comparison. Configure sandbox/production verifier tokens separately. Reject invalid signatures before parsing or scheduling. Valid events should trigger a CDC poll and be idempotent, with the configured realm enforced.

[Mandatory CloudEvents migration](https://medium.com/intuitdev/upcoming-change-to-webhooks-payload-structure-2a87dab642d0) extended its deadline to **2026-07-31**. Current webhook payload is a JSON **array** of events with `id`, `type` (such as `qbo.customer.updated.v1`), `time`, `intuitentityid`, and `intuitaccountid` (realm), plus CloudEvents fields. Support this contract; legacy `eventNotifications` is optional compatibility, not the current sole format. Five-minute delivery is not a latency guarantee.

[Official production/private-use requirements](https://static.developer.intuit.com/output_html/qbo/docs/go-live/publish-app.html), read with curl, require Production App Details (including EULA/privacy and application URLs), profile, OAuth setup, and **approved App Assessment Questionnaire** before Show Credentials appears. The page explicitly allows private use after production credentials; marketplace listing is separate. This supports a self-hoster-owned app path, but does not guarantee any specific applicant's approval. [Partner Program guide](https://static.developer.intuit.com/resources/Intuit_App_Partner_Program_Guide.pdf) distinguishes production assessment from additional marketplace security/technical/marketing reviews. Document current quotas/billing from the applicant's dashboard before production use; do not promise unlimited free calls.

## Business Central

[Microsoft S2S documentation](https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/administration/automation-apis-using-s2s-authentication): unattended polling can use OAuth client credentials. Register an Entra application; give Dynamics 365 Business Central **Application** permission `API.ReadWrite.All` for standard APIs, obtain tenant consent, and create/enable its Client ID on the BC **Microsoft Entra applications** page with restricted object permissions. `Automation.ReadWrite.All` is for automation APIs; the setup article lists both but standard customer/vendor polling does not call that route. Applications cannot receive `SUPER`. Exact object permission sets must be validated against the user's tenant.

Token endpoint: `https://login.microsoftonline.com/{tenantId}/oauth2/v2.0/token`, form `grant_type=client_credentials`, client ID/secret, scope `https://api.businesscentral.dynamics.com/.default`. Full standard API base: `https://api.businesscentral.dynamics.com/v2.0/{tenantId}/{environment}/api/v2.0`; resources `companies({companyId})/customers` and `/vendors`. Client-credentials setup needs no user authorization redirect unless BC consent is granted through the web-client flow. Separate delegated OAuth from S2S in the UI.

[Customer](https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/api-reference/v2.0/resources/dynamics_customer) and [vendor](https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/api-reference/v2.0/resources/dynamics_vendor) expose `id`, `displayName`, address/country fields, and `lastModifiedDateTime`. Customer `type` supports Company/Person; vendor standard schema has no equivalent type field. `blocked` controls posting restrictions, not deletion or an Active flag: keep blocked parties eligible for screening unless the product explicitly chooses another rule. Poll with URL-encoded `$filter=lastModifiedDateTime gt {cursor}`, overlap/deduplicate boundaries, and commit the cursor only after all pages succeed. This is a derived strategy, requiring sandbox validation for timestamps/ties.

[Server-driven paging](https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/webservices/server-driven-paging-in-odata-web-services) documents a 20,000 maximum/default and `Prefer: odata.maxpagesize=300`. Follow OData continuation links and ensure each stays within the configured trusted BC origin. Hard-deleted records no longer appear in timestamp queries: periodic successful full ID reconciliation is necessary when using polling alone; never infer deletion from a failed or partial sync.

[BC webhook lifecycle](https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/api-reference/v2.0/dynamics-subscriptions): `POST /api/v2.0/subscriptions` with notification URL, resource and optional secret `clientState`; create **and PATCH renewal** require echoing query `validationToken` in HTTP 200 response body. Subscriptions expire after **three days**; store `expirationDateTime` and renew well beforehand (daily is an implementation choice). Verify `clientState` on notifications and handle created/updated/deleted/collection, using deletion identity directly or full reconciliation. Default notification delay is 30 seconds. Delivery retries extend up to 36 hours only for 408, 429, or 5xx; other errors can delete the subscription. Retain polling even with webhooks. Sandbox still must confirm ETag/If-Match renewal behavior and deletion events.

## Jev and Laya

Applied the available `typesafe-ai` skill and read its [live index](https://docs.typesafe.ai/llms.txt), [HTTP API](https://docs.typesafe.ai/api), and [Noul contract](https://docs.typesafe.ai/primitives/noul). Endpoint: `POST https://api.typesafe.ai/v1/systemone`, Bearer API key, JSON. Required `state` (string/object/array), `model`, and a named `questions` map. For a pair:

```json
{
  "state": {"party": {}, "listing": {}, "components": {}},
  "model": "jev-1.13.0",
  "questions": {
    "same_party": {
      "type": "noul",
      "instructions": "These two records refer to the same party."
    }
  }
}
```

Read `answers.same_party.noul` as a finite number in [0,1], confirm its `type`, and preserve the response model ID and token usage. Noul has no separate Jev confidence field. Invalid schema/nonfinite/out-of-range answers must cause deterministic fallback, not a zero score.

[Current models](https://docs.typesafe.ai/models): `jev-1.13.0`; `jev-latest` and `jev-preview` currently alias it. Budget 64k tokens for state plus all questions, and 32k for state plus longest question. Current published price $0.042/million input tokens; outputs free; published limits 80 requests/s and 100k tokens/s can change. Pin the model for reproducible evaluations. [Quickstart](https://docs.typesafe.ai/introduction/quickstart) directs users to the console for a key; actual account access/billing was not tested. Keep AI optional and do not assert that early-access enrollment is guaranteed or blocked.

[Laya publisher model card](https://huggingface.co/convaiinnovations/laya/raw/main/README.md), read via curl, confirms Apache 2.0, Python >=3.10, `pip install "laya[serve]"`, and `laya-serve` on `POST /v1/systemone`. Checkpoints: root English 512 tokens, multilingual 1,024 default with explicit `max_len=8192` support, typed-decisions 1,024. Defaults allocate 192/256 tokens to the question head, leaving approximately 320/768 state tokens. Token budgets include instructions; character-count trimming is not proof the state fits. Preserve the design's ordered trimming and record truncation.

[Publisher serving source](https://raw.githubusercontent.com/NandhaKishorM/laya/main/laya/serve.py) accepts the same state/questions shape and additional `model`/`max_len` controls. It binds 0.0.0.0:8000 without auth by default; configure local binding or `LAYA_API_KEY`. `LAYA_JEV_STRICT=1` projects responses to the Jev schema. Explicit `model: multilingual` avoids long English inputs selecting the 512-token checkpoint. The source reports caps for body/state/question counts and optional token budgets; use a pinned runtime and test its actual HTTP schema.

The publisher explicitly warns base checkpoints perform near chance on its typed-decisions benchmark and specialization improves results. Those results do not establish screening quality. Keep deterministic safety rails and require fixture/held-out comparison before trusting the default blend. This initial contract research did not download weights. The subsequent [local Laya validation](laya-validation.md) ran pinned weights against 120 labeled probes and a stress case. Its screening-quality results support deterministic weighting by default for the unspecialized local checkpoint.

## Deferred verification

1. Confirm successful ITA sources authentication and response shape with a user-owned subscription key; preserve bulk stamps if unavailable.
2. QBO sandbox: OAuth refresh rotation, full query pagination/inactive records, CDC caps and tombstones, CloudEvents signatures; production assessment eligibility for the self-hoster.
3. BC sandbox: exact tenant object permissions, pagination/timestamp boundaries, deletion reconciliation, webhook handshake, ETag renewal and expiry recovery.
4. Optional Jev key/account check via models listing before any billed inference. The local Laya pinned-runtime and fixture-quality benchmark is complete; repeat calibration on representative held-out decisions before enabling a nonzero local-model blend.
5. Review purge eligibility against transaction-linked retention and holds before enabling automated deletion.

These are remaining external/manual validations, not reasons to block the authorized local CSV implementation. The parent agent records the concrete follow-ups in the shared projects todo.

## Matching and spreadsheet dependencies

Verified public package metadata through PyPI on 2026-10-13 and pinned the implementation to rapidfuzz 3.14.6 (MIT), text-unidecode 1.3 (Artistic License option), pycountry 26.2.16 (LGPL-2.1-only), Metaphone 0.6 (BSD), openpyxl 3.1.5 (MIT), python-multipart 0.0.32 (Apache-2.0), and defusedxml 0.7.1 (PSFL). The container must preserve applicable package license notices; the parent integrator owns `THIRD_PARTY_LICENSES.md`.

The specified Unidecode package is [GPLv2+ upstream](https://github.com/avian2/unidecode). The parent approved [text-unidecode](https://github.com/kmike/text-unidecode) under its Artistic option as the implementation substitute, with the same `unidecode()` function and retained original Unicode fields. This is a documented dependency deviation, not a change to the deterministic matching contract. [openpyxl's security guidance](https://openpyxl.readthedocs.io/en/stable/) recommends defusedxml; the implementation pins it and verifies openpyxl activates its safe XML path. Spreadsheet zip expansion and row/cell/upload limits are enforced independently.
