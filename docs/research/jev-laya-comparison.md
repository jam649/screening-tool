# Jev through OpenRouter versus local Laya

Status: **actual paired comparison completed on 2026-10-14**, with 120 successful Jev calls through OpenRouter and 120 fresh local Laya calls. There were no retries, errors, or incomplete pairs. The coordinator retrieved the authorized OpenRouter credential in memory; credentials and raw provider records were not published. An independent source-and-result review verified every input, usage charge, quality statistic, and file permission. Product adapters and default weights remain unchanged.

Jev distinguished these synthetic identities much better than Laya, at an actual inference fee of **$0.003261426 for all 120 comparisons**. Its mean request latency was **209.86 ms**, versus **71.33 ms** for local Laya. The deterministic baseline still had better accuracy than either model at the diagnostic 0.5 threshold; these results do not establish production calibration or justify enabling model weighting.

## Fixed paired experiment

The primary comparison uses the exact 120 inputs from the earlier local benchmark: 60 planted fixture cases and 60 unrelated controls, with 45 positive and 75 negative identity labels. These labels describe identity, including fixtures for removed listings; they do not constitute a legal sanctions determination. The named Noul question is unchanged: “These two records refer to the same party.” Both providers receive identical structured states and questions, including the original deterministic components. Ground truth, categories, case identifiers, and prior model answers are kept outside model input.

[`scripts/research/compare_adjudicators.py`](../../scripts/research/compare_adjudicators.py) reconstructs all inputs from the public CSL/CSV/expected fixtures and the pinned offline Laya tokenizer. It never imports or executes the earlier network-running benchmark. Fixture hashes and the complete input digest are pinned. All reconstructed requests also match the authorized retained private file; that file supplies earlier Laya answers and timings when explicitly selected.

- Complete state/question input digest: `f6e69d38fb8298c0e00011a26257e21e75e15fafa75367f215356d7eb5ace68f`.
- Earlier private result-file digest: `039b9b5b9360a3e129deb0d011fdc8d5f74a3f875380f0e60e132f07a4166aeb`.
- Laya head plus state stays below 1,024 tokens; maximum observed sequence length is 997. All 120 retained cases reported zero runtime state-token drops. One case needed explicit input trimming.

This is a small synthetic, fixed corpus, not production calibration or a blinded sample from customer records. New recall diagnostics belong to a separately labeled experiment after completing the primary 120; they must not replace or contaminate this comparison.

## Verified provider contract and spend guard

