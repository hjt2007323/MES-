# 职责架构

A07 把“受控问答”和“停机录入”放在同一页面入口下，但数据源、写入路径和验收边界分开。

## 模块与职责

| 模块 | 职责 | 数据/副作用 |
|---|---|---|
| `app.py` | Streamlit 入口：自然语言问答、停机录入、命名查询三场景 | 只读页面；录入确认才写 SQLite |
| `mes_assistant/question_runtime.py` | 问答统一入口、预算、计划校验与结果整形 | mock 无网络；real 需显式配置 |
| `mes_assistant/query_planner.py` | 把中文问题规划为 `query/clarify/refuse`，构造提示与解析动作 | 无 SQL、无工具 |
| `mes_assistant/semantics.py` | 固定指标口径、七条边界、产线别名、full/focused 上下文 | 纯数据 |
| `mes_assistant/query_catalog.py` | 三个命名查询的列定义与业务说明 | 纯数据 |
| `mes_assistant/llm_client.py` | 固定 provider/端点的模型客户端、预算与 usage 观测 | 仅用户显式执行 real 请求时调用 |
| `mes_assistant/intake_extraction.py` | 原文 → 五字段草稿 + 字段证据；离线模板/模型输出校验 | 不写库 |
| `mes_assistant/intake_runtime.py` | 录入统一入口 `run_intake_request`（offline/real） | 不写库；返回 draft/run |
| `mes_assistant/intake_ui.py` | 页面状态机：生成、编辑、重校验、确认守卫、查回 | 确认后调用 store |
| `mes_assistant/intake_batch.py` | 12 题 CLI：dry-run/execute、预算/输入绑定、resume | 只生成草稿，零 commit |
| `mes_assistant/intake_store.py` | SQLite 沙箱：记录校验、幂等提交、窗内查回 | 唯一写入 SQLite |
| `projects/C-eval-toolkit/evaluation/` | 独立离线判定、live 回放、公开摘要 | 只读结果文件/临时库 |

## 两条数据流

**问答**

```text
中文问题 → planner（query/clarify/refuse）→ 命名查询参数校验
        → mock 结果或既有只读 SQL → 页面表格/口径/边界
```

**停机录入**

```text
原文 + reference_time → 草稿（模板或模型）→ 字段级原文证据
→ 人工编辑/重校验 → 显式确认 → DowntimeStore.commit（幂等）
→ record_id/replayed → 显式时间窗 list_window 查回
```

## 边界

- 问答不开放任意 SQL，不自动启停数据库；录入是唯一人工确认后的写入动作。
- 问答与录入数据源不同；统一工作台已可通过 search_window 按当前会话已确认的
  record_id 或明确时间窗/设备查询录入数据，历史 MySQL 与新增 SQLite 分来源展示。
- 录入只收单条已结束事件；多事件、未结束、未知设备/工单走澄清或拒绝。
- 模型请求只在用户显式执行 real 查询或生成草稿时派发；普通 rerun、确认和 SQLite
  查回不派发模型。批测执行另需批准预算；当前 v2 真实字段/会话质量未认证。
- 公开候选不含 MySQL 二进制、运行库、密钥、原始实验日志或内部协作文件。
