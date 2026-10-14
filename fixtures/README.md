# Frozen screening contract

`csl_sample_v1.json` contains 300 unchanged public government listings selected from the official ITA bulk export. `provenance.json` records the download URL, raw SHA-256, source timestamps, seed and twelve populated source lists. The source metadata also contains FSE, which had no current listings in the verified export; no FSE listing is invented.

`csl_sample_v2.json` removes the five planted removal targets, changes one synthetic alternate alias on each of five other rows, and adds five real government rows outside the original sample. `expected/csl_diff.json` identifies the exact source/published-ID pairs and the required 5/5/5 counts. Published IDs are not globally unique; always include their source list.

`parties.csv` is entirely synthetic test data: 2,000 background companies/individuals in thirty countries plus sixty planted cases. The planted cases intentionally resemble public restricted-party records to exercise matching; they are not actual customer/vendor records. `expected/hits.json` fixes bucket ranges and address-only exclusion, not individual scores. `expected/file_hashes.json` detects accidental changes to these frozen inputs.

Reproduce the fixtures using a separately downloaded complete official bulk file with the recorded checksum:

```sh
backend/.venv/bin/python fixtures/generate.py --bulk .workspace/csl-bulk-verified.json
```

The generator makes no network requests. A different bulk file produces a different dataset: treat that as a new, explicitly reviewed fixture contract, not a routine refresh. The reproducibility test runs locally when the original public bulk file is present; CI always checks fixture hashes, counts, source coverage, diff counts and planted behavior.

For CSV upload, map each header to the same named field. `external_id` is stable in this fixture. Background results may produce additional low/review candidates; the sixty target-pair outcomes are the contract. The visible review queue must stay stable on an unchanged second run; extra per-run comparison history remains in the audit file.
