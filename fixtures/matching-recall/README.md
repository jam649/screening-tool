# Independently frozen matching recall controls

The main performance list is separately frozen as `public-csl-25000.json.gz`.
`public-csl-provenance.json` records the official public capture SHA, original 300
rows copied unchanged, deterministic seed/source/ID hash selection and output
checksums. No customer data is included. It is not an identity-label dataset.
The benchmark defaults to these public rows; `--synthetic-list` and `--dense`
select the earlier repetitive synthetic stress distribution explicitly.

These are synthetic identity labels, not actual government listings or customer
records. The labels were written and SHA-256 frozen before changing the matching
algorithm in milestone 9. They do not follow engine output. The original
300-entry/2,060-party/60-case fixture contract remains untouched.

`cases.json` contains 120 positive identity pairs and 60 negative/confounder pairs.
The 80 `must_find` positives cover equivalent Unicode representations, provided
aliases, token order, supported suffixes, exact short names and a first-run
three-block recall control. Cross-script and romanization-family positives remain
positive labels but are separately reported linguistic diagnostics; a fuzzy name
score is not proof of identity. Negative common-name/country and similar-company
controls may enter review, but must never reach likely under the default policy.
Unrelated names sharing an address must remain below the candidate cutoff.

`mechanism-controls.json` contains 31 additional positive source-pairs in two
isolated candidate-index controls: one alias-heavy distractor cannot consume the
fuzzy-choice slots of a target, and all 30 independently identified source records
for one synthetic identity must survive a fallback query. The main corpus's alias
cases and these isolated controls intentionally measure different mechanisms.

`freeze.json` and `mechanism-freeze.json` identify the original exact bytes. Never
retune these labels or replace the SHA values to accommodate matcher output. A
new independently reviewed case set should receive a separate version.

Run the source-only recall measurement with:

```sh
PYTHONPATH=backend backend/.venv/bin/python scripts/gates/milestone9_matching.py --recall
```

The report distinguishes candidate, stored and review/likely recall by family.
Production-engine integration tests additionally verify immutable original
snapshots and the declared must-find outcomes.

Run a fresh local profile from the repository root (Python environment includes
the normal backend dependencies):

```sh
PYTHONPATH=backend backend/.venv/bin/python scripts/gates/milestone9_matching.py \
  --fixture-benchmark --workspace .workspace/matching-m9/new-5k-public \
  --parties 5000 --entries 25000 --seconds 300
```

The workspace must not exist. There are no resets or resumptions. `--profile`
adds cProfile overhead, `--cold-only` omits the repeat diagnostic, and `--dense`
selects the explicitly repetitive synthetic list. Setup is timed separately.
“Cold” means the first screen after the actual import; pure normalization caches
can already be warmed by setup. The token index is built for each actual run.
`report.json` records full scope/evidence, zero-hit participants, no-model proof,
resource measurements and original evidence checksums. Complete runs also report
the five/ten-minute and 3-GiB targets as separate booleans, without implying a
universal guarantee or omitting any stored comparison.

Only the coordinator may run the 20k book in its declared 2-vCPU/4-GiB container:

```sh
PYTHONPATH=/app/backend python /tmp/gates/milestone9_matching.py \
  --fixture-benchmark --workspace /data/new-m9-public-20k \
  --parties 20000 --entries 25000 --allow-20k-fixture --seconds 600
```

Mount the complete frozen fixture directory at `/app/fixtures`; no network is
needed. All party data is synthetic and generated keys stay in process memory.
The original 2,060 field values are preserved, with an empty alias column for those
rows; extra parties mix regional company names, personal names/middle initials,
aliases and 30 countries. There are no candidate, hit or audit caps.
