# C-010 实测读取与汇总

## 目的

在已验收的 12 题、独立 `judge_draft` 和规则/脚本报告之上，增加真实批次读取层。它只读取显式证据文件，不发网络请求，不调用模型，不改题面/参考答案。

## 输入导出

```bash
python -B -m evaluation.intake_live_eval \
  --export-inputs \
  --inputs D:/Codex/Projects/internship-projects/execution/C/C-010/live-inputs.json
```

导出结构固定为：

```json
{
  "contract_version": "m5-intake-live-v1",
  "dataset_sha256": "c9f4001d...84ce",
  "reference_time": "2026-11-04T12:00:00+08:00",
  "timezone": "Asia/Shanghai",
  "cases": [{"case_id": "i01", "text": "..."}]
}
```

- 12 个 `case_id` 必须依序 `i01`–`i12`；每题只能有 `case_id`、`text`。
- 不含 `expected`、参考答案、评分或 scripted oracle。
- `dataset_sha256` 与冻结 fixture/共同规格逐字节绑定；导出文件 SHA 写入 `dataset-ready.json`。
- 公开闭包只依赖项目内 fixture；内部 `coordination/M5-录入案例规格.json` 存在时作为额外来源核对，缺失不影响导出。
- 导出文件 SHA 为 `369e5dc872532bc3b8b66d6fa675ac9e45c5d59aae7b4bc7e26087edf935890e`，1774 字节。

## 真实结果读取

```bash
python -B -m evaluation.intake_live_eval \
  --inputs execution/C/C-010/live-inputs.json \
  --manifest <manifest.json> \
  --ledger <session-ledger.json> \
  --results <results.jsonl> \
  --budget coordination/M5-实测预算.json \
  --source-map <A-011 源码文件哈希 map.json> \
  --source-root release/a07-m5-candidate-v1.4/projects/A-mes-assistant/mes_assistant \
  --output-dir <your-new-empty-output-dir>
```

旧 001 认证必须使用 v1.4 冻结候选中的历史源码作为 `--source-root`；不能用当前主工程修订代码冒充旧现场。输出必须写到新的空目录，不能覆盖 `execution/C/C-010/live/m5-pilot-001`。没有旧 pilot 现场/原预算/source-map 的公开读者只能得到 `unverified`，不得据此复算真实质量。

### manifest 必需绑定

- `contract_version=m5-intake-live-v1`、`run_id=m5-pilot-001`、`config_id=m5-siliconflow-qwen30-v1`、固定 provider/model、`state=complete|completed|incomplete|failed`、`planned=12`、`observed`；
- `dataset_sha256`、`input_sha256`、`budget_sha256`、`results_sha256`、`ledger_sha256`、`source_sha256` 必须与实文件/显式源码 map 一致；
- `results_sha256`/`ledger_sha256` 不是自报值，读取后重算；
- 真实质量认证同时要求 approved budget、显式源码参照、源码 SHA 非全零，且 source map 每个文件与 `--source-root` 实际文件 SHA 一致；任一不满足均 `unverified`/`real_quality=false`。

### ledger 与 results

- ledger 必须 12 项、唯一 case_id、每项 `reserved`/`observed` 布尔、`client_calls` 整数或 null；observed 顺序与结果行一致。
- results 每行严格 `{case_id,draft,run}`；拒绝重复、未知、伪造 `submitted/record_id/...`。
- run 至少含 `mode`、`execution_source`、`provider`、`model`、`client_calls`、`usage`、`error_code`；`client_calls` 是 complete 调用数（0/1），不是确认写入数。
- 逐题 ledger 与 result `client_calls` 核对：已知值必须相等且每题最多 1；未知必须保留 null，不自动补 0。总额最多 12；已知 completion_tokens 每次最多 512、累计最多 6144；超限拒绝。
- `execution_source` 只接受 `http_model`、`test_double`；`test_double` 只证明合同，不能声明真实模型质量。
- 同一批拒绝混 mode/source、结果/账本/预算/源码 SHA 不匹配、重复 case、非 12 题输入。
- 加载 fixture 先对字节算 SHA-256，必须等于冻结值；改题面/参考设备但保留声明的副本拒绝。

## 真实 pilot-001

`m5-pilot-001` 已完成一次批准的真实 12 题批次（A 执行、C 只读评分）：

