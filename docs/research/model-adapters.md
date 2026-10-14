# Optional adjudicator contracts — verified 2026-10-14

Read-only M8 preparation using the TypeSafe skill and live primary sources. No account credentials, environment files, customer records, paid inference, runtime changes, or adapter implementation were used. The completed [local Laya benchmark](laya-validation.md) remains authoritative for measured screening quality; this note updates transport contracts and implementation decisions.

## Hosted Jev

[HTTP reference](https://docs.typesafe.ai/api.md) and [Noul](https://docs.typesafe.ai/primitives/noul.md): `POST https://api.typesafe.ai/v1/systemone`, JSON, `Authorization: Bearer <API_KEY>`. Use this named-map envelope, with the populated screening fields replacing the empty objects:

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

Read `answers.same_party.type == "noul"` and `answers.same_party.noul`; preserve returned `model` and `usage.input_tokens`/`output_tokens`. Reject missing answers, booleans masquerading as numbers, nonfinite values and values outside [0,1]. Noul has no separate confidence field. Optional criteria are descriptions under `true` and `false`. Do not invent a question list, `claim` wire field, `probability` response field, or generation/chat endpoint. The older `/noul.md` and `/migrating-to-v1.md` returned 404; the live index points to `/primitives/noul.md`, and no current migration guide was discovered.

[Models](https://docs.typesafe.ai/models): pinned `jev-1.13.0`; `jev-latest` and `jev-preview` presently resolve to it. Returned model identity must be recorded even when using an alias. Published limits are 64k tokens across state/all questions and 32k across state/the longest question. Price is $0.042 per million input tokens, with free output tokens. Published 80 requests/s and 100k tokens/s are explicitly mutable. One pair/one Noul is the product contract. No tokenizer or token-count endpoint was documented in the pages inspected: do not describe a character count or the Laya tokenizer as an exact Jev budget proof. Record the chosen estimate separately from actual response usage; an oversized/invalid request must fall back honestly.

[Quickstart](https://docs.typesafe.ai/introduction/quickstart) directs users to [console keys](https://console.typesafe.ai/keys), which redirects an unauthenticated visitor to login. This does not verify enrollment, account model entitlement, credits, billing or inference access. Authenticated `GET /v1/models` lists account-accessible names; no account request was made. Keep Jev optional and do not claim either guaranteed availability or confirmed invite-only gating. [Legal documentation](https://docs.typesafe.ai/legal) states no training on customer requests and distinguishes enterprise zero-retention arrangements; do not promise universal zero retention.

## Python SDK verification and retry ownership

Current public [typesafe-sdk metadata](https://pypi.org/pypi/typesafe-sdk/0.7.4/json) reports 0.7.4, Python >=3.10. The public wheel was inspected in memory without installation; SHA-256 `61cf3f2fe9da20b68c3d3fe2fb76bbb2f35f8c4a9a65a6b81c43a535d2a53210`. [Client reference](https://docs.typesafe.ai/sdk/python/api/clients/sync) accepts explicit API key/model/base URL, `system_one(state=..., questions=...)`, and typed response models. Its logging redacts secret headers but **not request/response bodies**; disable body logging in this application.

[RetryPolicy](https://docs.typesafe.ai/sdk/python/api/retries) defaults: two retries after the initial request; 0.5-second initial backoff, 5-second ceiling, 0.25 jitter; HTTP 408/429/500–599, including 529; connection/timeouts retry; honor `Retry-After` and `retry-after-ms`; total call retry budget 30 seconds. [Constants](https://docs.typesafe.ai/sdk/python/api/constants) set each HTTP operation's default timeout to 10 seconds. HTTP docs explicitly identify 401 invalid key, 422 validation, 429 rate limit and 529 overload. Neither 401 nor 422 should be retried. Set one bounded application retry owner; if using the SDK, explicitly configure its budget or disable retries before implementing another retry loop. Existing application `httpx` can implement the verified wire directly without adding SDK dependency. This is a proposed integration choice, not implemented code.

## Local Laya

[Current package metadata](https://pypi.org/pypi/laya/json) reports 0.4.7; the actual 121-request MPS benchmark used 0.4.6. Preserve that distinction. [Publisher card](https://huggingface.co/convaiinnovations/laya/raw/main/README.md) retains English 512-token and multilingual 1,024-token defaults; extending multilingual to 8,192 is an explicit override, not a new default. The base checkpoint's measured screening results continue to justify `w_model=0`, `w_det=1`. No new checkpoint calibration was performed here.

[Serving source](https://github.com/NandhaKishorM/laya/blob/432312b36714d8889eba405507b37eda25257d9c/laya/serve.py), inspected at commit `432312b36714d8889eba405507b37eda25257d9c`: native `POST /v1/systemone` accepts the same state/questions map, plus explicit `model`, `max_len`, `head_max_len`. It requires Bearer auth only when `LAYA_API_KEY` is configured. `LAYA_JEV_STRICT=1` projects the response to model/answers/usage and removes Laya-only routing/action/confidence additions. Health without a configured key returns detail; an unauthenticated request when a key is set gets liveness only. HTTP failures include malformed JSON/envelope 400, invalid question/budget 422, size limits 413, invalid auth 401, busy 503, inference failure 500. A server ceiling of 8,192 does not establish that any checkpoint was calibrated at that context length.

[Router registry](https://github.com/NandhaKishorM/laya/blob/432312b36714d8889eba405507b37eda25257d9c/laya/router.py) names `english`, `multilingual`, `typed-decisions`. Explicit `model:"multilingual"` is required for our validated path; a Jev-style model name or the root bundle ID can mean automatic routing, not a pinned multilingual checkpoint. [Question validator](https://github.com/NandhaKishorM/laya/blob/432312b36714d8889eba405507b37eda25257d9c/laya/agent.py) requires instructions and the current Noul schema; unrelated criteria keys are rejected. Supporting some older Choice/Score forms does not establish an alternate Noul transport.

For the pinned tokenizer/checkpoint/question, the prior benchmark measured 35 overhead tokens and 989 state tokens within 1,024. Recompute with publisher sequence construction for any changed instruction, tokenizer, checkpoint or criteria. Apply the established ordered removal: remarks, street address lines, then aliases beyond the best matching alternate. Preserve primary names and other identity evidence; if essential state still does not fit, fall back rather than silently slice it. Record removed fields and budget provenance. Strict response mode removes truncation diagnostics, so client budget proof or separately verified extended diagnostics remain necessary.

## Stable application boundary proposed for M8

Keep `Adjudicator.noul(context: str, claim: str) -> float` as the compatibility primitive. Internally use a per-call evaluation result for probability, provider, requested/returned model, token usage, trimming, attempts and sanitized failure code; avoid shared mutable last-response metadata. A runner bridges this to the engine's existing immutable `Adjudication` hook. Retain deterministic candidate generation, cutoff `review_threshold - 10`, remembered human decisions, and the deterministic likely-match rail. Missing/failed inference gives `model_prob=null` and deterministic confidence with an audited fallback, never probability zero. Default blend remains deterministic until task-specific calibration supports a positive weight.

Before a Jev key can be saved, show the exact design sentence: “Enabling Jev sends your customer and vendor names and addresses to TypeSafe AI for scoring. Laya keeps all data on this server.” Enforce the local endpoint deployment assumption; a remote arbitrary URL cannot substantiate that Laya statement. Keep secrets server-side and redact settings, requests and logs.

Training-pair export must use original immutable hit snapshots and actual human actors with `false_positive` or `confirmed_match` labels. Exclude escalations, slider/system decisions, removals, zero-hit observations and suppressed repeats without a new human label. Retain decision and hash provenance, including legitimate fanout groups, rather than inventing a new decision or reconstructing snapshots from current records. Purged evidence requires an explicit unavailable/omitted result. Fine-tuning remains out of v1 scope.

## Implemented M8 boundary

The application uses direct pinned HTTP with one retry owner, an overall 30-second deadline, identity encoding and bounded raw response reads. It validates write-only printable-ASCII keys and enforces exact hosted acknowledgment, fixed Jev destination, local-only Laya URLs, no proxies or redirects, and per-pair policy/authority checks outside the writer. The offline tokenizer and original licensing notices are packaged; actual local usage must agree with the prepared sequence. Returned failures clear all model result fields and preserve deterministic scoring. One encrypted model configuration owns weights and generation; historical PDF/CSV/ZIP and human training pairs use original evidence. The [local Laya report](laya-validation.md) records actual same-host inference; synthetic provider checks do not establish live hosted access.
