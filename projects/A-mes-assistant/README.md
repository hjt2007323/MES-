# A07 MES assistant（离线候选子项目）

候选根 `projects/A-mes-assistant` 的单一离线入口。Streamlit 只提供自然语言问答；
模型只产生 `query/clarify/refuse`，查询只走 `line_snapshot`、
`work_order_progress`、`downtime_details` 三个命名查询的演示参数。

公开 mock 冻结样例：line_snapshot 三行；work_order_progress 仅 line_id=1；
downtime_details 仅 `2026-11-01T00:00:00+08:00..2026-11-02T00:00:00+08:00`。
样例外参数返回范围澄清。表、SQL、计数都是离线模拟，不是真实查询结果。

## 运行

工作目录为本目录：

```text
..\..\.venv\Scripts\python.exe -B -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true
```

CLI mock：

```text
..\..\.venv\Scripts\python.exe -B -m mes_assistant.question_cli --request fixtures/m2/request.json --mode mock --script fixtures/m2/scripts/success.json --log ..\..\.test-tmp\cli-success.jsonl
```

`--mode real`、独立命名查询和真实数据库不在公开候选；CLI real 入口固定
`REAL_UNAVAILABLE`。三分钟演示/架构见根 README 与
[docs/demo.md](../../docs/demo.md)。数据为合成
`mes-a07-boundary-v1-pending`，`review_status=pending`。
