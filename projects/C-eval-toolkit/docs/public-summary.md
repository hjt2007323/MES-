# Public evaluation summary

**Data nature: synthetic demo only.** This is not a production accuracy or
safety certification.

## Offline candidate demo (separate from historical experiment)

The candidate's `ceval` CLI runs six candidate-local synthetic cases:
three expected passes and three expected fails. It demonstrates exact table
comparison, correct empty table with wrong params, duplicate/missing rows,
clarification zero SQL and refusal zero SQL.

```text
offline cases=6 expected_matched=6 verdict_pass=3
```

This demo is self-contained: no model, database, network, API key, browser or
internal experiment directory is required.

## Historical local model run (synthetic data; read-only MySQL)

- 12 pilot questions × two context strategies (`full`, `focused`).
- 24 requests, 25 model attempts (one allowed correction), 18 questions not run.
- 30-question total rate: **null**.
- full/focused each **12/12 quality pass**; paired changed 0; badcases 0.
- Query/clarification/safety: **8/2/2** pass in each strategy.
- Input/output tokens: full **16,229/577**, focused **12,272/505**; totals
  **28,501/1,082**.
- Median/max internal latency: full **1,675/9,320 ms**, focused
  **1,613.5/4,270 ms**.
- Model: `Qwen/Qwen3-30B-A3B-Instruct-2507` via `siliconflow`; the run used a
  local MySQL instance in read-only mode with synthetic data.
- This section is separate from the six candidate-local synthetic offline
  examples above; it is a reviewed summary, not a new live run.

| strategy | executed | pass | query | clarification | safety | model calls | corrections | input | output | median ms | max ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| full | 12/12 | 12 | 8 | 2 | 2 | 13 | 1 | 16,229 | 577 | 1,675 | 9,320 |
| focused | 12/12 | 12 | 8 | 2 | 2 | 12 | 0 | 12,272 | 505 | 1,613.5 | 4,270 |

## Source binding

Hashes below are for independent internal archives; no host paths or raw logs
are published.

| object | SHA256 |
|---|---|
| dataset | `18769a781d99db6e4399634fa0cd9250fea9c62b5dc4e5cb654517143e748a6c` |
| budget | `2ab139f96b1128fc1c52c2f0ac093c8dd222a0001efd26bde9012888d2a59cd3` |
| manifest | `bd56e81396ce504bfcfa0c3384d80172f48915fa1e01989ce13a303d8cb4f0f2` |
| ledger | `43977f4420afc14a214c42034e26265ff404956e652960cfded2a7ff4388a37f` |
| full results | `82aa690b5e141415a5d3f433ae113f402968877879840ef996ece5405f821370` |
| focused results | `eadc75bfb6269781539c0cb60ef08ec6aaf1c384a2e3546e16d174ea6dbe7ef7` |

## Limitations

- 12/12 is a 12-case pilot, not a 30-case or production accuracy.
- 30-case total rate is null; 18 cases were not executed.
- Synthetic reference tables follow approved demo metrics; they are not
  production labels.
- Offline candidate synthetic data and historical field records are separate.
- No absolute host paths, credentials, raw logs, process/port details or
  database material are included.
