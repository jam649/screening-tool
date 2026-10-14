# screening-tool: Design Document v1

Oct 13, 2026 · @Jordan Mallory

## 1. Purpose and scope

screening-tool is a self-hosted, open-source application that screens a company's customers and vendors against the U.S. government's Consolidated Screening List (CSL), re-screens them whenever the list or the party record changes, and keeps a per-party audit file that a compliance officer can hand to an auditor. Repository: [github.com/Jam649/screening-tool](https://github.com/Jam649/screening-tool). This document is the build specification for coding agents; where it says "must" the agents implement it as written, and where it says "default" the value is configurable in Settings.

**1.1 Who it is for.** A compliance officer, controller, or export administrator at a company with roughly 500 to 20,000 customer and vendor records in QuickBooks Online or Dynamics 365 Business Central, who today either pays an incumbent screening vendor or screens by hand. The user is not an engineer. A second audience is the IT person who runs `docker compose up` once.

**1.2 What v1 does.**

- Ingests party records (companies and individuals) from CSV upload, QuickBooks Online, and Business Central.
- Maintains a local copy of the CSL from the official bulk download, versioned by hash.
- Screens every party against the CSL with deterministic scoring, optionally refined by a System One decision model (Jev hosted, or Laya local).
- Routes candidates into a review queue, records human dispositions, remembers cleared false positives, and optionally auto-clears below a confidence level the compliance officer sets.
- Re-screens nightly against list changes and on every party change, and emails a morning report.
- Exports audit records per party and per run.

**1.3 What v1 does not do.** No hosted offering, no lists beyond the CSL, no transaction or shipment screening, no ownership (50 percent rule) analysis, no phone UI beyond a responsive layout that happens to work. Section 13 lists these in full.

**1.4 Legal posture.** The CSL is public-domain U.S. government data. ITA's terms state that the CSL is one tool to assist users and is not a substitute for due diligence, and that users should make their own risk-based determinations. The application must display that language verbatim on first run and in the footer of every export, and must never describe a result as "cleared by the government" or similar.

## 2. Decisions already made

These are settled. Agents do not re-open them; anything not listed here is the agents' call, subject to section 14.

| Decision | Choice | Why |
| --- | --- | --- |
| Stack | Python 3.12, FastAPI, SQLite, React 18 + Vite + TypeScript | Laya (`pip install laya`), rapidfuzz, and the OpenSanctions matching libraries are Python; one language on the backend keeps the matching code where the libraries are |
| AI role | Optional adjudication layer on top of deterministic scoring | The tool must produce defensible results with no API key at all; Jev is still early access |
| Model adapters | Jev (hosted, TypeSafe AI) and Laya (local, Apache 2.0) behind one interface | Same request shape; Laya's `laya-serve` exposes a Jev-compatible endpoint |
| v1 connectors | CSV upload, QuickBooks Online, Dynamics 365 Business Central | Covers the small-business and mid-market ERPs where vendor masters live |
| Users | Multi-user with two roles, Admin and Reviewer | The audit trail must record who cleared what |
| License | MIT, with DCO sign-off required on every commit | Keeps the option to move to AGPL later without chasing contributors |
| Retention | Never delete screening records by default; optional admin-set retention period with a floor of 5 years | Matches the EAR recordkeeping horizon; the floor prevents an accidental purge |
| Deployment | One `docker compose up`; no external services except SMTP and optional Jev | The IT person runs it once |
| Name | `screening-tool` is the repo name and the working product name | Agents keep the display name in one config constant so a rename is a single change |
| Hosted version | Out of scope | Revisit if users ask |

## 3. Architecture overview

One Docker Compose service runs the FastAPI backend, the APScheduler worker, and the built React frontend, with SQLite on a mounted volume. There is no message broker, no separate database server, and no outbound call except to the party source APIs, data.trade.gov, the SMTP server, and TypeSafe's Jev endpoint when enabled.

&#91;embedded content: architecture · one container, one store, optional model\]

Party records and CSL entries both land in the SQLite store with content hashes. The engine reads both, produces hits, and the optional adjudicator refines each hit's confidence one pair at a time. The queue, the morning report, and the audit export all read from the same tables, so a disposition recorded in the UI is the same row the export prints.

**3.1 Backend layout.** `backend/app/` with packages `connectors/` (one module per source), `csl/` (fetch, parse, diff), `screening/` (normalize, candidates, score, adjudicate, bucket), `models/` (SQLAlchemy), `api/` (FastAPI routers), `scheduler/` (jobs), `reports/` (morning email, audit export), `cli.py` (Typer). Alembic for migrations from day one.

**3.2 Frontend layout.** `frontend/` Vite + React + TypeScript, TanStack Query for data, React Router, a small component library (shadcn/ui is acceptable). The backend serves the built assets from `/` and the API from `/api/v1/`.

**3.3 CLI.** `screening-tool` installs as a console script with `init`, `csl refresh`, `sync <source>`, `screen [--all | --party <id>]`, `export --party <id> | --run <id>`, and `report send`. Every scheduled job is a CLI command run by the scheduler, so an operator can run any step by hand.

## 4. Data model

Every record that feeds a screening decision carries a content hash, so the tool can prove later exactly what was compared with what. Timestamps are UTC. Primary keys are UUIDs.

| Table | Purpose | Key fields |
| --- | --- | --- |
| `users` | Login and audit identity | `email`, `display_name`, `role` (admin, reviewer), `password_hash` (argon2id), `active` |
| `sources` | A configured connector instance | `kind` (csv, qbo, bc), `label`, `credentials_enc`, `cursor` (last CDC timestamp or webhook state), `last_synced_at`, `record_count` |
| `parties` | One customer or vendor record | `source_id`, `external_id`, `party_type` (company, individual), `role` (customer, vendor, both), `name`, `alt_names` (JSON), `address_lines`, `city`, `region`, `postal_code`, `country_iso2`, `raw` (JSON as received), `record_hash`, `first_seen_at`, `last_seen_at`, `deleted_at` |
| `csl_versions` | One fetch of the bulk file | `fetched_at`, `file_sha256`, `entry_count`, `added`, `changed`, `removed`, `source_list_stamps` (JSON from the sources endpoint) |
| `csl_entries` | One CSL listing | `csl_id` (as published), `source_list` (SDN, EL, DPL, UVL, MEU, and so on), `name`, `alt_names` (JSON), `addresses` (JSON), `countries` (JSON), `programs`, `remarks`, `fr_notice_url`, `source_list_url`, `entry_hash`, `first_seen_version`, `last_seen_version`, `removed_version` |
| `screening_runs` | One execution of the engine | `trigger` (nightly, list\_change, party\_change, manual), `csl_version_id`, `started_at`, `finished_at`, `parties_screened`, `hits_new`, `hits_suppressed`, `auto_cleared`, `model_name`, `settings_snapshot` (JSON) |
| `hits` | One candidate pairing from one run | `run_id`, `party_id`, `party_hash`, `csl_entry_id`, `entry_hash`, `det_score` (0 to 100), `model_prob` (nullable), `final_confidence`, `bucket` (low, review, likely), `explanation` (JSON: per-field scores and matched tokens), `status` (open, dispositioned, suppressed) |
| `dispositions` | A human or system decision on a hit | `hit_id`, `kind` (false\_positive, confirmed\_match, escalated, auto\_cleared), `actor_user_id` (null for system), `note`, `slider_value_at_time` (nullable), `created_at` |
| `suppressions` | A remembered false positive | `party_id`, `csl_entry_id`, `party_hash`, `entry_hash`, `disposition_id`, `created_at`; unique on the four-way pair |
| `settings` | Key-value, encrypted where sensitive | thresholds, slider, SMTP, report schedule, model config, retention |
| `audit_log` | Append-only | `actor_user_id`, `action`, `target_table`, `target_id`, `before` (JSON), `after` (JSON), `created_at`; no UPDATE or DELETE permitted at the ORM layer |

**4.1 Hash rules.** `record_hash` covers normalized party type, name, alt names, address fields, and country, and nothing else, so a change to a phone number or role does not trigger a re-screen. `entry_hash` covers the CSL entry's normalized entity type, name, alt names, addresses, countries, and programs. Both are SHA-256 over a canonical JSON serialization with sorted keys. CSL text uses NFKC/casefold/punctuation/whitespace normalization, retaining Unicode and ASCII forms and legal suffixes; aliases, addresses, countries and programs are canonical unordered sets. Known countries normalize to ISO2 and unknown country text remains distinct. Original fields remain in snapshots. On 2026-10-13 the user explicitly approved including type in both hashes because type affects deterministic scoring and remembered false positives. This is hash rule version 2; semantic type aliases such as Entity/Company and Person/Individual share a canonical hash type. Version 1 historical hits, revisions, dispositions and raw-file hashes remain immutable. Existing caches require `screening-tool hashes upgrade` followed by `screening-tool screen --changed`; new runs record `hash_rule_version: 2` and actual party/entry hash manifests.

**4.2 Soft deletes only.** A party that disappears from the source gets `deleted_at` and is excluded from screening, never removed. Its hits, dispositions and audit rows stay.

## 5. Ingestion connectors

All three connectors implement one interface: `full_sync()` returns every active party; `incremental_sync(cursor)` returns parties changed since the cursor plus the new cursor; `normalize(raw) -> Party`. The engine never sees connector-specific shapes. Polling is the default for every connector because a self-hosted instance is usually behind a firewall; webhooks are an optional upgrade the admin turns on after exposing a public URL.

**5.1 CSV upload.** The universal fallback and the demo path. Accepts `.csv` and `.xlsx`. The upload screen shows the first 20 rows and lets the user map columns to `name`, `party_type`, `role`, `address_lines`, `city`, `region`, `postal_code`, `country`, `external_id`. Mappings are saved per source so a re-upload of the same export needs no re-mapping. A re-upload is a full sync: parties absent from the new file are soft-deleted. Country values are parsed with `pycountry` and rejected rows are listed, not silently dropped.

**5.2 QuickBooks Online.** OAuth 2.0. Each self-hoster registers their own Intuit developer app and pastes the client ID and secret into Settings; the tool never ships shared credentials. Pull the `Customer` and `Vendor` entities. Incremental sync uses the change data capture endpoint, `/v3/company/{realmId}/cdc?entities=customer,vendor&changedSince=<cursor>`, polled every 15 minutes by default. Webhooks, when enabled, arrive aggregated over five-minute windows and trigger an immediate CDC poll rather than being trusted as the payload. Deleted-in-QBO parties are inactive rather than removed, so map `Active: false` to `deleted_at`. Use the Intuit sandbox for tests.

**5.3 Dynamics 365 Business Central.** OAuth 2.0 via a Microsoft Entra app registration the self-hoster creates. Pull `/api/v2.0/companies({id})/customers` and `/vendors`. Incremental sync filters on `lastModifiedDateTime gt <cursor>`. Webhooks, when enabled, register through `POST /api/v2.0/subscriptions` with the validation-token handshake, one subscription per resource; Business Central retries unreachable subscribers for up to 36 hours, and a notification triggers an incremental poll. Multi-company tenants: one source per company.

**5.4 Sync behavior shared by all connectors.** Compute `record_hash` on every pulled record. Unchanged hash means no write and no re-screen. Changed hash means update the party, write `audit_log`, and enqueue a `party_change` screening of that party alone. New record means insert and screen. Every sync writes `last_synced_at` and `record_count` on the source so the Sources page can show health at a glance.

## 6. CSL data pipeline

The tool screens against a local copy of the official bulk file, never against the CSL search API, because the API's fuzzy search is capped at two character edits and would be the matcher's ceiling. The API key is still used, for the sources endpoint that reports when each underlying list last changed.

**6.1 Fetch.** Nightly at 05:00 server-local by default, and on demand via `csl refresh`. Download `https://data.trade.gov/downloadable_consolidated_screening_list/v1/consolidated.json` (agents verify this URL at build time; see section 14). Compute the file SHA-256 before parsing. If it equals the previous version's, record the fetch and stop.

**6.2 Parse and diff.** Parse every entry into `csl_entries` fields, keeping the raw entry in `remarks`-adjacent JSON for anything the schema does not model. Compute `entry_hash` per entry. Diff against the current version by `csl_id`: new ids are `added`, ids whose hash changed are `changed`, ids missing from the new file are `removed` (set `removed_version`, keep the row). Write a `csl_versions` row with the counts.

**6.3 Trigger re-screen.** Any `added` or `changed` entry enqueues a `list_change` screening run scoped to those entries only: every active party is screened against the delta, not the whole list. A `removed` entry closes any open hits against it with a system disposition of kind `auto_cleared` and note `entry removed from list in version <id>`, and the party file shows that history.

**6.4 Sources endpoint.** Once per fetch, call the CSL sources endpoint with the ITA API key and store the per-list last-updated stamps in `csl_versions.source_list_stamps`. The Settings page shows them so a compliance officer can see, for example, that the Entity List data is current as of a given date. The ITA key is free from developer.trade.gov; the tool works without it, minus this display.

**6.5 Provenance on every hit.** Each `hits` row stores `entry_hash` and the run stores `csl_version_id`, so the export can state the exact list version a decision was made against. The UI's "View source" link on a hit opens `fr_notice_url` when present, else `source_list_url`.

## 7. Matching and adjudication

Deterministic scoring decides which pairs are candidates; the optional model decides how confident to be about each one; thresholds decide where each candidate goes. The model never generates candidates and never sees more than one pair at a time.

&#91;embedded content: screening pipeline · 4 decisions, 1 optional model call\]

A pair below `candidate_min` is never stored. A remembered false positive is stored but never queued. The model, when present, only adjusts confidence; the bucket thresholds and the slider decide where the hit goes, and the slider can only act on the review bucket.

**7.1 Normalization.** Applied identically to party names and CSL names, with the original kept alongside. Unicode NFKC, casefold, transliterate to ASCII with `unidecode` (keep both forms and match on either), strip punctuation, collapse whitespace, drop a configurable list of legal-form suffixes (Inc, LLC, Ltd, Limited, Corp, Co, GmbH, AG, SA, SARL, BV, Pty, LLP, PLC, OOO, and so on), and tokenize. Individuals: generate both "First Last" and "Last, First" token orders. Country codes to ISO 3166-1 alpha-2 via `pycountry`.

**7.2 Candidate generation.** Build an in-memory index over every CSL entry's name and alt names at the start of a run. Block by shared normalized token of length three or more (excluding a stopword list: the, of, and, company, international, trading, group, general, global, services) and by Double Metaphone of the first token. Within each block, score with rapidfuzz. If a run's blocking yields fewer than three candidates for a party, fall back to `rapidfuzz.process.extract` over the full index with a cutoff of `candidate_min`. Default `candidate_min` is 50; pairs below it are never recorded.

**7.3 Deterministic score.** `det_score` is 0 to 100 and every component is written to `hits.explanation` so the UI can show why.

| Component | Rule | Default weight |
| --- | --- | --- |
| Name | max over (party name + alt names) × (entry name + alt names) of 0.5 × token\_set\_ratio + 0.25 × token\_sort\_ratio + 0.25 × partial\_ratio | base score |
| Country | both known and party country in entry countries: +5; both known and absent: −15; either unknown: 0 | additive |
| City | normalized city match: +5 | additive |
| Address tokens | overlap of address-line tokens, up to +5 | additive |
| Entity type | entry marked individual while party is a company, or the reverse: −10 | additive |
| Short names | party name of one token and under five characters: −10 unless an alt name also matches | additive |

Clamp to 0..100. Weights live in Settings under an "Advanced" disclosure and the agents document the defaults in the README.

**7.4 Suppression check.** Before writing a hit, look up `suppressions` by (`party_id`, `csl_entry_id`, `party_hash`, `entry_hash`). An exact four-way match writes the hit with `status: suppressed` and no queue entry, so the party file still shows the pairing was seen. A match on ids but not hashes means something changed: write an open hit and link the prior disposition in `explanation.prior_disposition_id` so the reviewer sees what was decided before and what moved.

**7.5 Model adjudication (optional).** For every open hit with `det_score >= review_threshold − 10`, build one Noul request: context is the party record fields and the CSL entry fields, both normalized and original, plus the component breakdown; the claim is "These two records refer to the same party." The model returns a probability `p`. `final_confidence = round(w_det × det_score + w_model × 100p)` with defaults `w_det = 0.4`, `w_model = 0.6`. Without a model, `final_confidence = det_score`. Safety rail: a hit with `det_score >= likely_threshold` is bucketed `likely` regardless of the model's answer; the model may raise confidence but cannot demote a strong deterministic match. Both numbers are stored and both are shown.

**7.6 Model adapters.** One interface, `Adjudicator.noul(context: str, claim: str) -> float`, two implementations behind it. Jev: TypeSafe AI's hosted endpoint, API key in Settings, request budget around 32,000 tokens, billed per input token with output free; the adapter retries with backoff and falls back to deterministic-only on failure, logging the fallback on the run. Laya: `pip install laya` and `laya-serve`, which exposes a Jev-compatible `/v1/systemone` endpoint; the adapter points at a local URL. Laya's context runs 512 to 1,024 tokens depending on checkpoint, so the adapter truncates context in a fixed order (drop remarks, then address lines, then alt names beyond the best-matching one). Agents must read TypeSafe's and Convai's current docs at build time for the exact request shape; the shape in this document is intent, not a schema.

**7.7 Buckets and thresholds.** Defaults: `review_threshold` 65, `likely_threshold` 90. `final_confidence` below `review_threshold` is bucket `low`: recorded on the party file, not queued, no disposition required. Between the two is `review`: queued. At or above `likely_threshold` is `likely`: queued at the top, flagged, and included in an immediate alert email if alerts are on.

**7.8 Auto-clear slider.** Off by default. Enabling it requires an Admin to type "I understand" in a confirmation dialog that states the tool will record system dispositions without human review below the chosen value; the acknowledgment and the user are written to `audit_log`. The slider value is an integer between `review_threshold` and `likely_threshold`, never above. When on, a hit in bucket `review` with `final_confidence` below the slider value gets a disposition of kind `auto_cleared` with `actor_user_id` null, `slider_value_at_time` set, and a note recording the run id, the model name, and both scores. Auto-cleared hits never appear in the queue, always appear in the party file, and are counted in the morning report. Changing the slider does not retroactively alter past dispositions.

**7.9 Training data export.** `screening-tool export training-pairs` writes every dispositioned hit as a labeled pair (party fields, entry fields, label false\_positive or confirmed\_match) so a Laya fine-tune can be run from the compliance team's own decisions. Out of scope to run the fine-tune in v1.

## 8. User interface

The review queue is the product; the other screens exist to feed it and to prove what it did. Desktop-first, responsive enough to read on a phone, no phone-specific flows in v1. Keyboard shortcuts on the queue are mandatory, not a nicety.

**8.1 Home.** On a normal day this screen is empty and calm: "412 parties screened against CSL version 2026-10-13 (fetched 05:00), 0 open items." Below it, four small numbers: open items, likely matches, auto-cleared since last report, parties not screened in over 24 hours. Each number links to a filtered view. No charts.

**8.2 Review queue.** A list on the left sorted `likely` first, then `review` by confidence descending, then by age. The selected item fills the right pane:

- Header: the CSL source list as a badge, `final_confidence` as a plain number with `det_score` and `model_prob` beneath it in smaller type, bucket, and age.
- Comparison: the party record on the left, the CSL entry on the right, field by field (name, alt names, address, city, country, type). Tokens that matched are highlighted in both columns; the explanation JSON drives the highlighting.
- Why: three one-line reasons generated from `explanation` (for example "name 94, country matches, city unknown").
- Prior history: if `explanation.prior_disposition_id` is set, a yellow strip says what was decided before, by whom, and what changed since.
- Actions: three buttons with shortcuts. `F` Clear as false positive, `M` Confirmed match, `E` Escalate. Clear needs no note; the other two require a note before the button activates. `J`/`K` move through the list, `V` opens the source URL in a new tab. After a disposition the next item loads automatically.

**8.3 Parties.** A table of every party: name, role, source, country, last screened, status (clear, open hit, confirmed match, suppressed pairings count). Full-text filter, column filters, CSV export of the current view. Clicking a row opens the party file.

**8.4 Party file.** The page an auditor sees. Top: the current record and its source. Then a reverse-chronological list of every screening event for this party: run, CSL version, candidate entry, both scores, bucket, disposition, actor, note. Suppressed pairings are collapsed under "previously cleared (n)". A button exports this page as PDF and as CSV.

**8.5 Sources.** One card per configured source: kind, label, record count, last sync, next sync, health. "Add source" offers CSV upload, Connect QuickBooks Online, Connect Business Central. The CSV flow is the column-mapping screen from section 5.1. The OAuth flows ask for the self-hoster's app credentials first, then redirect.

**8.6 Audit and export.** A list of runs: trigger, started, duration, parties screened, new hits, suppressed, auto-cleared, model used, CSL version. Per run: export CSV and PDF. A global "full audit export" produces a zip of every party file plus the run list.

**8.7 Settings (Admin only).**

- General: product display name, timezone, retention period (floor 5 years).
- Thresholds: `candidate_min`, `review_threshold`, `likely_threshold`, score weights under an Advanced disclosure.
- Auto-clear: the on/off switch with the acknowledgment dialog, then the slider bounded by the two thresholds, with live text "items scoring below N will be cleared without review."
- Model: none / Jev / Laya. Jev asks for an API key and shows the privacy statement from section 10.3 before saving. Laya asks for the local endpoint URL and shows a connection test.
- CSL: ITA API key (optional), fetch time, per-source-list toggles, last fetched, per-list stamps.
- Notifications: SMTP host, port, credentials, from address; morning report recipients and send time; immediate alerts for `likely` on/off.
- Users: invite by email, role, deactivate.

**8.8 First-run.** On an empty database the app shows a setup wizard: create the first Admin, show the ITA disclaimer and require acknowledgment, offer to fetch the CSL now, offer to add a source. Nothing else is reachable until the Admin exists.

## 9. Scheduling and continuous monitoring

Continuous monitoring has two triggers, and the tool must handle both: the list changed, or the party changed. A full re-screen of every party against the whole list happens only on first load and on manual request.

| Job | Default schedule | What it does |
| --- | --- | --- |
| `csl refresh` | daily 05:00 server-local | Fetch, hash, diff; enqueue `list_change` run if the delta is non-empty |
| `sync <source>` | every 15 minutes per API source | Incremental pull; enqueue `party_change` runs for changed or new parties |
| `screen --changed` | immediately after any sync or refresh | Runs the enqueued scoped screenings |
| `screen --all` | manual, and once at first load | Full book against full list |
| `report send` | daily at the configured time, default 07:00 | Morning report email |
| `alert send` | on demand when a `likely` hit is written, if enabled | Immediate email for likely matches |
| `retention apply` | weekly | Purges rows older than the configured period, if one is set; never below 5 years |

**9.1 Job runner.** APScheduler inside the API process with a SQLite job store, so a restart resumes schedules. Jobs are idempotent: a second `csl refresh` in the same hour sees the same hash and stops. Overlapping runs are prevented with a lock row; a job that finds the lock held logs and exits.

**9.2 Webhooks.** Optional. An Admin enables them per source after setting a public base URL in Settings. Inbound notifications are validated (Intuit signature; Business Central validation token), then treated only as a signal to run an incremental poll now. Payload contents are never trusted as data.

**9.3 Morning report.** One email per recipient at the configured time, plain text with an HTML alternative, subject `screening-tool: <n> open items, <m> likely matches`. Body, in order: the as-of line (CSL version and fetch time), open items with a link to the queue, likely matches listed by party name and source list, new hits since the last report, auto-clears since the last report (count, with a link to the filtered party list), list changes (added, changed, removed counts), sync health (any source not synced in over 24 hours), and the disclaimer footer. If there is nothing to report the email still sends, and says so in one line. SMTP failures are logged and shown as a banner in the UI.

**9.4 Immediate alerts.** Off by default. When on, a `likely` hit sends an email within the same run that wrote it, one email per run listing all likely hits from that run.

## 10. Security, privacy and compliance notes

The tool holds a company's complete customer and vendor list, which is commercially sensitive on its own, so the defaults assume a hostile network and a cautious compliance officer.

**10.1 Secrets at rest.** Connector credentials, SMTP passwords, and model API keys are encrypted with a key read from the `SCREENING_TOOL_SECRET_KEY` environment variable (Fernet, via `cryptography`). The container refuses to start without it and the README shows how to generate one. The SQLite file itself is not encrypted; the README says to put the volume on an encrypted disk.

**10.2 Authentication.** Email and password, argon2id hashes, server-side sessions in SQLite with HttpOnly, Secure, SameSite=Lax cookies. Rate-limit login attempts. No OAuth login for users in v1. The README documents putting a reverse proxy with TLS in front; the container serves plain HTTP on an internal port.

**10.3 Data leaving the host.** With no model configured, the only outbound calls are to the configured party sources, data.trade.gov, api.trade.gov, and SMTP. Enabling Jev sends party names, addresses, and the candidate CSL entry to TypeSafe AI's API for every adjudicated hit. The Model settings page must show this sentence before the key can be saved: "Enabling Jev sends your customer and vendor names and addresses to TypeSafe AI for scoring. Laya keeps all data on this server." Laya calls go only to the local URL the Admin configured.

**10.4 No telemetry.** The application sends nothing to the project maintainers. Version checks are opt-in and off by default.

**10.5 Audit integrity.** `audit_log` is insert-only at the ORM layer and the SQLite trigger layer. Every disposition, setting change, user change, source change, and slider change writes a row. Exports include the audit rows relevant to the exported parties.

**10.6 Disclaimer and language.** The ITA statement from section 1.4 appears in the first-run wizard, the footer of every export, and the Settings page. The UI never uses the words "approved", "cleared by", or "compliant" in reference to a party; it uses "no match found", "false positive", "confirmed match", and "escalated".

**10.7 Dependencies.** Pin every dependency, run `pip-audit` and `npm audit` in CI, and keep the production image free of build tooling (multi-stage Dockerfile).

## 11. Repository scaffold and build order

The first session creates the repository and the skeleton; each later milestone is a working increment that can be run end to end before the next starts. No milestone begins until the previous one's gate passes.

**11.1 Repository layout.**

```
screening-tool/
  LICENSE                 MIT
  README.md               install, configure, run, screenshots
  CONTRIBUTING.md         DCO sign-off required (git commit -s)
  CLAUDE.md               agent instructions, see 11.2
  docker-compose.yml      one service, one volume
  Dockerfile              multi-stage: node build, python runtime
  .github/workflows/      ci.yml (lint, type-check, tests, audits), dco.yml
  backend/
    pyproject.toml        uv or pip-tools, pinned
    app/                  see section 3.1
    alembic/
    tests/
  frontend/
    package.json
    src/
  fixtures/               synthetic parties and a frozen CSL sample, see section 12
  docs/
    design-v1.md          this document, exported
```

**11.2 CLAUDE.md contents.** Point to `docs/design-v1.md` as the source of truth. State the four settled decisions from section 2 in one line each. Require: run the test suite before claiming a milestone is done; never fabricate an API endpoint, verify against the vendor docs and record the URL consulted in the commit message; never weaken an audit or privacy rule to make a test pass; when the spec is silent, choose the option that writes more to `audit_log`, not less. After each milestone, write a short `docs/milestones/<n>.md` saying what was built, what was verified, and what could go wrong.

**11.3 Build order.**

1. **Scaffold.** Repo, license, DCO workflow, Dockerfile, compose file, empty FastAPI app that serves a built React page, Alembic baseline with every table from section 4. Gate: `docker compose up` serves the first-run wizard and creates an Admin.
2. **CSL pipeline.** Fetch, hash, parse, diff, versions, sources endpoint. CLI `csl refresh`. Gate: two consecutive refreshes produce one version; a modified fixture file produces correct added/changed/removed counts.
3. **CSV connector and deterministic screening.** Upload with column mapping, normalization, candidate generation, scoring, buckets, hits, suppressions. CLI `screen --all`. Gate: the fixture dataset from section 12 produces the expected hits with the expected buckets, and a second run produces zero new open hits.
4. **Review queue, party file, dispositions, users.** The UI from section 8, auth, roles, audit log. Gate: a Reviewer can work the fixture queue end to end with the keyboard, and every action appears in `audit_log` and the party file.
5. **Scheduling, false-positive memory, auto-clear, morning report, audit export.** Gate: a nightly cycle on the fixture data sends a report, suppressed pairs do not reappear, a changed party re-opens its pair with prior history shown, and the slider's acknowledgment flow writes to `audit_log`.
6. **QuickBooks Online connector.** Full and incremental sync against the Intuit sandbox, optional webhook. Gate: a sandbox vendor edit shows up as a `party_change` run within one poll interval.
7. **Business Central connector.** Same gate against a BC sandbox tenant.
8. **Model adapters.** Jev and Laya behind the interface, blend, safety rail, privacy statement, training-pairs export. Gate: with Laya running locally, fixture hits carry `model_prob` and the rail holds; with the adapter unreachable, screening completes deterministic-only and the run records the fallback.
9. **Hardening and release.** `pip-audit`, `npm audit`, rate limiting, README with screenshots, `v0.1.0` tag.

**11.4 Definition of done for v1.** All nine gates pass in CI against fixtures, plus manual gates 6 and 7 against live sandboxes, and a fresh machine can go from `git clone` to a screened CSV in under fifteen minutes following only the README.

## 12. Testing and fixtures

The fixture dataset is the contract between milestones: every gate in section 11 runs against it, so the agents build it in milestone 3 and never silently change expected outcomes afterward.

**12.1 Frozen CSL sample.** `fixtures/csl_sample_v1.json`: 300 real entries copied from the public bulk file, chosen to cover every source list, individuals and companies, entries with multiple alt names, entries with addresses and entries without, and at least ten entries whose names are common words or common surnames. A second file, `csl_sample_v2.json`, is the same with 5 entries added, 5 changed (one alt name each), and 5 removed, with the expected diff counts recorded in `fixtures/expected/csl_diff.json`.

**12.2 Synthetic parties.** `fixtures/parties.csv`: 2,000 generated companies and individuals with plausible addresses in 30 countries, plus 60 planted cases with expected outcomes in `fixtures/expected/hits.json`:

- 10 exact-name matches to sample entries with matching country (expected `likely`)
- 10 transliteration or diacritic variants (expected `review` or `likely`)
- 10 token-order variants, including "Last, First" individuals (expected `review` or above)
- 10 legal-suffix-only differences, such as Ltd versus Limited (expected `likely`)
- 10 common-name false positives with a different country (expected `low` or `review`, never `likely`)
- 5 address-only overlaps with unrelated names (expected below `candidate_min`)
- 5 same-name parties whose entry is `removed` in `csl_sample_v2` (expected auto-close on refresh)

Expected outcomes are bucket ranges, not exact scores, so weight tuning does not break the suite as long as the behavior holds.

**12.3 Test layers.** Unit tests for normalization and each score component with table-driven cases. Integration tests for the CSL diff, the suppression lookup, the slider rules, and the morning report body. API tests for every router. A Playwright test that works the fixture queue with the keyboard and checks the party file afterward. Connector tests run against recorded HTTP fixtures (`vcrpy`) in CI and against live sandboxes manually.

**12.4 What the agents report after each milestone.** The milestone note in `docs/milestones/` lists the tests added, the fixture outcomes, any deviation from this document and why, and the failure modes the agent can foresee, written for a non-engineer.

## 13. Out of scope for v1

Each of these is a deliberate exclusion; an agent that finds itself building one stops and asks.

- A hosted or multi-tenant version.
- Lists beyond the CSL: OpenSanctions, EU, UK, UN, PEP, adverse media. OpenSanctions in particular requires a commercial data license, which conflicts with an MIT tool.
- Ownership and control analysis (OFAC 50 percent rule, BIS affiliates rule), beneficial-ownership lookups.
- Transaction, order, or shipment screening; the unit of screening is the party record.
- Connectors beyond CSV, QuickBooks Online, and Business Central: NetSuite, SAP, Xero, Salesforce, HubSpot, Coupa, Ariba. The connector interface in section 5 is designed so each is a later module.
- Unified-API middleware (Apideck, Merge, Codat, Rutter).
- Running a Laya fine-tune inside the tool; v1 only exports the training pairs.
- Single sign-on, SCIM, LDAP.
- Phone-specific UI, native apps, push notifications.
- Screening of employees, visitors, or research collaborators (the HR and university use case).
- Country or embargo screening of destinations.

## 14. Risks and open items the agents must verify at build time

This document was written from public documentation as of 13 October 2026 without touching any of the vendor APIs. Each item below is something the first agent to reach that part of the build confirms against the live source and records in the milestone note.

| Item | What to verify | If it differs |
| --- | --- | --- |
| CSL bulk URL | `https://data.trade.gov/downloadable_consolidated_screening_list/v1/consolidated.json` is current and its JSON field names | Update section 6 and the parser; the hash-and-diff design does not change |
| CSL sources endpoint | Path, auth header, and response shape on developer.trade.gov | Adjust the adapter; the feature is cosmetic |
| Jev API | Endpoint, request schema for the Noul primitive, token budget, early-access gating, pricing | If keys are not obtainable, ship Laya-only and leave the Jev adapter behind a feature flag |
| Laya | Current checkpoint names, context limits, `laya-serve` endpoint path and request shape | Adjust the adapter; truncation order in section 7.6 stands |
| Laya zero-shot quality | Convai's own card says Laya is a base for specializing, not a zero-shot decision engine; benchmark scores against the fixture set | If `model_prob` adds nothing on fixtures, default the blend to `w_model = 0` and document the fine-tune path |
| QuickBooks developer app | Whether Intuit's app review or production-key requirements block a self-hoster from using their own app against their own company | If blocked, document the sandbox-only path and raise it as a v1 limitation |
| QuickBooks CDC window | Maximum look-back the CDC endpoint accepts | Set the poll interval well inside it |
| Business Central auth | Entra app registration steps and the exact API permission names | Document them in the README step by step |
| Business Central webhooks | Subscription expiry and renewal interval | Add a renewal job |
| Name matching recall | rapidfuzz plus blocking misses transliteration families that the OpenSanctions `nomenklatura` matcher would catch | Evaluate adding `rigour` or `nomenklatura` name normalization (MIT-licensed code; the license concern in section 13 is about the data, not the library) |
| Performance | 20,000 parties against 25,000 entries with blocking should finish a full run in minutes on a small VM | If not, add a persistent token index in SQLite |
| Retention floor | Confirm the five-year figure against the EAR recordkeeping rule before writing it into the UI text | Adjust the floor and the README citation; the mechanism does not change |
| Jev early-access status | The hosted model may still be invite-only at build time | Covered by the AI-optional decision; no gate depends on Jev |

**14.1 Risks to the product, not the build.** False negatives are the real risk in screening and no fuzzy matcher eliminates them; the README must say plainly that this tool is one control in a compliance program, not the program. Jev's privacy trade-off may deter exactly the compliance officers most likely to adopt; Laya exists so there is a local path. Each self-hoster registering their own Intuit and Microsoft apps is real friction; it is also the only honest way to ship connectors in an open-source tool without the maintainer holding everyone's credentials.
