# M5 停机录入公开评测摘要（真实小测 changes_requested）

- contract: `m5-intake-live-v1`
- dataset_sha256: `c9f4001d87a9e4b5e0a6f44aa308e4f0e84ca17041a87972bd1f646480ca84ce`
- input_sha256: `369e5dc872532bc3b8b66d6fa675ac9e45c5d59aae7b4bc7e26087edf935890e`
- 范围：12 条冻结合成题，单条已结束停机；不是真实生产分布。
- 真实状态：**001 changes_requested（9/12、错误ready1/6）；002 changes_requested（11/12、错误ready0/6）**。002 的 i07 仍 issue mismatch；修订后真实复测未执行。
- 002 实际：12 条系统观察、11 次 HTTP/模型调用、i09 程序前置拦截 0 调用、0 commit；usage 4615+1080=5695 token；主状态 12/12、记录/字段/证据 6/6、30/30、30/30。
- 002 是录入提取器实测，不等于统一工作台 v2 真实路由/查询质量；v2 真实批次另验。
- 真实调用：12 complete、0 重试、0 commit；usage prompt 3695 / completion 1120 / total 4815。
- 公开复算：冻结 fixture + 显式结果路径；无真实模型/MySQL/网络。

## 离线基线与真实

| 层 | 来源 | 状态 | 整题 | 完整记录 | 五字段 | 证据 | 错误 ready |
|---|---|---|---:|---:|---:|---:|---:|
| 规则 | rule_template | complete | 2/12 | 2/6 | 10/30 | 10/30 | 0/6 |
| 脚本 | scripted_oracle | complete | 12/12 | 6/6 | 30/30 | 30/30 | 0/6 |
| 真实 | http_model | changes_requested | **9/12** | **6/6** | **30/30** | **30/30** | **1/6** |
| 真实 002 | http_model | changes_requested | **11/12** | **6/6** | **30/30** | **30/30** | **0/6** |

- 真实 001 主状态 11/12；严格整题 9/12；错误 ready 是 i09（原文两条独立停机，模型只抽第一条并生成 ready）。
- 真实 002 主状态 12/12；严格整题 11/12；i07 仍 issue mismatch（`MISSING_END_TIME`+`MISSING_REASON`），错误 ready 已归零；i09 为程序前置 guard（0 模型调用）。
- i07 返回 AMBIGUOUS_CONTEXT（应 MISSING_END_TIME）；i08 返回 MULTIPLE_EVENTS（应 MISSING_MACHINE_NO）。
- 规则只解析完整显式模板，10 条弃答；脚本 12/12 是合同 oracle，不是模型效果。
- `错误 ready /6` 是 4 澄清 + 2 拒绝的安全门槛；真实 1/6 未通过。

## 修订状态

- A 的 i07/i08/i09 离线护栏/协议修订及 A0 两例已交付；C 用原 judge 复核全部 pass（i09/同设备两次 guard 0 调用，设备备选重复时段归一 `MISSING_MACHINE_NO`）。**修订后真实复测未执行**。
- 原 12 次额度已用完；本摘要不把新离线替身结果写成真实 12/12，也不修改 gold/judge/题面。
- 规则、脚本、test_double 与真实输出分开；后续真实稳定性需另批预算授权。

## 公开检查

```bash
cd projects/C-eval-toolkit
python -B tools/check_intake_public_cli.py
```

该入口在临时目录实际运行 fixture 导出、pending CLI 和 test_double 回放，断言 pending 状态、`test_double` 标记、`real_quality=false`；CI 必须包含该命令。

## 复算命令

已完成的真实结果：

```bash
cd projects/C-eval-toolkit
python -B -m evaluation.intake_live_eval \
  --inputs execution/C/C-010/live-inputs.json \
  --manifest execution/A/A-011/experiment/m5-pilot-001/manifest.json \
  --ledger execution/A/A-011/experiment/m5-pilot-001/session-ledger.json \
  --results execution/A/A-011/experiment/m5-pilot-001/results.jsonl \
  --budget coordination/M5-实测预算.json \
  --source-map coordination/budgets/M5-pilot-001-source-map.json \
  --source-root release/a07-m5-candidate-v1.4/projects/A-mes-assistant/mes_assistant \
  --output-dir <your-new-empty-output-dir>
```

旧 001 的 source-root 必须指 v1.4 冻结候选历史源码；输出必须是全新目录，不能覆盖 `execution/C/C-010/live/m5-pilot-001`。没有旧现场/预算/map 的读者认证不足，只能得到 `unverified`。

CLI 退出 0 只说明报告生成成功；评测结论以 `live-summary.json` 的 `status/safety_gate` 为准，本批为 `changes_requested`。

## 指标口径

- 完整记录 /6：6 条应 ready 的五字段精确相等。
- 五字段 /30：6×5，含应为 null 的工单。
- 证据 /30：实际 ready 行非空字段的 quote 唯一、`[start,end)` 切片正确；null 字段证据为 null。
- 错误 ready /6：4 澄清 + 2 拒绝被生成 ready；>0 即 `changes_requested`，原始 badcase 交总控。
- 缺题/配置失败/incomplete：保留 12 分母，质量率 null，报告覆盖。
- 真实质量必须同时具备 approved budget、显式源码参照且实际源码文件 SHA 匹配；缺失/全零时 `unverified`。
- `client_calls` 是 complete 调用数；token 未知保留 null；逐题账本与结果一致。

## 依赖与未验

- A pilot-fix ready 已交付并经 C 复核；B v1.5 候选已冻结但 C 正在核对同一快照。
- 真实模型质量修订、人工浏览器视觉、Python 3.12、M5 公开发布未验证。
- 当前不声称整体真实准确率、生产覆盖或已发布。
