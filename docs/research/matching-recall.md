# Matching recall and performance evidence

Milestone 9 began after signed milestone 8 checkpoint `c6a685b9b7ab06d496f80e0ece64330b5a7203e1`
passed all eight CI jobs. This document separates independently labeled synthetic
identity cases, candidate retrieval, deterministic scoring and actual audited
engine measurements. No customer records or lists outside the public CSL were imported.

## Frozen labels and unchanged baseline

The separate [recall controls](../../fixtures/matching-recall/README.md) were frozen
before matcher edits. The main 180-case SHA-256 is
`e270de4facd9326a7cd7392140b23a834460e7397221717eb618bdc765963581`.
The two isolated mechanism controls, containing another 31 positive source-pairs,
have SHA-256 `45243cde0f2d26d94c54bcb76228b1447d42772fb502322b842c9fb641023370`.

The unchanged candidate policy retrieved, stored and placed into review/likely
103 of 120 positive pairs. It stored 70 of 80 declared must-find pairs. All ten
first-run three-block controls missed their target. The broader diagnostic gaps
were one Arabic pair, all five Korean romanization pairs and one romanization
family pair. All 60 negative controls avoided likely; all 20 unrelated-address
controls remained below the candidate minimum. These are small synthetic samples,
not estimates of commercial screening accuracy.

The isolated alias-slot control retrieved zero of one target. The distinct-target
fallback control retrieved 20 of 30 targets. The existing fallback ranked name
variants with a 20-choice cap; several aliases of one entry could consume slots.
It also skipped fuzzy fallback when three unrelated token/first-word phonetic
blocks existed. Previously observed pairs were forced back into later runs, but
that safeguard did not establish first-run recall.

## Policy 2 and remaining limits

Candidate policy 2 supplements exact/first-token phonetic blocks with approximate
word blocks across every name part. Words of at least five characters require two
distinct shared trigrams and a RapidFuzz ratio of at least 80. This retrieves
spelling variants independently of whether three unrelated exact blocks exist.
The complete-name fallback retains its existing candidate-minimum cutoff but no
longer truncates name choices; aliases cannot crowd another source identity out
of a fixed top-20 window. There is no result, candidate, hit or audit cap.

On the unchanged labels, candidate/stored/review recall is now 114/120 and all
80/80 must-find pairs are present. The isolated controls retrieve 1/1 alias-crowded
target and 30/30 distinct source targets. No negative reaches likely. Remaining
diagnostics are `Mohammed Al Salem` versus `محمد السالم` and five Hangul/Latin
name pairs (for example `Kim Min Su` versus `김민수`). Character transliteration
does not establish every linguistic name-family equivalence. These six labels
remain positive and missed; no identity dictionary or fixture-specific alias was
added, and the tested uplift does not justify adding a heavyweight dependency.

Run and scoring snapshots include `matching_policy_version: 2`. Existing coverage
is unproven under the new retrieval policy until an operator performs a full
`screen --all`; a delta alone cannot prove complete coverage. Immutable previous
runs/hits/decisions are untouched. Hash rule v2 is unchanged, so remembered human
false-positive and completed-positive decisions still require the original exact
four-hash pairing and latest-human precedence. Policy changes may create new
unresolved queue anchors; a repeat under the same new context creates none.

Pure normalization uses bounded value-keyed caches, including type/legal suffixes
and strict country mode. Hash validation reads only the declared matching hash
fields, not a redacted copy of irrelevant raw metadata. Prepared comparisons
share one original entry snapshot, while each persisted Hit and inference input
receives its own copy. Every candidate receives its complete deterministic score;
only explanations for below-cutoff comparisons that will never be stored are
deferred. All stored components and scores match the original scorer exactly.

## Required primary-source library assessment

