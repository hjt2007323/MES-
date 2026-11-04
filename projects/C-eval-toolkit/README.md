# C offline evaluation demo

A small, dependency-free demonstration of the C evaluation layer for the MES
question-answering project. It judges **candidate-local synthetic cases and
synthetic results**; it never calls a model, database, network or API key.

## What is here

- `ceval/compare.py` — exact table comparison with type normalization and
  row-order-ignore multiset semantics.
- `ceval/quality.py` — status/final-action/query/params/table judging; clarify
  and refuse require zero SQL.
- `ceval/cli.py` — offline report entry.
- `data/cases.json`, `data/results.json` — six synthetic examples, including
  wrong params with the same empty table, duplicate-row difference, clarification
  zero-query, refusal zero-query, and a refusal that wrongly dispatched SQL.
- `tests/test_offline.py` — standard-library regression.
- `docs/public-summary.md`, `docs/public-summary.json` — public historical
  summary derived from the already reviewed M3 experiment; no raw logs or paths.

## Run

From this directory:

```bash
python -B -m ceval.cli --output-dir reports/offline
python -B -m unittest discover -s tests -v
```

The CLI reads only `data/cases.json` and `data/results.json`, writes
`cases.jsonl`, `summary.json` and `report.md` under `--output-dir`, and exits
`0` when every synthetic case matches its expected verdict/reason. No install,
model key, MySQL, browser, or repository checkout is required.

Input binding is strict before any judging: the case and result lists must be
non-empty, IDs must be non-empty strings and unique on both sides, and the two
ID sets must match exactly. Empty sets, duplicate IDs, unknown/missing results
and invalid input objects print a fixed `ERROR input_binding` and exit `2`.
For a query, the final plan's `query_id` must equal the top-level query ID; for
clarify/refuse, the SQL count must be the integer `0`.

Expected summary:

```text
offline cases=6 expected_matched=6 verdict_pass=3
```

## Scope and limitations

- `scope=synthetic_demo_only`; these six cases are illustrative, not the 30-case
  frozen M3 set and not a production accuracy measurement.
- The historical result is summarized separately in `docs/public-summary.md`.
  It came from a local real-model run using synthetic data and a read-only MySQL
  instance; this demo only replays the reviewed summary, not a new live run.
- Candidate data contains no host paths, credentials, raw logs, ports, process
  details, model responses, or database material.
- The comparison is exact; no tolerance/fuzzy matching is applied. Column
  types/nullable, truncation, row count and row multiset are checked.

## Historical summary

- 12 pilot questions × two context strategies; each strategy 12/12 quality pass.
- 24 questions, 25 model attempts (one allowed correction), input 28,501 /
  output 1,082 tokens; 18 questions not run; 30-question total rate is null.
- Query / clarification / safety: 8 / 2 / 2 per strategy.
- See `docs/public-summary.md` for the compact table and source hashes.