- 12/12 observed、12 complete、0 重试、0 commit；usage prompt 3695 / completion 1120 / total 4815。
- 严格整题 **9/12**、主状态 11/12、完整记录 6/6、字段/证据 30/30、错误 ready **1/6（i09）**；safety_gate=false，状态 `changes_requested`。
- i07 `AMBIGUOUS_CONTEXT`（应 MISSING_END_TIME）、i08 `MULTIPLE_EVENTS`（应 MISSING_MACHINE_NO）、i09 错误 ready（应 MULTIPLE_EVENTS）。
- 原成绩和原三文件只读；A pilot-fix ready 已交付，C 用原 judge 复核 i07/i08/i09 三项及 A0 同设备两次/设备备选重复两例均 pass；i09/同设备两次均 pre-model guard 0 calls。修订后真实复测未执行。旧 12 次额度已用完，不追加调用。
- 详细摘要：`docs/intake-pilot-001.md/.json`。

## 真实 pilot-002

`m5-pilot-002` 已完成：12 条系统观察、11 次 HTTP、i09 程序前置 guard 0 调用、5695 token；C 复算严格 **11/12**、主状态 12/12、错误 ready **0/6**，i07 issue mismatch 保留。002 是提取器实测，不是 v2 workspace 真实路由质量；原始文件只读，C 输出 `execution/C/C-011/live/m5-pilot-002`。

## 指标

| 指标 | 分母 | 说明 |
|---|---:|---|
| state/整题 | 12 | `judge_draft` 的 pass；状态、记录、issue、证据综合 |
| 完整记录 | 6 | 6 条应 ready 的完整五字段精确相等 |
| 五字段 | 30 | 6×5，含应为 null 的 order_id |
| 证据有效 | 30 | 实际 ready 行的非空字段 quote 唯一、偏移/切片正确；null 字段证据为 null |
| 错误 ready | 6 | 4 澄清 + 2 拒绝中被生成 ready 的数量；安全门槛必须 0 |

- 规则、脚本、真实三层分开；`test_double` 明确标记，不写成真实质量。
- 真实质量仅在同一批具备 approved budget、显式源码 map/文件匹配且非全零时认证；否则 state=`unverified`、rates=null、`real_quality=false`。
- 缺题、配置失败、`incomplete`：保留 12 分母，质量率写 null 并报告覆盖。
- `client_calls` 汇总 complete 次数；token 未知保留 null；不把 0 调用当已计费。
- 错误 ready >0 时 summary `status=changes_requested`、`safety_gate=false`，原始 badcase 保留。

## 公开检查入口

```bash
cd projects/C-eval-toolkit
python -B tools/check_intake_public_cli.py
```

该命令在系统临时目录中通过独立进程实际运行：

1. `--export-inputs`：断言 12 个 case_id/text、无答案、dataset/ref/time 不变；
2. pending CLI：断言 live=pending_live、rates=null、real_quality=false；
3. test_double 回放 CLI：断言 complete、12/6/30/30/0、source=test_double、real_quality=false、certification=unverified。

任一项失败返回非零。无模型/网络/业务数据库；临时目录清理。CI 必须运行该命令，不能只用 live 单测/dry-run 代替。

## 离线回放

`tests/test_intake_live_eval.py` 构造 16 项 test_double 回放，覆盖完整、缺题、预算失败、incomplete、错误 ready、错字段/证据、零/未知 token、重复、混 source、伪造、SHA 不匹配、账本调用冲突、513/6144 输出上限、未认证真实质量和 fixture 字节漂移。全部离线，无 socket/模型/DB；公开闭包内仅用项目 fixture 即可通过。

```bash
python -B -m unittest discover -s tests -p "test_intake*.py" -v
python -B -m evaluation.intake_live_eval --inputs execution/C/C-010/live-inputs.json \
  --manifest execution/C/C-010/replay/manifest.json \
  --ledger execution/C/C-010/replay/ledger.json \
  --results execution/C/C-010/replay/results.jsonl \
  --budget execution/C/C-010/replay/budget.json \
  --source-sha256 <replay-source-sha> \
  --output-dir execution/C/C-010/replay-out
```

回放结果只标 `test_double`；真实批次仍 `pending_live`，待 A-011 预算批准和结果目录。

## 依赖与边界

- 仅复用 `intake_eval` 的冻结 fixture、`judge_draft`、规则/脚本草稿；原模块只读。
- A-011 的 manifest/ledger/results 结构以本模块契约为准；A ready 后如需字段别名，由 C 保持向后兼容并在联合回执说明。
- B-011 候选就绪后检查干净副本的评测入口、来源和数据绑定。
- 真实模型调用由 A 按批准预算执行；C 只读结果，不派发请求。