OpenRouter accepts a Decisions POST at `https://openrouter.ai/api/alpha/decisions`, with structured `state`, named `questions`, and model `typesafe/jev-1.13`. The documented answer contains `answers.same_party.noul`; response metadata includes returned model/provider and `usage.input_tokens`, `output_tokens`, and `cost`. The currently published returned model is `typesafe/jev-1.13-20260917`. [Decisions API](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request), [Jev tutorial](https://openrouter.ai/blog/tutorials/how-to-use-jev/).

The official endpoint metadata currently publishes $0.042 per million input tokens, $0 per output token, and a maximum of 32,000 input tokens. These are inference prices, excluding unmeasured account credit purchase fees and taxes. [Model endpoint metadata](https://openrouter.ai/api/v1/models/typesafe/jev-1.13/endpoints).

The script selects only TypeSafe, disables fallbacks, and sets the documented provider price caps at those rates, with zero per-request pricing. It checks the public endpoint metadata before inference and fails closed if the model identity, input limit, or prices exceed the pinned bounds. [Provider routing and price caps](https://openrouter.ai/docs/guides/routing/provider-selection).

Before every sequential paid call it reserves the full published maximum charge, **$0.001344**, against the **$0.10** ceiling. Only valid returned usage releases the unused part. It makes no paid retry; 401/402 and other failed or ambiguous calls stop execution, retaining the full reservation if no valid charge is returned. An invalid probability still retains a valid actual charge. If the provider violates its published bound, the script records the actual returned charge and stops rather than misreporting the fee. The bound depends on the vendor enforcing its published token limits and price caps; an independently capped OpenRouter key can additionally constrain account billing. Unknown requests cannot be resumed automatically or silently charged again.

Noul is a probability of the requested proposition, not a separate confidence score. Finite numeric answers in [0, 1] are required; booleans and invalid usage are rejected. [TypeSafe Noul](https://docs.typesafe.ai/primitives/noul), [structured state](https://docs.typesafe.ai/concepts/state).

## Actual paired measurements

Both providers processed the same 120 original inputs in fresh sequential batches. Jev returned `typesafe/jev-1.13-20260917` from provider `TypeSafe` on all 120 calls. Local Laya returned `laya-rl-agent` for the `multilingual` request on all 120 calls. The new Laya probabilities exactly reproduced the earlier retained probabilities; the timings below are from the fresh run.

| Measurement | Jev via OpenRouter | Fresh local Laya | Deterministic baseline |
|---|---:|---:|---:|
| Completed primary comparisons | 120 | 120 | 120 |
| AUROC | 1.0000 | 0.3191 | 1.0000 |
| Brier score, lower is better | 0.05294 | 0.63401 | 0.08589¹ |
| Accuracy at diagnostic threshold ≥ 0.5 | 87.50% | 36.67% | 90.83%¹ |
| True positives / false negatives | 45 / 0 | 43 / 2 | 45 / 0 |
| False positives / true negatives | 15 / 60 | 74 / 1 | 11 / 64 |
| Identity recall at 0.5 | 100% | 95.56% | 100%¹ |
| Specificity at 0.5 | 80.00% | 1.33% | 85.33%¹ |
| Planted-only accuracy | 75.00% | 71.67% | — |
| Mean request latency | 209.859 ms | 71.332 ms | Not measured here |
| Median request latency | 193.824 ms | 60.701 ms | Not measured here |
| P95 request latency | 305.545 ms | 96.307 ms | Not measured here |
| P99 request latency | 357.143 ms | 311.088 ms | Not measured here |
| Maximum request latency | 448.615 ms | 730.596 ms | Not measured here |
| Sum of request durations | 25.183 s | 8.560 s | Not measured here |
| API inference fee, entire run | $0.003261426 | $0 | $0 |

¹ The deterministic baseline uses score/100 and a cutoff of 50 for this diagnostic table. Its score is not a calibrated probability, and this cutoff is not the product’s review/likely thresholds. Its Brier statistic therefore compares numerical errors without establishing probability calibration.

Both models misclassified **all 15 planted hard negatives** at 0.5: ten common-name/country cases and five address-only cases. Jev correctly rejected all 60 unrelated controls; Laya falsely matched 59 of them. Jev found all 45 planted positives; Laya missed two legal-suffix positives. AUROC measures ranking rather than a chosen threshold: Jev ranked positives above negatives in this small corpus while still assigning the 15 hard negatives probabilities above 0.5. A useful operating threshold needs independently held-out cases and the relevant cost of misses/false positives; it must not be tuned and claimed validated on these same 120 cases.

Jev’s mean wall-clock request time was about **2.94×** Laya’s. These are fresh measurements on the same inputs, taken in sequential provider batches rather than interleaved trials. Remote network/service conditions and local server warmth/hardware are not controlled equivalently. They are measured request latencies, not GPU kernel time or concurrent full-book throughput. The earlier Laya mean was 94.85 ms; it is retained for provenance and is not substituted for the fresh 71.33 ms result.

The historical 40% deterministic / 60% Laya diagnostic blend has AUROC **0.9363** on this exact corpus. No diagnostic blend or threshold experiment changes the product’s deterministic default. Live validation of this **OpenRouter research route** does not establish that the separately configured direct TypeSafe application API/account has been tested.

## Independent usage and privacy verification

The actual Jev responses report **77,653 input tokens** and **2,520 output tokens**: a mean of **647.108 input tokens** and **21 output tokens** per comparison. Every one of the 120 individual charges equals `input_tokens × $0.000000042`; output pricing is zero. The summed charge is exactly **$0.003261426**, comfortably within the $0.10 research ceiling, with no uncertain charges or retries.

The independent review compared every stored Jev and Laya request against the original/publicly reconstructed state and question, including serialization order. Pair indices were unique and complete; ground truth and prior answers were absent from requests. All probabilities, labels, quality counts, and latency aggregates were recomputed from individual private records and agree with the summary.

All **242** pricing, summary, and per-call JSON files are mode **0600**, inside a mode **0700** ignored workspace directory. Inspection found no authorization/credential fields, Bearer values, or OpenRouter key patterns. The verifier did not retrieve or inspect the credential itself. Raw provider records remain private. The independently checked summary has SHA256 `fef7aae0e5afa59b44adfb960b97ffd241ebc4512faf134afdd2b1cbfe45d9d0`.

## Per-comparison and full-book projections

Actual Jev cost was **$0.00002717855 per successful comparison**. All 120 calls succeeded, so the priced-request and successful-comparison denominators are identical. The following extrapolations use that observed average input size and current price, with one request per eligible comparison. Serial latency uses the measured mean and excludes database/candidate generation, queue management, and other application work.

| Eligible comparisons | Projected Jev API fee | Projected Jev serial request time | Projected Laya serial request time |
|---:|---:|---:|---:|
| 1,000 | $0.02718 | 3.50 min | 1.19 min |
| 10,000 | $0.27179 | 34.98 min | 11.89 min |
| 100,000 | $2.71786 | 5.83 h | 1.98 h |
| 1,000,000 | $27.17855 | 58.29 h | 19.81 h |

A book with 20,000 parties does not automatically make 20,000 model calls, and it does not evaluate every party against every listing. The relevant multiplier is the number of candidate pairs that actually reach model evaluation after deterministic candidate generation and prior human decisions. A completed full-book measurement is needed to supply that count; no book-specific cost is inferred from the interrupted performance attempt. Input lengths, failures/retries, reuse, concurrency, and provider pricing can change both costs and runtime. Account credit purchase fees and taxes are not included in returned inference `usage.cost`.

Laya’s API fee is zero; electricity, hardware, idle service consumption, and maintenance are not free or measured here. Optional power/tariff arguments produce an explicitly assumed wall-time energy figure: `watts × summed_request_seconds / 3,600,000 × USD_per_kWh`. There are no default wattage or tariff guesses, and hardware depreciation remains unmeasured.

## Reproduction and privacy

Run the network-free checks from the repository root:

```sh
backend/.venv/bin/python scripts/research/compare_adjudicators.py --self-test
backend/.venv/bin/pytest --tb=short -q scripts/research/test_compare_adjudicators.py
backend/.venv/bin/python scripts/research/compare_adjudicators.py
```

A clean clone can reconstruct and validate all 120 inputs without the earlier private result file. To compute the retained baseline locally, add `--retained-results .workspace/laya-validation/results.json`; that original file remains ignored and is not bundled in a public checkout.

The actual run was performed only by the coordinator’s approved credential worker, using `--run-openrouter --run-laya` and the retained file for exact identity validation. For a new explicit comparison, the worker inherits `OPENROUTER_API_KEY` without putting it in arguments or files:

```sh
backend/.venv/bin/python scripts/research/compare_adjudicators.py \
  --run-openrouter --run-laya \
  --retained-results .workspace/laya-validation/results.json
```

For a fresh public checkout, `--run-openrouter --run-laya` also obtains both fresh measurements without requiring the earlier private file. Omitting `--run-laya` while supplying retained results deliberately compares Jev against earlier local measurements instead. Optional `--eligible-comparisons N` and paired `--power-watts W --tariff-usd-kwh R` record explicit projection assumptions. No inherited secret values should be printed, and credential access must not be retried while approval is pending.

By default the script generates a unique output directory; an explicit `--output-dir` must not exist. Each actual run requires a new direct child of the ignored `.workspace` folder, creates it with mode 0700, and creates individual request/response and pricing/summary files with mode 0600 and no overwrite. Redirects, environment proxies, automatic retries, and error-body logging are disabled. The transport requests identity encoding, rejects compressed responses before reading or decoding, and bounds cumulative raw bytes to 1 MiB; already-consumed identity-only mock responses follow the same size bound. A reflected authorization value is blocked before persistence. The console contains only aggregate progress, fixed diagnostics, and an output path. Only reviewed aggregate tables may be copied into this document; raw responses, transcripts, credentials, and runtime logs stay private.

Network-free validation: **31 focused cases passed**, self-test passed, and backend-config Ruff format/check passed. These tests cover exact public/private input identity, label isolation, sequential request accounting, 401/402/no retries, ambiguous timeout charges, price increases, usage errors, truthful unexpected-charge reporting, output modes/no-overwrite, and energy/projection labeling. The actual 120+120 run and its independent checks are complete; direct TypeSafe application-account testing and representative held-out calibration remain pending.
