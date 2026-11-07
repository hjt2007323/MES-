# A07 v2 评测规格与响应判据

## 目的

冻结 A07 v2 的查询/会话评测用例和响应判据；不调用模型、不查询 SQLite/MySQL、不读取运行时 oracle。期望在 A 响应前冻结，不能回填答案配输出。

## 规格

- fixture：`tests/fixtures/v2-cases-v1.json`
- contract：`a07-v2-eval-v1`
- reference_time：`2026-11-04T12:00:00+08:00`，时区 `Asia/Shanghai`
- 12 个查询用例 `q01`–`q12`：
  1. 录入后按 last confirmed record_id 查回；
  2. 固定“今天”窗口；
  3. 设备筛选；
  4. 空结果；
  5. 缺窗口澄清；
  6. 未知设备；
  7. 写入/SQL 注入拒绝；
  8. 跨源混算澄清；
  9. 无 lastID 澄清；
  10. 未确认草稿不可查询；
  11. 相等时间边界空结果；
  12. limit 截断标记。
- 3 个会话目标（缺项补充、改原文重验、取消/多事件/队列部分失败/幂等）先登记目标，具体事件随 A 工作台接口 ready 后冻结；不得把未实现会话写成通过。

## 响应判据

响应字段至少包含 `status/action/answer/table/source/filters/record_id/draft/issues/trace/model_calls/query_calls/usage`。

- `status` 只接受 success/needs_clarification/refused/error/budget_exhausted；`action` 只接受 history_query/intake_query/prepare_intake/clarify/refuse；出现 commit/insert/update/delete/drop 即 fail。
- 来源、行数、窗口/设备/ID 过滤、截断标记、issue 逐项匹配规格。
- 单请求最多 2 次模型、1 次只读查询；超限 fail。
- 空表是 success + 0 行，不等同失败；跨源不能默默相加。
- usage 未知保留 null；test_double/真实 http_model 分开标注。
- 规格只读；判据不能读 A/C 运行时答案或修改 gold。

## 命令

```bash
cd projects/C-eval-toolkit
python -B -m evaluation.v2_eval --responses <responses.jsonl> --output-dir <new-output-dir>
```

`responses.jsonl` 每行 `{case_id,response}`。输出 `v2-summary.json`，含 pass/fail/逐题判据；无响应/不全时返回非零。当前仅有合成响应测试，真实 A 响应尚未到。

## 边界

- 不覆盖任意 SQL、自动拆分同一行多事件、OCR/语音、无确认写库、自由跨库汇总。
- 会话目标只是规格；未实现/未响应前不能声称 v2 已完成。
- 公开说明不写最终 manifest SHA，避免哈希循环。
