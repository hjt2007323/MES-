# 受控问答流程

```text
中文问题
  -> B semantics：固定口径/别名/上下文选择
  -> B planner：只产生 query | clarify | refuse
  -> A run_question：预算、30s deadline、一次纠错、计数/usage
  -> query 只能走 M1 三个命名查询（只读工具）
  -> A 结果格式化：表/SQL/解释/状态/trace；页面或 CLI 展示
```

- `mock`：只替换模型、工具和时钟；B planner/semantics 与真实路径相同。
- `clarify/refuse`：零 SQL；`query` 的 SQL 来自命名目录，不来自模型自由生成。
- 单问预算：30 秒、模型 2 次、SQL 2 条、纠错 1 次；模型单次 10 秒/512 token。
- 结果包含 `semantic_policy_version`、`context_mode`、`context_evidence`、最终
  `plan`；页面分别显示“演示口径已确认”和“合成数据”。
- 计数：未派发问题 `queries=0`；只有工具已调用但未知时才 null。

本候选用合成 fixtures 跑离线成功/澄清/拒绝；真实模型/DB 不在此入口。
