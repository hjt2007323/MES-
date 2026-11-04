# 来源、依赖与数据库边界

> GitHub 首版以合成数据和离线模拟演示受控问答。历史真实测试使用合成夹具与只读 MySQL，聚合结果见 [实测摘要](../projects/C-eval-toolkit/docs/public-summary.md)。

## 1. 首版文件（54 个）

- 根：`README.md`、`LICENSE`、`.gitignore`、`.github/workflows/offline.yml`、`docs/demo.md`、
  `docs/source-and-db.md`
- A 子项目：`projects/A-mes-assistant/app.py`、`mes_assistant/`（14 个模块）、
  `tests/`（4 个离线测试）、`fixtures/`（12 个合成请求/脚本）、`docs/`（3 个设计文档）、
  `README.md`、两版 requirements
- C 子项目：`projects/C-eval-toolkit/` 的 `ceval/`（4）、`data/`（2）、`docs/`（2）、
  `tests/`（1）、`README.md`、`.gitignore`

完整相对路径列表见附录。

## 2. 来源与许可

| 内容 | 来源 | 许可状态 |
|---|---|---|
| A 应用/编排/UI（`app.py`、`mes_assistant/`） | 本项目自有 | [MIT](../LICENSE) |
| B 语义、规划器、模型客户端（`semantics.py`、`query_planner.py`、`llm_client.py`） | 本项目自有，仅标准库 | 同上；不再分发第三方代码 |
| C 离线判定/回放（`projects/C-eval-toolkit/`） | 本项目自有 | 同上 |
| `fixtures/`、C 的 `data/` 与公开摘要 | 本项目合成数据 | 合成示例随仓库提供 |
| Streamlit 1.66.1 与 33 个传递依赖 | 官方 PyPI，按 `requirements-ui.lock.txt` 固定版本 | 第三方许可，逐包记录见 §2.1；通过包管理器安装 |
| 五表 schema（派生自内部原型） | 内部原型子集 | **不在本离线版本内**；如后续加入，需单独确认派生内容与再许可 |
| MySQL Community 8.0.28 | Oracle 分发，GPLv2 | **作为外部依赖**；历史测试只使用外部只读 MySQL |

第三方依赖通过锁定版本安装，并各自遵循其许可证。

### 2.1 锁定依赖许可证（34 个包，版本与 lock 一致）

按安装元数据核对；无缺失许可证元数据。

| 元数据许可证 | 包数 | 待确认点 |
|---|---|---|
| Apache-2.0（含 “Apache License 2.0”） | 7 | — |
| Apache-2.0 OR BSD-2-Clause | 1 | `packaging`：分发时选其一 |
| BSD-3-Clause | 9 | — |
| BSD 分类器（条款未细化） | 2 | `itsdangerous`、`Jinja2`：确认 3 条款 |
| BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | 1 | `numpy`：复合许可，保留声明 |
| MIT | 10 | — |
| MIT-CMU | 1 | `pillow` |
| MPL-2.0 | 1 | `certifi` |
| PSF-2.0 | 1 | `typing_extensions` |
| 版权文字（Vega-Altair Developers） | 1 | `altair`：确认具体许可证文本 |

已验证的运行环境：**Windows 11 + Python 3.14.6 实际验证**；Python 3.12、
Ubuntu 与跨平台安装未验证。
安装只从官方 PyPI 按 lock 执行；离线 CI 不安装/启动数据库。

## 3. 数据库边界与限制

- 本仓库**不包含**数据库客户端、schema、初始化工具、MySQL 二进制、数据目录或凭据；
  离线页面/CLI 的 `mock` 模式不需要 API key，也不需要数据库。
- 历史真实测试曾使用**合成夹具 + 只读 MySQL**；这些结果属于历史证据，以摘要形式提供，
  **不能由本仓库复现**，也不构成企业数据或业务准确率。
