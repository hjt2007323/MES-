# A07 v2 统一工作台

## 入口

Streamlit 入口新增 **统一工作台（v2）**；原问答、命名查询、停机录入入口保留。

## 路由

同一请求按业务词路由：

- `history_query`：offline 使用受控替身；real 必须由模型输出严格 JSON 计划，
  再调用真实命名查询；模型/配置/数据库不可用明确 error，**不静默回退 mock**；
- `intake_query`：按已确认 record_id 或“今天/本周/明确日期 + 可选设备”调用 B
  `search_window`，来源 `intake_sqlite`；
- `prepare_intake`：调用现有离线/real 提取服务，只生成草稿，不写库；
- 无法确定时间窗、设备不唯一、未知设备或“刚录入”没有已确认记录时返回
  `needs_clarification`。

页面/CLI/服务共用 `WorkspaceService`。历史与新增 SQLite 结果不默默相加；
“刚录入”只接受当前会话已确认的 record_id，不能查询草稿。未知设备（如 M99）
不会被丢成无过滤条件。

## 草稿/确认守卫

- 服务按 draft_id 注册本会话草稿；确认只接受注册草稿，不能伪造 `state=ready`。
- 原文变化后旧 ready 失效：服务比较注册原文与确认时的原文，不匹配返回
  `STALE_DRAFT`；确认前重新校验字段和单事件护栏。
- 页面保留服务/会话状态；补充轮次最多3轮，累积追加原文，不覆盖旧证据。
- 多行文本每个非空行一条，最多10条；每行独立 draft_id/状态/证据/提交键。
  一行多事件仍使用单事件护栏，不自动拆分；逐条明确确认，部分失败不丢其他草稿。

## 限制

- v2程序/SQLite路由规格12/12，0次模型；r2回执报告提取/会话14次模型尝试、
  6445已记录token。i07超时及队列一项error保留，预期澄清/拒绝另列。
- 来源标签误标，且未保存完整逐题草稿字段/证据与会话响应；真实字段/会话质量
  unverified。普通状态匹配不等于字段或模型质量认证，旧记录不补造、不付费重跑。
- 历史查询仍受旧 M2 三场景和只读 MySQL 限制；不开放任意 SQL。
- 新 SQLite 缺库时明确报未初始化，不自动创建。
- 浏览器演示为 agent tab + 隔离测试库；人工可见浏览器、Python 3.12、
  hosted CI 与真实 MySQL 未验证。

## 测试

```text
.venv-ui\Scripts\python.exe -B -m unittest discover -s tests -p "test_workspace*.py" -v
```

使用临时 SQLite 和离线提取；不调用真实模型/MySQL。
