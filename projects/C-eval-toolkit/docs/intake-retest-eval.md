# m5-pilot-002 读取/评分适配

## 边界

- 父预算：`coordination/budgets/S1-20261107-授权预算.json`，SHA `7a42c64df858d1d737bfc19a584ede8dc3914cd43edd49e8070084cc669c7059`，status=approved。
- run：`m5-pilot-002`；固定输入 `execution/C/C-010/live-inputs.json`（SHA `369e5dc...5890e`）、dataset `c9f4001...84ce`、reference_time `2026-11-04T12:00:00+08:00`、timezone Asia/Shanghai。
- 结果目录：`execution/A/A-012/experiment/m5-pilot-002`；最多 12 次、每题 1 次、零重试、512 输出/次、总输出≤6144、draft_only_no_commit。
- 旧 `m5-pilot-001` 预算/结果/判据不重写、不复用；C 不调用模型。

## 新模块

`evaluation/intake_retest_eval.py`：

- 校验父预算 SHA/approved/002 条目、输入/dataset/模型/结果目录；
- 校验 002 manifest 的 run/parent_budget/input/dataset/source map、ledger/results 一致；
- 独立核 source map 与实际 A source-root 文件 SHA；
- 复用原 `judge_draft`；输出严格整题 /12、记录 /6、字段/证据 /30、错误 ready /6；
- 分列 `http_model_cases`、`program_guard_cases`、`http_model_calls`、usage；guard 0 调用不能冒充模型实测；incomplete/失败保留分母、质量率 null；
- 不修改 001 的 `intake_live_eval`/`intake_eval`/gold。

## 命令

```bash
cd projects/C-eval-toolkit
python -B -m evaluation.intake_retest_eval \
  --manifest <002 manifest.json> \
  --ledger <002 session-ledger.json> \
  --results <002 results.jsonl> \
  --source-map <002 source-map.json> \
  --source-root <A-012 source root> \
  --output-dir execution/C/C-011/live/m5-pilot-002
```

A 的 002 已完成：12 条系统观察、11 次 HTTP、i09 程序 guard 1 条、5695 token；C 复算严格 11/12、错误 ready 0/6，i07 issue mismatch 保留。原始 002 文件只读，C 输出 `execution/C/C-011/live/m5-pilot-002`。002 是提取器实测，不是统一工作台 v2 真实路由质量。