- 新机器上的独立数据库初始化是**内部尚未实现、未验证的计划**，不属于本仓库范围。
  已知限制：需要外部安装 MySQL（GPLv2）；旧工具链含 Windows 专有路径与权限操作；
  端口/实例冲突需要人工处理；Python 3.12 未验证；因此**不得宣称一键复现真实数据库**。
- 若后续扩展真实数据库入口，应单独发卡，并提供外部依赖说明、隔离路径、只读账号与清理证据。

## 4. 验证方式

- 离线入口按根 `README.md` 的 Python 3.14.6 环境与 lock 安装步骤运行；
  回归覆盖 CLI、运行时与页面离线测试，不需要 key 或数据库。
- 本文件中的依赖版本来自 `requirements-ui.lock.txt`；许可信息来自安装元数据，
  不一致或缺失项会标为待确认（本次 34/34 版本一致，0 缺失）。
- 已用 Chrome 跑通正常查询、澄清和拒绝三种离线演示。

## 附录：首版文件相对路径

```text
.github/workflows/offline.yml
.gitignore
LICENSE
README.md
docs/demo.md
docs/source-and-db.md
projects/A-mes-assistant/README.md
projects/A-mes-assistant/app.py
projects/A-mes-assistant/requirements-ui.txt
projects/A-mes-assistant/requirements-ui.lock.txt
projects/A-mes-assistant/mes_assistant/__init__.py
projects/A-mes-assistant/mes_assistant/fakes.py
projects/A-mes-assistant/mes_assistant/llm_client.py
projects/A-mes-assistant/mes_assistant/model_factory.py
projects/A-mes-assistant/mes_assistant/query_catalog.py
projects/A-mes-assistant/mes_assistant/query_planner.py
projects/A-mes-assistant/mes_assistant/query_runtime.py
projects/A-mes-assistant/mes_assistant/question_cli.py
projects/A-mes-assistant/mes_assistant/question_fakes.py
projects/A-mes-assistant/mes_assistant/question_runtime.py
projects/A-mes-assistant/mes_assistant/semantics.py
projects/A-mes-assistant/mes_assistant/service.py
projects/A-mes-assistant/mes_assistant/structured_log.py
projects/A-mes-assistant/mes_assistant/ui_presenter.py
projects/A-mes-assistant/docs/metrics.md
projects/A-mes-assistant/docs/question-flow.md
projects/A-mes-assistant/docs/ui.md
projects/A-mes-assistant/fixtures/m1/completion-params.json
projects/A-mes-assistant/fixtures/m1/downtime-params.json
projects/A-mes-assistant/fixtures/m1/request.json
projects/A-mes-assistant/fixtures/m1/snapshot-params.json
projects/A-mes-assistant/fixtures/m2/request.json
projects/A-mes-assistant/fixtures/m2/request-budget.json
projects/A-mes-assistant/fixtures/m2/request-timeout.json
projects/A-mes-assistant/fixtures/m2/scripts/clarify.json
projects/A-mes-assistant/fixtures/m2/scripts/correction.json
projects/A-mes-assistant/fixtures/m2/scripts/refuse.json
projects/A-mes-assistant/fixtures/m2/scripts/success.json
projects/A-mes-assistant/fixtures/m2/scripts/timeout.json
projects/A-mes-assistant/tests/test_question_app.py
projects/A-mes-assistant/tests/test_question_cli.py
projects/A-mes-assistant/tests/test_question_runtime.py
projects/A-mes-assistant/tests/test_ui_presenter.py
projects/C-eval-toolkit/.gitignore
projects/C-eval-toolkit/README.md
projects/C-eval-toolkit/ceval/__init__.py
projects/C-eval-toolkit/ceval/cli.py
projects/C-eval-toolkit/ceval/compare.py
projects/C-eval-toolkit/ceval/quality.py
projects/C-eval-toolkit/data/cases.json
projects/C-eval-toolkit/data/results.json
projects/C-eval-toolkit/docs/public-summary.json
projects/C-eval-toolkit/docs/public-summary.md
projects/C-eval-toolkit/tests/test_offline.py
```
