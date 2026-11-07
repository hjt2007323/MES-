# A07 MES 制造数据助手

面向制造业务的中文数据助手，支持受控数据问答与经人工确认的停机记录录入，并提供查询依据和录入查回结果。

## 范围

**受控数据问答（默认离线 mock，可选本机 real）**

- 自然语言先规划为 `query` / `clarify` / `refuse`，查询只走既有命名查询与参数化 SQL；
- 公开演示默认 mock，不联网、不访问数据库、不需要 key；显式 `real` 才读取用户本机配置，
  配置/数据库不可用时明确报错，不回退 mock；
- 只覆盖 `line_snapshot`、`work_order_progress`、`downtime_details` 三个命名查询；
- 默认 mock 的表、SQL 与查询计数是离线模拟，不是真实业务结果；real 的执行来源另行标注。

**统一工作台（v2）**

- 同一页面入口“统一工作台（v2）”：自然语言请求经程序校验后路由为
  `history_query`（旧三场景，受控只读）、`intake_query`（B `search_window`，只读）、
  `prepare_intake`（现有提取服务，只生成草稿）或 `clarify/refuse`；
- 录入后可按当前会话已确认的 `record_id` 或固定窗口 + 设备查回；跨源结果分来源展示，不混算；
- 多行文本按非空行生成最多 10 条草稿队列，逐条明确确认；澄清可补原文重新提取；
- 历史与录入数据源分离：历史仍走受控 MySQL/mock，录入读独立 SQLite 沙箱；不开放任意 SQL。

**停机录入（合成沙箱）**

- 单条已结束事件 → 离线模板/模型生成五字段草稿 → 原文证据 → 人工编辑复核 →
  显式确认写入 SQLite → 按时间窗查回；
- 默认 `offline`，不需要 key、不联网；只有显式选择 `real` 并点击“生成草稿”时才使用
  用户本机的硅基流动配置；
- 问答与录入使用不同数据源：历史问答默认 mock，可按本机配置访问受限 MySQL；录入写入独立 SQLite 沙箱
  `runtime/a07-intake/downtime.sqlite3`（相对候选根，已被 `.gitignore` 忽略）；
- 录入幂等：同一次确认重试返回同一 `record_id`，冲突不覆盖。

## 目录与职责

```text
projects/A-mes-assistant/
  app.py                     Streamlit：自然语言问答 + 命名查询 + 停机录入 + 统一工作台（v2）
  mes_assistant/
    question_runtime.py      问答统一入口与预算/校验
    query_planner.py         B：query/clarify/refuse 规划与提示
    semantics.py             B：固定语义目录与 full/focused 上下文
    llm_client.py            B：模型客户端（固定 provider/端点）
    intake_ui.py             A：录入页面、草稿状态与确认守卫
    intake_runtime.py        A：offline/real 统一入口
    intake_batch.py          A：12 题批测 CLI（dry-run/execute）
    intake_extraction.py     A：模型/模板草稿与原文证据
    intake_store.py          B：SQLite 沙箱、幂等事务、窗内查回与只读 search_window
    workspace_assistant.py   A：v2 路由/会话/队列服务
    workspace_ui.py          A：v2 同一工作台页面
    workspace_cli.py         A：v2 离线 CLI
  tests/                     离线回归（问答/录入/存储/AppTest）
  fixtures/                  合成请求与脚本
projects/C-eval-toolkit/     独立离线判定与 M5 12 题 live 回放
docs/demo.md                 三分钟演示
docs/architecture.md         职责架构
docs/source-and-db.md        数据来源与数据库边界
.github/workflows/offline.yml 离线 CI（Windows + Python 3.14）
```

## 快速开始（Windows PowerShell）

从候选根执行：

```text
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r projects\A-mes-assistant\requirements-ui.lock.txt

cd projects\A-mes-assistant
..\..\.venv\Scripts\python.exe -B -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true
```

打开 `http://127.0.0.1:8501`：顶部可选“自然语言问答”“命名查询”“停机录入（合成沙箱）”或“统一工作台（v2）”。
页面只绑定 `127.0.0.1`，结束后停止进程。

CLI mock 问答（工作目录 `projects/A-mes-assistant`）：

```text
..\..\.venv\Scripts\python.exe -B -m mes_assistant.question_cli --request fixtures/m2/request.json --mode mock --script fixtures/m2/scripts/success.json --log ..\..\.test-tmp\cli-success.jsonl
```

## 数据与真实边界

- 公开问答与录入演示均使用**合成数据**，带 `synthetic`/`review_status=pending` 标识；
- 本候选不携带 MySQL 分发件、运行库、凭据或内部实验日志；
- 历史真实问答/数据库测试属于既有实测证据，不由本候选复现；
- 工作台历史查询的 `real` 使用模型计划与受控命名查询；录入 `real` 使用模型生成草稿，
  SQLite 写入始终需要明确人工确认。历史 MySQL 链路本轮未复验；
- 首次 12 题真实批次已执行：严格整题 9/12，错误 ready 1/6 已发现；原批 12 次授权已用完。
- 002 真实批（同一 12 题、新预算）已完成：严格 11/12、错误 ready 0/6；i07 仍 issue_mismatch，原始结果保留，不重跑刷分。
- 统一工作台 v2：程序与 SQLite 路由规格 12/12，模型调用 0；r2 回执报告提取/会话
  共 14 次模型尝试、6445 已记录 token。i07 超时及队列一项 error 保留；预期的澄清/拒绝
  单独统计。由于模型来源标签有误、缺逐题草稿字段/证据及会话响应，真实模型的字段与
  会话质量为 **unverified（未认证）**，不能以状态分数替代。独立离线测试 SQLite 的
  确认/重放/查回为 1 行，与真实模型批次分开。失败运行和超时的未知用量未计入上述 token。

## 测试

```text
cd projects/A-mes-assistant
..\..\.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_question*.py" -v
..\..\.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_ui*.py" -v
..\..\.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_intake*.py" -v
..\..\.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_workspace*.py" -v

cd ..\C-eval-toolkit
..\..\.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_offline.py" -v
..\..\.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_intake_live_eval.py" -v
..\..\.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_v2_eval.py" -v
..\..\.venv\Scripts\python.exe -B -m evaluation.intake_live_eval --export-inputs --inputs ..\..\.test-tmp\live-inputs.json
..\..\.venv\Scripts\python.exe -B tools\check_intake_public_cli.py

cd ..\A-mes-assistant
..\..\.venv\Scripts\python.exe -B -m mes_assistant.intake_batch --dry-run --inputs ..\..\.test-tmp\live-inputs.json --budget fixtures\intake\budget-template.json
```

全部离线，使用替身与独立临时 SQLite，零外部模型请求。预算样例
`fixtures/intake/budget-template.json` 仅为 `pending_user_approval` 的 dry-run 样例，
不能替代真实批次审批。CI 配置见 `.github/workflows/offline.yml`。

## 演示与架构

- [三分钟演示](docs/demo.md)
- [职责架构](docs/architecture.md)
- 数据来源与数据库边界见 [source-and-db.md](docs/source-and-db.md)

## 验收范围

本版通过离线工程与基本浏览器演示验收，公开自动检查配置已包含录入和工作台回归。
GitHub 托管检查尚未执行；真实模型字段/会话质量、真人视觉、Python 3.12 与本轮真实
MySQL 链路均未验证。旧实验原始数据保持冻结，本版不追加付费批次。

## 许可

MIT，见 [LICENSE](LICENSE)。合成数据与文档仅用于演示，不代表企业真实数据或业务准确率。
