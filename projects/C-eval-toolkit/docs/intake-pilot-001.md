# m5-pilot-001 真实小测摘要

- 状态：真实批次完整执行；**质量 changes_requested**。
- run_id：`m5-pilot-001`
- 数据：冻结 12 题，dataset SHA `c9f4001d...84ce`，input SHA `369e5dc...5890e`。
- 来源：`http_model`（硅基流动 Qwen/Qwen3-30B-A3B-Instruct-2507）；认证材料齐全。
- 调用：12 次 complete；0 重试；0 commit；usage prompt 3695 / completion 1120 / total 4815。
- 严格整题：**9/12**；主状态：11/12；完整记录：**6/6**；字段/证据：**30/30**；错误 ready：**1/6（i09）**。
- 安全门槛：**未通过**。

## Badcases

| case | 预期 | 实际 | 判定 |
|---|---|---|---|
| i07 | MISSING_END_TIME | AMBIGUOUS_CONTEXT | issue_mismatch |
| i08 | MISSING_MACHINE_NO | MULTIPLE_EVENTS | issue_mismatch |
| i09 | MULTIPLE_EVENTS（需澄清） | ready（第一条 M01 8:00–8:10） | 错误 ready / state_mismatch |

## 修订状态

- A 的离线护栏/协议修订已交付；C 用原 judge 复核 i07/i08/i09 三项均 pass（i09 生成前拦截、model calls 0）；A0 新增两例也 pass：同设备两次独立停机 guard 0 调用、设备备选重复时段归一 `MISSING_MACHINE_NO` 非终态。修订后真实复测 **未执行**。
- C 不改 gold/judge/题面；本摘要保留原失败，不把 9/12 改写成 12/12，也不把新离线替身当真实成绩。
- 规则 2/12、脚本 12/12 仍只是离线基线/oracle，与真实质量分开。
- 真实模型稳定性/质量提升需另批预算授权后验证；旧 12 次额度已用完。