Verified on 2026-10-14 without installation: current PyPI versions are
[Rigour 2.5.0](https://pypi.org/pypi/rigour/json) and
[Nomenklatura 4.17.2](https://pypi.org/pypi/nomenklatura/json). Both source licenses
are MIT: [Rigour LICENSE](https://github.com/opensanctions/rigour/blob/3cf1403590aee31445707b0fdafe7d2e97b5b496/LICENSE)
and [Nomenklatura LICENSE](https://github.com/opensanctions/nomenklatura/blob/9f5cd1b3b51ffc968d50e38971460a21e81cd20f/LICENSE).
Code licensing does not authorize importing OpenSanctions datasets.

Rigour offers tagged name parts, organization-form aliases, initials, symbolic
alias alignment, representative aliases and constrained fuzzy residue matching.
Its current [transliteration surface](https://github.com/opensanctions/rigour/blob/3cf1403590aee31445707b0fdafe7d2e97b5b496/rigour/text/translit.py)
admits only Latin, Cyrillic, Greek, Armenian, Georgian and Hangul. Other scripts
are preserved, or dropped when explicitly requested. Blindly replacing our broad
lossy ASCII path would remove useful Arabic/Han/Devanagari behavior. Its
[component tests](https://github.com/opensanctions/rigour/blob/3cf1403590aee31445707b0fdafe7d2e97b5b496/tests/names/test_org_types.py)
cover German legal-form variants and Russian prefixes; embedded Chinese company
suffixes still have an explicit word-boundary limitation. These demonstrate
components, not universal identity recall.

The [Rigour project configuration](https://github.com/opensanctions/rigour/blob/3cf1403590aee31445707b0fdafe7d2e97b5b496/pyproject.toml)
uses a Rust/PyO3 core and adds normality, PyYAML, prefixdate, orjson, python-stdnum
and Jinja2. Current [normality metadata](https://pypi.org/pypi/normality/json)
requires PyICU; [PyICU 2.16.2](https://pypi.org/pypi/PyICU/json) currently offers only
a source distribution, so adoption needs an image build/dependency audit.
Nomenklatura's [configuration](https://github.com/opensanctions/nomenklatura/blob/9f5cd1b3b51ffc968d50e38971460a21e81cd20f/pyproject.toml)
adds FollowTheMoney, DuckDB, scikit-learn, lxml, Rich/Textual and a linkage framework.
Its [name analysis](https://github.com/opensanctions/nomenklatura/blob/9f5cd1b3b51ffc968d50e38971460a21e81cd20f/nomenklatura/matching/logic_v2/names/analysis.py)
also documents ID-only cache invalidation and cross-script pruning constraints.
It is not a small normalization replacement.

The current [text-unidecode](https://pypi.org/project/text-unidecode/) remains under
the explicitly chosen Artistic license; it produces a basic lossy character
transliteration. [Double Metaphone](https://github.com/oubiwann/metaphone) encodes
English phonetic irregularities from several language origins, without a semantic
name-family dictionary. The evidence supports fixing the measured candidate gaps
first. A future Rigour A/B should demonstrate additional independently labeled
recall and quantify false positives before accepting its build/runtime cost.

## Real-engine benchmark contract

[matching.py](../../tests/integration/matching.py) creates only a
fresh explicit synthetic workspace. Actual migrations, CSL service, validated CSV
connector, audited `apply_sync` and protected `screen_all` write original evidence.
Setup is timed separately. Models, SMTP and the background scheduler are disabled.
No evidence/audit writes or candidate comparisons are omitted for the benchmark.

The main list is a frozen 25,000-row subset of the verified 26,128-row public CSL
capture, SHA-256 `fd63c5fcfbe921972ef691785c4dd4573a783ec7425787270b25e399f2b889e3`.
It copies the original 300 raw rows unchanged, then selects 24,700 remaining
source/ID identities by SHA-256 rank of JSON `[20261014, source_list, csl_id]`.
The [manifest](../../fixtures/matching-recall/public-csl-provenance.json) records
the source URL, input/output fingerprints and deterministic gzip capture.
It is public government list data, not customer records or a new list scope.

Publication preparation independently reconstructed the subset from the retained
full capture using standard JSON/source-ID selection, without calling the gate's
freeze/load helpers. Full-capture, decompressed and compressed byte counts/SHA-256
matched the manifest; every selected raw row matched its unmodified capture row,
all 12 source codes were present, and deterministic gzip reconstruction (mtime
zero) matched exactly. Nine original fixture/support files, including the
2,060-party/300-entry/60-case contract, were byte-identical to published milestone
8 HEAD. All seven new recall fixture files and decompressed government bytes had
zero provider-key, private-key, credential-assignment, actual private-home/runtime
or transcript indicators; documented dummy workspace paths remain public usage
examples. No known secret values were available in the inherited environment,
so this check did not claim coverage of unavailable Keychain/runtime credentials.
No credential stores, network, customer databases or Docker runtimes were read.
The coordinator's definitive exact-staged-byte scan remains a separate release gate.

The selected list has 24,148 distinct normalized primary names, at most eight
identical names and 784 repeated-name groups across all 12 populated source lists.
Its 36,988 token blocks have p95 size seven, p99 size 34 and maximum 1,552;
the largest first-token phonetic block contains 1,559 entries. The earlier
repetitive synthetic list has only 1,786 distinct primary names and up to 67
identical names. It remains available with `--synthetic-list` or `--dense` as a
stress diagnostic, and is not described as a representative public-list baseline.

Parties remain explicitly synthetic; the full book contains all original 2,060
CSV rows, including the planted controls. Larger books mix business words,
personal names, aliases, types, addresses and 30 countries. Cold/repeat measurements report wall
and CPU time, process RSS, phase timings, candidate distributions, fallback counts,
SQL counts, audit/database growth and queue/coverage reads. The repeat must have
zero new queue anchors and preserve the original active IDs. Every selected party
must have immutable participation evidence, including zero hits; the complete
scope records actual party and entry hashes.

The unchanged engine's 1k-party/1k-repetitive-entry profiled run completed in
55.218 seconds and wrote 12,561 hits. Its original harness misread the coordinator's
nested success payload after commit; read-only persisted attempt timestamps prove
the timing, but no detailed CPU profile survived. The first 1k/25k repetitive-list
attempt hit the explicit 120-second bound and atomically retained zero runs/hits.
These dense results are not a routine-book performance claim. The harness now
reads `outcome.run_ids`, preserves bounded failure summaries and distinguishes
engine peak RSS from the full gate peak including queue/coverage reads.

Actual public-list measurements on the local Darwin host (eight reported CPUs,
single synchronous engine, no CPU/memory constraint) are:

| Measurement | Cold | Unchanged repeat |
| --- | ---: | ---: |
| Pre-final-review 5k/25k wall seconds | 123.571 | 141.851 |
| CPU seconds | 122.752 | 133.815 |
| Preparation seconds | 99.649 | 107.360 |
| Atomic writer seconds | 22.611 | 32.250 |
| Engine / full gate peak MiB | 864.42 / 864.42 | 864.42 / 864.42 |
| Queue read seconds | 1.457 | 4.249 |
| Coverage read seconds | 1.273 | 1.617 |
| Actual candidates / hits | 1,323,360 / 40,225 | 1,323,360 / 40,225 |
| New / current queue anchors | 4,482 / 4,482 | 0 / 4,482 |
| Participation / zero-hit parties | 5,000 / 1,529 | 5,000 / 1,529 |

Setup took 8.881 seconds. Candidate p50/p95/max were 104/1,546/2,199, with 135
fallback parties. The actual database grew from 629,854,208 bytes after cold to
1,091,346,432 after repeat; WAL peaks at the recorded checkpoints were
466,524,112/479,415,592 bytes. There were 70,241/110,473 total audit rows. Every
original Hit, Run and party participation row retained its within-book SHA-256
fingerprint after repeat. Full current scope hashes/coverage and disabled-model
provider/call-count/null-probability checks passed. Detailed databases/reports stay
ignored; UUIDs/timestamps mean different fresh books have different evidence hashes.

Before scalar historical projections, the same 5k inputs produced identical
candidate/hit/queue counts but repeat full-process RSS was 1,886.09 MiB and queue
read time 10.075 seconds. Historical scans now read only IDs, hashes, status,
bucket and context/anchor references. Complete immutable snapshots are still
loaded for the returned queue anchors and actual prior-human differences. No
history or audit row is omitted or reconstructed. The earlier 1k public run took
45.682 seconds before deferred below-cutoff explanation allocation; it is a
mechanism measurement, not the final implementation's 1k release benchmark.

“Cold” is the first screen after actual import; pure normalization caches can be
warmed by setup, and every run constructs a fresh candidate index. Peak RSS is
process-wide and includes setup. The full gate peak additionally includes queue,
coverage and streamed immutable fingerprint reads. A profiler-enabled baseline
has extra tracing overhead and is not compared as an uninstrumented timing.

The release result is recorded under
[v0.1.0 release benchmark](#v010-release-benchmark) below. The
engineering target is five minutes, with a ten-minute bound and process RSS below
3 GiB on a declared 2-vCPU/4-GiB fixture container; it is not a universal hardware
or AI-enabled performance guarantee. Generated databases and detailed reports
remain in ignored fixture workspaces and are never published.

## Final review and measured follow-up

The independent final review identified historical full-scope retention in queue
eligibility: every completed Run manifest was loaded even for one selected pair.
Queue lookup now streams relevant completed identifiers newest first, loads one
scope, resolves pending pairs, and stops once their newest covering scopes are
known. Identifier-only sorting avoids SQLite sorting every large JSON payload.
No history cap is used; legacy missing scopes, tied ordering, low/suppressed
sentinels, later zero-hit scopes and human decision authority are preserved.
Coverage likewise streams current-policy manifests and combines actual current
entry hashes with compact per-party integer masks. Modern zero-hit participation
is explicit; legacy fallback uses immutable revision hashes, never present-record
reconstruction. A timestamp-only helper serves list/staleness facts, while
selected-party full history is complete and lazy, loading one original Run at a
time. Actual 60-unrelated-run tests verify one queue scope/two coverage scopes and
no ORM Run loads, original anchors/snapshots, zero-hit participation and human
authority; separate legacy/tie controls preserve original behavior.

The low-severity recall aggregate concern did not affect the frozen dataset
because its positive/negative families are disjoint. The aggregate now counts
actual positive labels directly, and an in-memory mixed-family test verifies it;
the frozen label bytes remain unchanged.

The first actual 2-vCPU/4-GiB, network-disabled 20k/25k run on the earlier image hit
the explicit 600-second bound. Setup took 16.740 seconds; all 20,000 candidate
queries completed with 4,492,235 candidate comparisons and 169 fallback parties.
Preparation took 342.760 seconds; the writer was still running at 257.717 seconds
when the alarm fired. Engine peak RSS was 1,645.34 MiB. This is a failed
prior-checkpoint result, not a completed-run performance claim; its
container, volume and report are preserved.

Fresh 1k/25k mechanism profiles retained every actual snapshot and audit write.
The profiled writer spent 2.892 seconds constructing/redacting its final Run
payload, and 0.638 seconds across 3,991 repeated `Session.new` property calls.
Preparation's large immutable entry snapshots incurred 66.944 profiled cumulative
seconds of redaction, with repeated normalization of each key for every marker.
These profiling timings have tracing overhead and are not release benchmarks.
The coordinator owns identical-predicate key normalization, exact immutable
primitive copying and once-per-loop membership sets in the audit helpers; no
bulk/audit bypass is proposed. The engine now freezes its auto-clear policy map
once per preparation rather than 3,966 repeated snapshots in this 1k profile;
inference inputs and persisted explanations still receive independent copies.
Entry geometry/scoring behavior was not changed speculatively. Gate measurement
also reports score/human eligibility separately from actual disabled calls: the
1k profile had 2,204 pairs eligible if a model were enabled, with actual calls zero.

After the verified shared audit optimizations, the identical writer-only 1k
experiment retained 3,965 hits/29,981 total audit rows and reduced profiled writer
time from 8.299 to 3.961 seconds. Run-payload redaction fell from 2.892 to 0.397
seconds; audit guarding from 1.977 to 0.587 seconds; `_safe` from 1.005 to 0.308
seconds. `Session.new` was no longer a leading hotspot. No scorer geometry,
candidate cap, bulk insert or audit omission was added.

The final fresh local 5k/25k cold/repeat measurement passed after those audit and
bounded-history changes:

| Final local measurement | Cold | Unchanged repeat |
| --- | ---: | ---: |
| Wall / CPU seconds | 107.107 / 105.604 | 111.453 / 109.695 |
| Preparation / writer seconds | 86.467 / 19.321 | 89.267 / 20.697 |
| Engine / full gate peak MiB | 748.70 / 748.70 | 795.27 / 795.27 |
| Queue / coverage read seconds | 1.531 / 0.104 | 2.982 / 0.142 |
| Actual candidate comparisons / stored hits | 1,323,360 / 40,225 | 1,323,360 / 40,225 |
| New / current queue anchors | 4,482 / 4,482 | 0 / 4,482 |
| Actual party / zero-hit participation | 5,000 / 1,529 | 5,000 / 1,529 |
| Eligible pairs if a model were enabled / actual calls | 19,558 / 0 | 19,558 / 0 |

Setup took 9.468 seconds. Candidate p50/p95/max and fallback counts remain
104/1,546/2,199 and 135. Original complete Hit/Run/party-evidence fingerprints
remained identical after repeat; actual scope hashes, current coverage and disabled
provider/null probability proofs passed. The final database/WAL checkpoints were
630,923,264/467,566,472 bytes after cold and 1,093,218,304/480,293,152 after repeat,
with the same 70,241/110,473 total audit rows. Local five/ten-minute and 3-GiB
resource booleans passed. The constrained 20k checkpoint and release results
follow below.

The final history review also identified a tied-start ordering defect: current
observations were ordered by hit insertion time while covering scopes were
ordered by run ID. Both current observations and writer anchor selection now use
the same deterministic run order (`started_at`, then run ID, descending), with
hit timestamps only breaking ties inside a run. Modern complete-scope regressions
invert hit insertion order deliberately and prove unchanged repeats retain the
correct original anchor, a real human decision remains authorized, and a later
zero-hit covering run hides the queue without inventing a decision. Historical
snapshots and audit records are unchanged. The local 5k measurement above had no
such ties and remains a measurement of the immediately preceding checkpoint.

## Constrained 20k candidate checkpoint

The final product image
`f3f8d1d73149f6f5693f219884ce25a6ee9de28b9b4f9a3552b16037be1e9cd3`
completed the fresh 20,000-party/25,000-entry cold run with 2 CPUs, 4 GiB of
container memory and networking disabled. Setup took 13.741 seconds.

| Strict cold measurement | Result |
| --- | ---: |
| Wall / CPU seconds | 400.512 / 391.809 |
| Preparation / writer seconds | 257.465 / 126.034 |
| Engine / full gate peak MiB | 1,643.578 / 1,643.578 |
| Candidate comparisons / fallback parties | 4,492,235 / 169 |
| Stored hits / current queue anchors | 215,624 / 23,964 |
| Actual party / zero-hit evidence rows | 20,000 / 3,506 |
| Eligible pairs if a model were enabled / actual calls | 105,657 / 0 |
| Queue / coverage read seconds | 31.962 / 0.275 |

The cold run proved every actual party and entry scope hash, current coverage,
and disabled-provider/null-probability evidence. It wrote 260,640 total audit
rows; database/WAL sizes were 2,647,789,568/2,459,627,672 bytes. Cold screening
met the ten-minute bound and 3-GiB process target, but exceeded the five-minute
target. Its unchanged repeat finished candidate scoring but stopped when the
local Docker data disk reached zero free bytes, with the protected operation
still running. That attempt is a failed environment checkpoint, not a pass; the
release results below supersede it.

A separate attempt on this same image exited 137 with `OOMKilled` reported and
no completed report. The Docker VM then had roughly 534 MiB available while
30 retained fixture processes were still running. Stopping those completed
processes preserved all volumes and freed roughly 5.94 GiB. A separately labeled
2-CPU/8-GiB diagnostic subsequently completed cold screening in 416.298 seconds
with a 1,622.91-MiB peak, without a product change. This supports global VM
pressure as the cause of that OOM; it does not prove a container-limit failure or
turn the diagnostic into a 4-GiB pass. The first earlier-image 600-second failure
above and both failed volumes remain preserved.

## v0.1.0 release benchmark

The first GitHub-hosted run of the declared container check on `5e4ad66`
([CI run 38574776084](https://github.com/jam649/screening-tool/actions/runs/38574776084))
confirmed 2 CPUs, 4 GiB and no network, then stopped the cold screen at the
600-second bound (603.8 seconds, 1,649.21-MiB engine peak). The same image ran
locally on a host shared with concurrent benchmarks: cold screening took 482.1
seconds and the unchanged repeat was stopped at 601.0 seconds. Both are failed
gate attempts.

A separately labeled local diagnostic of that image used the same container and
canonical gate with a 30-minute per-run alarm. It verifies full-scale repeat
behavior; it does not claim the ten-minute bound. Every correctness check passed:

| Logic diagnostic (not a gate pass) | Cold | Unchanged repeat |
| --- | ---: | ---: |
| Wall seconds | 575.1 | 526.6 |
| Engine peak MiB | 1,644.4 | 2,085.8 |
| Stored hits | 215,624 | 215,624 |
| New / current queue anchors | 23,964 / 23,964 | 0 / 23,964 |
| Party / zero-hit evidence rows | 20,000 / 3,506 | 20,000 / 3,506 |

The original Hit, Run and party-evidence fingerprint was unchanged after the
repeat, and the disabled-model checks passed. Host contention inflated these
times; they are evidence of behavior, not performance.

Two exact speedups then removed most of the measured cost:

- Preparation computes proven upper bounds on every name comparison from batched
  rapidfuzz calls and skips only comparisons and pairs that cannot reach
  `candidate_min` or change the stored best comparison. `token_sort_ratio` equals
  the ratio of sorted-token strings; `token_set_ratio` equals it when the token
  sets are disjoint and unrepeated and is otherwise computed exactly;
  `partial_ratio` is at most 200h/(m+h) for multiset character overlap h and
  shorter length m. Context deltas are exact, and addition, clamping and rounding
  are monotone. Every remaining pair is scored by the unchanged scorer.
- The writer copies JSON evidence with an exact type-preserving tree copy instead
  of `deepcopy` and assigns run counters once at completion instead of rewriting
  the run row at every flush.

Results are unchanged. A differential harness compared base and new preparation
on fixture books under seven weight, threshold, suffix and forced-candidate
policies, with zero differences in candidates, scores, eligibility or
explanations. A deterministic harness compared every persisted table byte for
byte after cold, repeat and auto-clear/human-decision screens, also with zero
differences. Independent differential fuzzing checked about 600,000 string pairs
and 850,000 bound evaluations for the ceilings, 3.9 million explanation
comparisons for the reordered scorer and 650,000 retained pairs for pair
exclusion. `matching.py --recall` output is byte-identical. A batched fallback
scan and a writer garbage-collection pause were deferred after review found a
non-integer-cutoff boundary difference and memory risks; the fallback is the
original implementation.

| Release CI measurement ([run 38612203195](https://github.com/jam649/screening-tool/actions/runs/38612203195)) | Cold | Unchanged repeat |
| --- | ---: | ---: |
| Wall / CPU seconds | 268.854 / 249.707 | 395.484 / 282.245 |
| Preparation / writer seconds | 116.314 / 128.210 | 122.021 / 220.688 |
| Engine / full gate peak MiB | 1,759.61 / 1,759.61 | 2,101.90 / 2,101.90 |
| Stored hits / new queue anchors | 215,624 / 23,964 | 215,624 / 0 |
| Queue / coverage read seconds | 26.014 / 0.253 | 162.622 / 0.972 |

Setup took 19.130 seconds. The container was limited to 2 of the runner's 4 CPUs.
Both runs met the ten-minute bound and the 3-GiB process check. The cold run also
met the five-minute engineering target; the repeat did not (395.5 seconds), so
that target is reported as not met. The repeat created no new queue anchors, kept
the same 23,964 current anchors and left the original Hit, Run and
party-evidence fingerprint unchanged. Each run made 4,492,235 candidate
comparisons with 169 fallback parties and recorded 20,000 party evidence rows,
3,506 of them zero-hit. Total audit rows were 260,640 after cold and 476,271
after repeat; database/WAL sizes were 2,642,317,312/2,454,003,872 bytes after
cold and 5,080,727,552/2,518,234,672 after repeat. With models disabled there
were 105,657 hypothetically eligible pairs and zero model calls.

Reading the review queue scans every stored hit's full record, because the
queue's sort columns follow the large JSON snapshots in each row. On the release
runner the gate's queue read took 26.0 seconds after the cold screen and 162.6
seconds after the repeat, with 431,248 stored hits. Locally, a cold-cache read in
a 4-GiB container holding about 2 GiB of process memory took 247–311 seconds,
and 1,195–1,517 seconds under heavy host I/O contention; with a partly warm page
cache it took 77 seconds. Loading anchors in physical order or enlarging SQLite's page cache
each saved about 20%. Large books therefore need memory headroom for the page
cache, and a schema-level projection for queue reads is follow-up work.
