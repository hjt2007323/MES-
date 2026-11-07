# m5-pilot-002 真实小测摘要

- 状态：真实批次完成；**质量 changes_requested（i07 issue mismatch）**，安全门槛通过。
- run_id：`m5-pilot-002`；固定 12 题，dataset/input 同 001；父预算 S1、002 子预算机械绑定。
- 实际：12 条系统观察、**11 次 HTTP/模型调用**、i09 程序前置拦截 0 调用、0 commit、0 重试。
- usage：prompt 4615 / completion 1080 / total **5695** token。
- 严格整题：**11/12**；主状态：**12/12**；安全门槛：**error ready 0/6，通过**。
- 完整记录 6/6、字段/证据 30/30。
- 认证：parent/child budget、manifest/ledger/results SHA、source-map 与实际源码一致；**certified**。

## Badcase

- i07：期望 `MISSING_END_TIME` 单一问题，实际同时返回 `MISSING_END_TIME` + `MISSING_REASON`，判 `issue_mismatch`；主状态仍为 needs_clarification。
- 原 001 的 i07 也是 issue mismatch，但 002 错误 ready 已从 1/6 降为 **0/6**；不修改 001/002 原始结果或题库。

## 边界

- i09 是程序前置 guard，不是模型调用；不把 12 条都说成“模型实测 12 次”。
- 002 是录入提取器实测，不等于统一工作台 v2 真实路由/查询质量；v2 真实批次另验。
- 旧 001 严格 9/12、错误 ready 1/6 保留；001/002 原始文件、预算、gold/judge 只读。
