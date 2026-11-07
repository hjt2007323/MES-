# M5 真实录入与固定批测

## 统一入口

页面与批测共用：

```text
mes_assistant.intake_runtime.run_intake_request(
    text, *, reference_time, mode="offline", model_client=None, env=None
) -> {"draft": ..., "run": ...}
```

- `mode` 只接受 `offline` / `real`，缺省 offline；内部始终调用正式
  `intake_extraction.extract_draft`。
- offline 使用模板替身；real 使用 `ModelConfig.from_env(env, provider="siliconflow")`
  和 `ChatModelClient`，一次 complete、10 秒、最多 512 输出 token。
- `run` 包含 mode、execution_source（offline_template/http_model/test_double）、
  provider、model、client_calls、elapsed_ms、usage、error_code。
- 注入替身标 `test_double`；配置错误零调用并返回固定错误码；不输出 key、
  Authorization 或底层异常原文。
- 页面只有“生成草稿”显式触发；首次打开、普通 rerun、模式切换、校验、确认、
  查回均不派发模型。人工确认是唯一写入动作。

## 真实配置

- 只从进程环境读取 `SILICONFLOW_API_KEY`；可选 `MES_LLM_MODEL`、固定的
  `SILICONFLOW_BASE_URL`。provider 固定 siliconflow。
- 不把 key 写入文件、命令参数、日志或页面；配置缺失显示 `CONFIG_MISSING`。
- 002固定12题已有经独立评分的结果；v2工作台真实字段/会话质量仍未认证，不能用
  离线模板或状态分数替代。

## 固定 12 题批测

批测只接受由批准预算绑定的输入与预算文件（路径由调用方提供，不入候选）：

```text
..\..\.venv\Scripts\python.exe -B -m mes_assistant.intake_batch --dry-run ^
  --inputs <approved-live-inputs.json> ^
  --budget <approved-budget.json>
```

- 缺省 dry-run：核对 12 个 case_id、输入 SHA、预算，列出计划；零网络、零库写、
  不创建结果目录。
- `--execute` 必须先满足预算 `status=approved`、input SHA 绑定、run/config/provider/
  model/题号/上限匹配；否则固定错误退出。执行前用预算 provider/model 核对实际
  客户端配置，型号不符固定报错且零派发；manifest 使用已核对配置。
- 真实结果目录由批准预算绑定的固定运行目录决定；绝对路径或临时改目录拒绝。
- 互斥锁覆盖预检到归档；reserved 先落盘，收到输出后保存结果再标 observed；
  已有 reserved/observed 禁止补发，重入/并发零重复派发。中断、部分结果或账本
  写入失败保留已有预约/结果，当前批次标 incomplete；已完成批次重入不改写
  manifest/ledger/results。
- `CONFIG_MISSING/HTTP_ERROR/NETWORK_ERROR/BUDGET_INVALID/INPUT_BINDING_FAILED`
  停止余下实测；超时/坏模型输出作为该题失败保留。
- 批测只生成草稿，零 commit；人工确认写入另测。
- 001 首次 12 题真实批次已完成（严格整题 9/12、错误 ready 1/6）；002 新预算批次也已完成
  （严格 11/12、错误 ready 0/6）。两者原始结果均保留，不重跑刷分。
- 统一工作台 v2 已有一次真实记录（14 次调用、6445 token），但来源标签/字段证据未通过 C
  认证，统一判 unverified；本轮不追加付费批次。

## 测试

```text
..\..\.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_intake*.py" -v
```

使用离线替身、临时 SQLite 和隔离目录；不调用真实模型或 MySQL。
