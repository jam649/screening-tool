# Local Laya validation — 2026-10-13

## Decision

Actual local inference passed its transport and token-budget checks. The tested base multilingual checkpoint **does not justify a positive model blend weight** for this screening task. Use `w_model = 0`, `w_det = 1` by default; preserve the raw optional probability and the deterministic likely-match rail. This conclusion comes from measured fixture comparisons, not the publisher's benchmark card. It applies to this checkpoint, exact claim, and state format; it does not establish that every Laya checkpoint or specialized model is unsuitable.

This research fulfills the local-inference and zero-shot-quality investigation in design risk 14. It does not itself complete milestone 8: the app adapter, fallback audit, training export, privacy disclosure, and in-app gate remain separate work. No backend, frontend, fixture, or vendor contract was modified by this benchmark.

## Verified runtime and sources

The available `typesafe-ai` skill was applied. Read the [live documentation index](https://docs.typesafe.ai/llms.txt), the existing verified [HTTP contract](https://docs.typesafe.ai/api), and [Noul contract](https://docs.typesafe.ai/primitives/noul). The Markdown HTTP/Noul pages were unavailable through the web tool on this run; the project already records the successfully verified native shape in `vendor-contracts.md`. Current publisher metadata was independently fetched using system curl with TLS verification enabled:

- [PyPI package metadata](https://pypi.org/pypi/laya/json): latest observed runtime `0.4.6`; installed `laya[serve]==0.4.6` into an isolated Python **3.12.4** environment.
- [Publisher model card](https://huggingface.co/convaiinnovations/laya/raw/main/README.md): Apache 2.0; English context 512; multilingual default 1,024; optional multilingual 8,192 requires an explicit override. This run stayed at 1,024.
- [Publisher serving source](https://github.com/NandhaKishorM/laya/blob/main/laya/serve.py), checked against the installed pinned package: `LAYA_HOST`, `LAYA_PORT`, `LAYA_MODELS`, `LAYA_PRELOAD`, `LAYA_JEV_STRICT`; endpoint `POST /v1/systemone`. The loaded checkpoint is **`convaiinnovations/laya`, subfolder `multilingual`**, encoder `jhu-clsp/mmBERT-base`.

Actual downloaded revision: `7b928d828b7b0e022f929d9bd2e44165aa270148`. The checkpoint's `model.safetensors` SHA-256 is `9d628fd971b700382ac6f65920a86f149777b2e748e0c955fb3b19695aa8f204`. The strict response model ID is generic `laya-rl-agent`; pin and record the health endpoint's revision as well as that response ID.

Installed dependencies include Torch `2.14.1`, Transformers `5.19.0`, FastAPI `0.143.0`, Uvicorn `0.54.0`, Hugging Face Hub `2.2.0`, Tokenizers `0.23.3`, and Safetensors `0.8.0`. The full installed dependency snapshot is ignored `.workspace/laya-validation/requirements.lock.txt`, SHA-256 `084bec68899b8042082aa2e04acdc3d3b9075d81111f48ccc99cc171b47ff7bd`.

The Mac reports ARM64, 8 CPU cores, and 16 GiB unified memory. `/health` reported actual device `mps`, loaded model `multilingual`, and zero CPU fallbacks before and after inference. A post-run process sample showed RSS 353,600 KiB; this is a point-in-time CPU process sample, **not peak unified GPU memory**. Checkpoint/cache files totaled 678,275,784 bytes, about 647 MiB on disk; dependency trees and caches remain in the repository's ignored `.workspace`, outside Desktop/iCloud staging.

No credential was obtained. Only public weights, frozen public CSL rows, and synthetic parties were used. `HF_HUB_DISABLE_TELEMETRY=1` and `DO_NOT_TRACK=1` were set for the server and benchmark. Hugging Face's unauthenticated-download notice was informational; the download succeeded. The sandbox initially could not resolve public hosts; authorized network escalation allowed normal TLS-verified downloads. No certificate verification was disabled.

## Dataset and target labels

Read all **300** frozen public CSL entries with the existing app parser, maintaining source/published-ID composite identities, and read the existing synthetic CSV without changing it. Evaluate the 60 target pairs named in `fixtures/expected/hits.json`, then add 60 unrelated controls pairing `background-0000` through `background-0059` with public sample entry indices `(i * 7 + 29) % 300` in file order.

| Group | Count | Same-identity target |
| --- | ---: | --- |
| Exact name and matching country | 10 | True |
| Diacritic/transliteration variants | 10 | True |
| Token-order variants | 10 | True |
| Legal-suffix variants | 10 | True |
| Removal targets, compared with v1 before removal | 5 | True |
| Common-name false positives in a different country | 10 | False |
| Unrelated names with address overlap | 5 | False |
| Unrelated synthetic background controls | 60 | False |
| **Total** | **120** | **45 true / 75 false** |

The common-name cases intentionally represent different **synthetic identities**, even when their names are equal. Country mismatch alone does not prove two real records describe different people; the negative label follows the fixture's planted-case contract. Labels were not inferred from model predictions or merely assigned from string equality. Removal targets are positive identity pairs at v1; later removal is list lifecycle behavior, not evidence of different identity.

The production adjudication cutoff is deterministic score `>= review_threshold - 10`, currently **55**. Only **55** benchmark pairs meet it: the 45 true planted cases and ten common-name false positives. The other 65 requests are diagnostic probes; their model results must not create application candidates or bypass deterministic candidate generation.

Frozen input SHA-256:

| File | SHA-256 |
| --- | --- |
| `fixtures/csl_sample_v1.json` | `a3d69363591beae1393e60a2b04edbfde60df440c0074292c2df940d7b9ec3a4` |
| `fixtures/parties.csv` | `679766d38401aa3311e438ccc4661ff364568755839bc5c6743fbbacdcce6d04` |
| `fixtures/expected/hits.json` | `bc91c6dc93dced4cb78e3fd34cb5b89fb81745e63b4458761076e54318b6a8bb` |

## Actual HTTP contract and budgets

Send JSON with the exact verified question map and claim, explicitly select multilingual, and bound this run to 1,024 tokens:

```json
{
  "state": {"party": {}, "listing": {}, "components": {}},
  "model": "multilingual",
  "max_len": 1024,
  "questions": {
    "same_party": {
      "type": "noul",
      "instructions": "These two records refer to the same party."
    }
  }
}
```

The actual populated party has original name, aliases, type, normalized country, city, address lines, and normalized compared name. The listing has original name, alternate names, type, countries, full address objects, remarks, and normalized compared name. `components` is the existing deterministic scorer's complete numeric breakdown. The dataset identifiers and target labels are retained in the harness results, never sent as answers or label hints. Metadata URLs and the full raw government row are not needed for this narrow identity judgment and were excluded consistently before budgeting.

The checkpoint config has `max_len=1024`, `head_max_len=256`. This **does not reserve 256 tokens unconditionally**: using the installed publisher `state_room` and `build_sequence` with the exact tokenizer, question, default true/false options and special tokens yielded a 35-token actual head/closing overhead and **989 state tokens**. Larger or different instructions/options must be measured again; a generic 768-state-token estimate is conservative, not the measured room for this question.

Before every request, serialize state with the publisher's `serialize_state` (`json.dumps(..., ensure_ascii=False)`), count tokens with the downloaded checkpoint tokenizer, and apply only the specified order when required:

1. Drop listing remarks.
2. Clear party street address lines and remove the `address` street-line field from each listing address; keep available city, region, postal code and country context.
3. Drop alternate names beyond the deterministic scorer's best-matching alternate alias, if that best match is an alternate alias. Preserve both primary names and normalized compared names.

If essential state still exceeds the measured budget, stop rather than silently slice identity evidence. The harness checks publisher `build_sequence(..., return_truncation_stats=True)` reports zero discarded state tokens. This check passed for all 120 pairs and the stress request; response `usage.input_tokens` matched the measured final sequence length on all 120 pairs.

Observed largest ordinary state was 1,228 tokens before trimming and 962 after trimming. Exactly one pair (`control-38`) needed remarks then address-line trimming, producing a 997-token whole sequence. An additional artificial oversized-state probe added public synthetic repetitive remarks, street lines and extra aliases: **24,725 → 322 state tokens**, **357 total tokens**, executing all three stages with zero runtime state-token loss. The exact fixture question survived in every request. The stress call returned HTTP 200, probability 0.9998, 357 input tokens, and took 67.56 ms. This stress probe checks budget handling and successful inference, not identity quality.

A real response for `case-exact-01` was HTTP 200:

```json
{
  "model": "laya-rl-agent",
  "answers": {"same_party": {"type": "noul", "noul": 0.9987}},
  "usage": {"input_tokens": 538, "output_tokens": 0}
}
```

All **121** actual inference requests returned HTTP 200 with a `type: noul` and finite probability in `[0,1]`. Noul has no separate confidence field. This is successful transport/schema inference, not proof of correct identity decisions.

## Measured discriminatory effect

Deterministic values came from the existing `score_pair` implementation, not a duplicated approximation. Compare those rankings to raw Laya probabilities and the design's proposed `round(0.4 * det_score + 0.6 * 100 * p)` experiment. AUROC uses every positive/negative pair with half credit for ties; it assesses ranking, not whether a deterministic score is calibrated.

| Scope | Deterministic AUROC | Laya AUROC | Proposed numeric blend AUROC |
| --- | ---: | ---: | ---: |
| All 120 pairs, 45 true / 75 false | 1.0000 | 0.3191 | 0.9363 |
| Original 60 planted pairs, 45 true / 15 false | 1.0000 | 0.3533 | 0.8733 |
| 55 cutoff-eligible pairs, 45 true / 10 false | 1.0000 | 0.4800 | 0.8444 |

All-pair probability threshold accuracy at `p >= 0.5` was **36.67%** (planted-only **71.67%**, partly reflecting its 75% positive prevalence). All-pair Brier error was **0.6340** for Laya, versus **0.0859** when treating deterministic score/100 as a diagnostic pseudo-probability. Planted-only errors were 0.2929 versus 0.1306. Deterministic scores are ranking heuristics, so this comparison does not establish calibrated deterministic probabilities.

| Group | Mean deterministic score | Mean Laya probability | Laya range |
| --- | ---: | ---: | --- |
| Exact | 100.0000 | 0.89006 | 0.5059–1.0000 |
| Diacritics | 100.0000 | 0.93384 | 0.6291–1.0000 |
| Token order | 100.0000 | 0.99909 | 0.9950–1.0000 |
| Legal suffix | 100.0000 | 0.78405 | 0.0000–0.9978 |
| Removed targets in v1 | 100.0000 | 0.99098 | 0.9754–0.9995 |
| Common-name false positives | 85.0625 | 0.99710 | 0.9910–1.0000 |
| Address-only false positives | 33.7080 | 1.00000 | 1.0000–1.0000 |
| Unrelated controls | 17.8540 | 0.979115 | 0.0002–1.0000 |

At a 0.6 model weight, **all ten eligible common-name false positives changed from review to likely**. The address-only/control results illustrate the model's failure to discriminate here; their hypothetical blend changes are not production queue changes because they do not meet the adjudication cutoff.

The safety rail was applied to every experimental blend. All **45** deterministic likely matches stayed `likely`, including `case-legal_suffix-01` and `case-legal_suffix-10`: `det_score=100`, **actual Laya p=0.0000**, numeric blend 40, bucket still `likely`. Keeping a zero-weight default avoids new model-induced promotions while continuing to expose measured probabilities when the optional model is explicitly enabled.

Inference took **11.3823 seconds total for the 120 sequential HTTP calls**, including cold first inference **2.8062 seconds**. Warm mean was **72.07 ms**, warm median approximately **66.9 ms**, warm p95 **109.72 ms**; maximum including cold start was 2.8062 seconds. This excludes install/download/startup and tokenizer/context preparation and reflects this Mac, not small-VM throughput. The oversized stress probe was additional to these latency figures. No broad application suite was rerun for this research-only work.

## Reproduction and retained runtime

Ignored runtime artifacts remain under `.workspace/laya-validation/`:

- `benchmark.py`: exact executed harness, SHA-256 `e599bf761a5b065844155e66ff7a28fd95ef9b047331a1176b2ebbeb8b0341be`.
- `results.json`: all populated requests, raw responses, labels, deterministic scores, experimental buckets, timings and measured budgets.
- `summary.json`, `stress.json`, `health.json`: summary, oversized probe and actual device/revision/fallback evidence.
- `requirements.lock.txt`, `pypi.json`, `model-card.md`, `server.log`, `benchmark.log`: runtime/source/log evidence, all nonsecret and ignored.
- `venv/`, `uv-cache/`, `hf-cache/`: local dependency/model data, never committed.

On this machine, rerun only the harness against the retained server:

```sh
HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1 \
  .workspace/laya-validation/venv/bin/python \
  .workspace/laya-validation/benchmark.py
```

For a fresh equivalent isolated environment, use Python 3.12 and the exact retained dependency snapshot. Fetch only the pinned public checkpoint; preserve normal TLS checks. A matching server launch is:

```sh
HF_HOME="$PWD/.workspace/laya-validation/hf-cache" \
HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1 \
LAYA_REVISION=7b928d828b7b0e022f929d9bd2e44165aa270148 \
LAYA_HOST=127.0.0.1 LAYA_PORT=8009 \
LAYA_MODELS=multilingual LAYA_PRELOAD=1 LAYA_JEV_STRICT=1 \
  .workspace/laya-validation/venv/bin/laya-serve
```

The executed initial server resolved the public current revision and records the exact resulting pin above; the restart command pins it explicitly. Keep this server available for the later in-app milestone 8 gate. At handoff it remained alive in tool session **80434**, PID **81342**, listening only on **127.0.0.1:8009**; port 8000 is the app and 8001 is reserved for the separate gate. The local HTTP endpoint is loopback-only and unauthenticated. A future container must use an intentionally configured reachable/authenticated address rather than assuming container localhost reaches this host process.

The retained harness/results are machine-local evidence, not committed release artifacts. This lasting report records methodology, exact fixture/checkpoint fingerprints, source contracts, representative raw values and the recommendation. The final app integration should also have a committed reusable validation path for future releases.

## Limits and tuning path

The fixture deliberately makes deterministic positives strong and common-name negatives difficult but clearly labeled. Its perfect deterministic AUROC must not be generalized to unseen sanctions identities, transliteration families, or real customer data. These are 120 labeled probes against a 300-row public sample, not all 618,000 comparisons across the complete synthetic book, and there is no held-out domain validation. No model/claim tuning was performed after seeing the labels; changing the wording/state/checkpoint requires a fresh measurement. The generic multilingual checkpoint is not a sanctions-specific specialist.

Keep the default model weight zero. Export historical labeled snapshots from actual reviewer decisions once the app's training export is ready; preserve label lineage and separate training, calibration, and held-out evaluation by identity and time. The publisher's [fine-tuning notebook](https://github.com/NandhaKishorM/laya/blob/main/notebooks/laya_finetune_typed_decisions_2xT4_kaggle.ipynb) describes offline specialization and calibration; **running that fine-tune remains outside v1 scope**. Tune a prospective weight/threshold only on held-out evidence, compare discrimination, false-negative behavior and false-positive promotions with deterministic scoring, then rerun the immutable likely rail and unreachable/invalid-answer fallback gates. No tuning may weaken audit/privacy protection or permit the model to generate candidates.
