# A07 MES 受控数据问答助手

这是一个面向制造业务的中文数据问答助手。用户在 Streamlit 或 CLI 提问，系统返回查询计划、结果表和业务口径。
GitHub 首版使用合成数据和离线模拟，演示 `query / clarify / refuse` 三种受控动作及三个命名查询。

## 演示范围

- 自然语言问题由固定语义目录/planner 转成受控动作；
- 只允许 `line_snapshot`、`work_order_progress`、`downtime_details` 三个命名查询；
- 缺产线/时间窗等问题澄清；目录外或写操作拒绝；
- 公开页面和 CLI 都只提供 **mock**，不联网、不访问 DB、不需要 key；
- 表、SQL、查询计数都是**离线模拟**，不是真实查询或业务结果；
- 数据始终标为合成，`review_status=pending`。

公开 mock 的冻结样例：

- `line_snapshot`：L01/L02/L03 三行；
- `work_order_progress`：只覆盖 `line_id=1`；`line_id=2` 或其他值返回范围澄清；
- `downtime_details`：只覆盖 `2026-11-01T00:00:00+08:00` 到
  `2026-11-02T00:00:00+08:00`；其他窗口返回范围澄清。

## 目录

```text
projects/A-mes-assistant/
  app.py                         单一自然语言问答入口（离线 mock）
  mes_assistant/                 A 编排、语义/planner、受控查询参数与模拟 DTO
  tests/                         离线回归（AppTest/CLI/运行时）
  fixtures/m1, fixtures/m2       合成请求与脚本
  requirements-ui.lock.txt       锁定依赖
docs/demo.md                     三分钟演示脚本
.github/workflows/offline.yml    离线 CI（Windows + Python 3.14）
docs/source-and-db.md            B 的外部 DB/来源边界说明
projects/C-eval-toolkit          C 的独立离线判定与公开摘要
```

## 快速运行（Windows PowerShell）

从仓库根执行：

```text
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r projects\A-mes-assistant\requirements-ui.lock.txt
```

CLI mock（工作目录 `projects/A-mes-assistant`）：

```text
..\..\.venv\Scripts\python.exe -B -m mes_assistant.question_cli --request fixtures/m2/request.json --mode mock --script fixtures/m2/scripts/success.json --log ..\..\.test-tmp\cli-success.jsonl
..\..\.venv\Scripts\python.exe -B -m mes_assistant.question_cli --request fixtures/m2/request.json --mode mock --script fixtures/m2/scripts/clarify.json --log ..\..\.test-tmp\cli-clarify.jsonl
..\..\.venv\Scripts\python.exe -B -m mes_assistant.question_cli --request fixtures/m2/request.json --mode mock --script fixtures/m2/scripts/refuse.json --log ..\..\.test-tmp\cli-refuse.jsonl
```

`--mode real` 会在读 key/派发模型前以固定 `REAL_UNAVAILABLE` 退出；本仓库的演示入口使用 mock。

页面：

```text
..\..\.venv\Scripts\python.exe -B -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true
```

打开 `http://127.0.0.1:8501`，保持离线 mock，提交示例问题。页面只绑定
127.0.0.1；结束后停止进程。

回归（工作目录 `projects/A-mes-assistant`）：

```text
..\..\.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_question*.py" -v
..\..\.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_ui*.py" -v
```

C 离线判定（工作目录 `projects/C-eval-toolkit`）：

```text
python -B -m unittest discover -s tests -v
python -B -m ceval.cli --output-dir ../../.test-tmp/c-offline
```

## 环境与边界

- 本地验证环境：**Windows 11 + Python 3.14.6 + Streamlit 1.66.1**，锁定包从官方
  PyPI 安装。CI 目标对齐 Windows + Python 3.14。
- **Python 3.12 与 Ubuntu 未在本轮验证**；不声称跨版本/跨平台已通过。
- 生成 `.venv`、`.test-tmp`、`runtime`、日志、`.env` 和生成报告由 Git 忽略。
- 公开入口不包含真实模型、真实 DB、real 模式或独立命名查询；B 的外部 DB 安装与
  未验证限制见 [source-and-db.md](docs/source-and-db.md)。
- C 的离线判定和历史公开摘要见 [C-eval-toolkit](projects/C-eval-toolkit/README.md)、
  [public-summary.md](projects/C-eval-toolkit/docs/public-summary.md)。
- 历史 12 题 pilot 是 **已归档的合成实测摘要**，与本仓库的模拟演示分开；12/12 只是 pilot
  工程匹配，不是 30 题企业准确率；其余 18 题未跑。
- 本仓库采用 [MIT 许可证](LICENSE)。第三方依赖及数据库来源见 [依赖与数据库边界](docs/source-and-db.md)。

## 文档

- [三分钟演示脚本](docs/demo.md)
- [B 来源/DB 边界](docs/source-and-db.md)
- [C 公开摘要](projects/C-eval-toolkit/docs/public-summary.md)
- [应用模块说明](projects/A-mes-assistant/README.md)
