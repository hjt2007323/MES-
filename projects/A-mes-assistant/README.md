# A07 MES 制造数据助手

面向制造业务的中文数据助手，支持受控数据问答与经人工确认的停机记录录入，
并提供查询依据和录入查回结果。

## 当前范围

- **受控问答**：自然语言先规划为 `query` / `clarify` / `refuse`，查询只走既有
  命名查询和参数化 SQL；不开放任意 SQL、自由 Text2SQL 或写入。
- **停机录入**：单条已结束事件 → 模型/离线模板生成五字段草稿 → 原文证据校验 →
  人工编辑复核 → 显式确认写入 SQLite → 按时间窗查回。
- 问答与录入使用**不同数据源**：问答沿用原 MySQL 合成数据；录入使用独立的
  `runtime/a07-intake/downtime.sqlite3` 沙箱；统一工作台（v2）可按已确认
  record_id 或固定窗口查回新录入数据，跨源结果分来源展示。
- 录入默认离线模板；显式选择 real 才使用本机硅基流动配置。
- 2026-11-06 首次真实 12/12 调用：严格 9/12、错误 ready 1/6。
  2026-11-07 第二批 m5-pilot-002：12 条观察、11 次 HTTP + 1 程序 guard，
  严格 11/12、错误 ready 0/6、5695 token；i07 仍为 issue_mismatch，原样保留。
- v2 程序/SQLite 路由规格 12/12，0 次模型调用；r2 回执报告提取/会话共14次模型尝试、
  6445已记录token，i07超时及队列一项error保留。执行来源标签有误，缺逐题草稿字段/
  证据及会话响应，因此真实模型字段/会话质量 **unverified（未认证）**；不以状态分数
  代替字段准确率。独立离线确认/重放/查回1行，不能当作真实模型写库。

## 环境

- 目标 Python 3.12；本机实际验证 **Python 3.14.6** + Streamlit 1.66.1。
- UI 环境：`.venv-ui`；依赖锁定见 `requirements-ui.lock.txt`。
- 工作目录：`projects/A-mes-assistant`；统一使用 `-B` 禁止 `__pycache__`。

## 页面

```text
.venv-ui\Scripts\python.exe -B -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true
```

入口：

1. **自然语言问答**：默认 mock；real 才调用模型，查询仍走既有参数化目录。
2. **命名查询**：保留旧三场景入口。
3. **停机录入（合成沙箱）**：默认 offline；可切换 real。首次打开、普通 rerun、
   模式切换、校验和查回均不派发模型请求，只有“生成草稿”显式调用。
4. **统一工作台（v2）**：同一入口路由历史受控问答、已确认录入查询和停机草稿；
   多行文本生成逐条草稿队列。历史与 SQLite 来源分列，写入仍需逐条确认。
   [工作台说明](docs/workspace-v2.md)

录入页显示运行模式、`execution_source`、provider/model、实际 complete 次数和
usage；切换模式/原文/reference_time 会使旧草稿失去确认资格。

## 停机录入

- 预置独立 fixture 与文档：[录入演示](docs/intake-demo.md)、
  [真实模式与批测](docs/intake-live.md)。
- 数据表：`runtime/a07-intake/downtime.sqlite3`；固定参数化 INSERT、同草稿键
  幂等重放、冲突和半开时间窗查回。
- 真实模型配置：只从进程环境读取 `SILICONFLOW_API_KEY`，provider 固定
  siliconflow；一次 complete、10 秒、最多 512 输出 token。配置缺失零调用。
- 真实批测默认 dry-run；002 固定 12 题、最多 12 次调用、零提交。执行必须预算
  approved 且输入/预算 SHA 匹配；执行前核对预算 provider/model 与实际配置，
  型号不符零派发。002 结果目录为
  `execution/A/A-012/experiment/m5-pilot-002/`；001 已耗尽，不重入。

## 离线测试

```text
.venv-ui\Scripts\python.exe -B -m unittest discover -s tests -p "test_intake*.py" -v
.venv-ui\Scripts\python.exe -B -m unittest discover -s tests -p "test_workspace*.py" -v
.venv-ui\Scripts\python.exe -B -m unittest discover -s tests -p "test_question*.py" -v
.venv-ui\Scripts\python.exe -B -m unittest discover -s tests -p "test_ui*.py" -v
```

覆盖提取严格 JSON/引用唯一性、原文单事件护栏、i07/i08 缺项澄清码、页面确认守卫、
空值重复字段、伪证据、原文/参考时间/模式切换、SQLite 幂等/冲突/硬链接边界、
统一 runtime 和批测 dry-run/锁/账本/源码指纹。

## 数据源与边界

- 问答历史真实试验使用合成夹具和只读 MySQL；不是企业真实数据，也不代表一般准确率。
- 录入为单条合成停机事件；不计算停机分钟/新指标，不做多事件拆分、语音或图片。
- 不声称通用 Text2SQL、向量库或无人确认写入；人工确认是唯一写入动作。
- Python 3.12/Ubuntu、浏览器人工视觉、真实模型质量和公开发布均需对应环节验证。
- 不写凭据、原始日志、运行数据库、虚拟环境或内部协作文件到公开候选。
