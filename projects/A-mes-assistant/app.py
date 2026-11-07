"""A-006 M2：受控自然语言问答 + 保留三场景命名查询入口。

自然语言入口只允许模型产出 query/clarify/refuse 动作，SQL 仍由 M1 命名
查询执行；页面不开放 SQL 编辑，不自动启停数据库，不在加载时调用模型。
"""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from mes_assistant import intake_ui
from mes_assistant import ui_presenter as presenter
from mes_assistant import workspace_ui

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[1]
REQUEST_PATH = ROOT / "fixtures" / "m1" / "request.json"
M2_REQUEST_PATH = ROOT / "fixtures" / "m2" / "request.json"
LOG_PATH = WORKSPACE / "runtime" / "a07-app" / "ui-events.jsonl"

st.set_page_config(
    page_title="A07 MES 受控数据问答",
    page_icon="🏭",
    layout="wide",
)

st.title("A07 MES 受控数据问答")
st.caption(
    "M2 自然语言入口：模型只产生 query/clarify/refuse，程序复用 M1 命名查询；"
    "synthetic/pending；不是通用自由 Text2SQL，不开放任意 SQL。"
)

with st.sidebar:
    st.header("范围与边界")
    st.markdown(
        "- 自然语言问答 + 三场景命名查询两个入口\n"
        "- 默认 mock（脚本/替身，无网络无 DB）；real 需显式配置且只在提交时调用\n"
        "- 模型只产出 query/clarify/refuse，SQL 由 M1 命名查询执行\n"
        "- NULL 显示“未知”；空表不画成 0；截断/澄清/拒绝/预算会提示\n"
        "- 不计算新指标、不把停机条数当分钟，不自动启停数据库"
    )
    st.caption("A-006 · schema 0.1 r3")

def _render_question_entry():
    st.subheader("自然语言问答（受控查询计划）")
    st.caption(
        "模型只输出 query / clarify / refuse 三种动作；query 也必须经过 M1 "
        "参数化的三个命名查询。默认 mock 不联网。"
    )
    with st.form("question-form"):
        question = st.text_area(
            "中文问题",
            value="列出各产线当前累计实际产量快照（不按日期）",
            height=90,
        )
        reference_time = st.text_input(
            "reference_time", value="2026-11-02T00:00:00+08:00"
        )
        mode = st.radio(
            "运行模式",
            ["mock", "real"],
            index=0,
            horizontal=True,
            help="mock 用脚本/替身；real 需显式模型配置，且只在提交时调用一次。",
        )
        st.caption(
            "预算：总 30s / 模型 2 次 / 查询 2 条 / 纠错 1 次；"
            "真实模式只在点击提交时调用。"
        )
        submitted = st.form_submit_button("提问")

    if submitted:
        request = None
        try:
            base = json.loads(M2_REQUEST_PATH.read_text(encoding="utf-8"))
            request = presenter.build_question_request(
                base, question, reference_time
            )
            with presenter.open_log_sink(LOG_PATH) as log_sink:
                if mode == "mock":
                    response = presenter.run_question_submission(
                        presenter.mock_question, request, log_sink=log_sink
                    )
                else:
                    response = presenter.run_question_submission(
                        presenter.real_question, request, log_sink=log_sink
                    )
            st.session_state["last_question_response"] = response
            st.session_state["last_question_request_id"] = request["request_id"]
        except Exception as exc:
            request_id = request["request_id"] if isinstance(request, dict) else ""
            code = getattr(exc, "code", None)
            if code == "CONFIG_MISSING":
                code = "MODEL_CONFIG_MISSING"
            elif not isinstance(code, str) or not code:
                code = "UI_EXECUTION_ERROR"
            st.session_state["last_question_response"] = {
                "schema_version": "0.1",
                "run_id": "",
                "request_id": request_id,
                "case_id": "m2-ui",
                "data_version": "mes-a07-boundary-v1-pending",
                "reference_time": reference_time,
                "timezone": "Asia/Shanghai",
                "mode": mode,
                "status": "error",
                "answer": "问答未能完成（未产生真实成功结果）。",
                "table": None,
                "sql": None,
                "metrics": [],
                "error": {
                    "code": code,
                    "stage": "model" if code == "MODEL_CONFIG_MISSING" else "ui",
                    "message": "问答未能完成；未产生真实成功结果。",
                    "retryable": False,
                },
                "timing": {"internal_ms": 0},
                "counts": {
                    "model_calls": 0,
                    "queries": None,
                    "corrections": 0,
                },
                "usage": None,
                "config_id": "m2-ui",
                "query_id": None,
                "review_status": "pending",
                "trace": [],
            }
            st.session_state["last_question_request_id"] = request_id

    response = st.session_state.get("last_question_response")
    if not isinstance(response, dict):
        st.info("输入中文问题并提交；mock 可直接离线演示，real 需要显式模型配置。")
        return

    vm = presenter.question_view(response)
    st.divider()
    st.subheader("问答结果")
    st.caption(
        "request_id=%s | status=%s | mode=%s | data_version=%s | review_status=%s"
        % (
            vm["request_id"] or "—",
            vm["status"] or "—",
            vm["mode"] or "—",
            vm["data_version"],
            vm["review_status"] or "—",
        )
    )
    if vm["source"] == "real_success":
        st.caption("mode=real：本次来自真实模型与 B 命名工具；数据仍是 synthetic，业务口径 pending。")
    elif vm["status"] in ("needs_clarification", "refused") and vm["mode"] == "real":
        st.caption("mode=real 的受控规划结果；未执行数据库查询。")
    elif vm["mode"] == "real":
        st.caption(
            "请求配置 mode=real；本次没有取得真实成功结果，不能声称数据来自真实查询。"
        )
    else:
        st.caption("mode=%s：脚本/替身结果，不是真实模型或数据库。" % (vm["mode"] or "unknown"))

    if vm["demo_policy_confirmed"]:
        evidence = vm["context_evidence"] or {}
        selected = evidence.get("selected_query_ids") or []
        st.caption(
            "演示口径已确认：%s | context_mode=%s | selected=%s | reason=%s"
            % (
                vm["semantic_policy_version"],
                vm["context_mode"] or "—",
                ",".join(selected) or "—",
                evidence.get("reason") or "—",
            )
        )
    else:
        st.caption("演示口径未取得：本次响应没有语义版本/上下文证据。")
    st.caption(
        "合成数据；data_version=%s；review_status=%s | 采纳计划=%s"
        % (vm["data_version"], vm["review_status"] or "—", vm["plan_text"])
    )

    if vm["clarification"]:
        st.warning(vm["answer"] or "需要补充信息。")
    elif vm["refused"]:
        st.warning(vm["answer"] or "该问题超出受控范围。")
    elif vm["status"] == "ok":
        if vm["truncated"]:
            st.warning("结果被截断（%s）。" % (vm["truncation_reason"] or "reason 未提供"))
        if vm["empty"]:
            st.info("返回 0 行（空表，属 ok）；不画成 0。")
        else:
            records = [
                dict(zip(vm["columns"], row)) for row in vm["display_rows"]
            ]
            st.dataframe(records, width="stretch", hide_index=True)
        if vm["chart"]:
            st.caption("产量柱状图直接展示 B 返回的 actual_qty。")
            st.bar_chart(vm["chart"], x="line_no", y="actual_qty")
        st.markdown("**解释**")
        st.write(vm["answer"])
    else:
        st.error("%s / %s" % (vm["status"] or "error", vm["error_text"] or "未知错误"))
        if vm["answer"]:
            st.info(vm["answer"])

    if vm["sql"]:
        with st.expander("受控 SQL（M1/B 工具返回，只读）"):
            st.code(vm["sql"], language="sql")
    if vm["trace"]:
        with st.expander("trace"):
            st.json(vm["trace"])
    st.caption(
        "counts=%s | usage=%s"
        % (
            json.dumps(vm["counts"], ensure_ascii=False, sort_keys=True),
            json.dumps(vm["usage"], ensure_ascii=False, sort_keys=True),
        )
    )


entry = st.radio(
    "入口",
    ["自然语言问答", "命名查询", "停机录入（合成沙箱）", "统一工作台（v2）"],
    horizontal=True,
    index=0,
)
if entry == "自然语言问答":
    _render_question_entry()
    st.stop()
if entry == "停机录入（合成沙箱）":
    intake_ui.render_intake_entry()
    st.stop()
if entry == "统一工作台（v2）":
    workspace_ui.render_workspace()
    st.stop()

st.subheader("命名查询（A-005 原入口）")
labels = [item["label"] for item in presenter.SCENARIOS]
selected_label = st.selectbox("选择场景", labels, index=0)
scenario = presenter.SCENARIO_BY_LABEL[selected_label]
query_id = scenario["query_id"]
st.caption(scenario["hint"])

line_id = 1
window_start = presenter.DEFAULT_WINDOW_START
window_end = presenter.DEFAULT_WINDOW_END

with st.form("named-query-form"):
    if query_id == "work_order_progress":
        line_id = st.number_input(
            "line_id（1..2147483647）",
            min_value=1,
            max_value=2147483647,
            value=1,
            step=1,
        )
    elif query_id == "downtime_details":
        window_start = st.text_input(
            "window_start（含）", value=presenter.DEFAULT_WINDOW_START
        )
        window_end = st.text_input(
            "window_end（不含）", value=presenter.DEFAULT_WINDOW_END
        )
        st.caption(
            "格式 YYYY-MM-DDTHH:MM:SS+08:00；只返回与窗口相交的原始事件。"
        )
    else:
        st.caption("该场景无参数；返回当前累计快照，不是日增量。")
    submitted = st.form_submit_button("运行命名查询")

if submitted:
    request = None
    try:
        request = presenter.new_request(
            json.loads(REQUEST_PATH.read_text(encoding="utf-8")),
            query_id,
        )
        params = presenter.build_params(
            query_id,
            line_id=line_id,
            window_start=window_start,
            window_end=window_end,
        )
        with presenter.open_log_sink(LOG_PATH) as log_sink:
            response = presenter.run_submission(
                presenter.real_query,
                request,
                query_id,
                params,
                log_sink=log_sink,
            )
        st.session_state["last_response"] = response
        st.session_state["last_request_id"] = request["request_id"]
        st.session_state["last_query_id"] = query_id
        st.session_state["last_label"] = selected_label
    except Exception:
        request_id = request["request_id"] if isinstance(request, dict) else ""
        st.session_state["last_response"] = {
            "schema_version": "0.1",
            "run_id": "",
            "request_id": request_id,
            "query_id": query_id,
            "mode": "real",
            "status": "error",
            "answer": None,
            "table": None,
            "sql": None,
            "error": {
                "code": "UI_EXECUTION_ERROR",
                "stage": "ui",
                "message": "页面未能完成查询（未产生 real 结果）",
                "retryable": False,
            },
            "counts": {
                "model_calls": 0,
                "queries": None,
                "corrections": 0,
            },
            "trace": [],
            "data_version": "mes-a07-boundary-v1-pending",
            "review_status": "pending",
        }
        st.session_state["last_request_id"] = request_id
        st.session_state["last_query_id"] = query_id
        st.session_state["last_label"] = selected_label

response = st.session_state.get("last_response")
if not isinstance(response, dict):
    st.info("选择场景并提交；页面只在点击“运行命名查询”时执行一次查询。")
    st.stop()

vm = presenter.present_result(response)

st.divider()
st.subheader("结果")
st.caption(
    "request_id=%s | query_id=%s | mode=%s | data_version=%s | review_status=%s"
    % (
        vm["request_id"] or "—",
        vm["query_id"] or "—",
        vm["mode"] or "—",
        vm["data_version"],
        vm["review_status"] or "—",
    )
)
if vm["source"] == "real_success":
    st.caption("mode=real：本次来自真实 B 命名工具；数据仍是 synthetic，业务口径 pending。")
elif vm["mode"] == "real":
    st.caption(
        "请求配置 mode=real；本次没有取得真实 B 成功结果，"
        "不能声称数据来自真实查询。"
    )
else:
    st.caption("mode=%s：不是真实数据库结果。" % (vm["mode"] or "unknown"))

if vm["status"] == "ok":
    if vm["truncated"]:
        st.warning(
            "结果被截断（%s），不是完整结果。"
            % (vm["truncation_reason"] or "reason 未提供")
        )
    if vm["empty"]:
        st.info("返回 0 行（空表，属 ok）；不画成 0，也不补造数据。")
    else:
        records = [
            dict(zip(vm["columns"], row)) for row in vm["display_rows"]
        ]
        st.dataframe(records, width="stretch", hide_index=True)
    if vm["chart"]:
        st.caption("产量柱状图直接展示 B 返回的 actual_qty；不计算新指标。")
        st.bar_chart(
            vm["chart"],
            x="line_no",
            y="actual_qty",
        )
    st.markdown("**解释**")
    st.write(vm["answer"])
elif vm["status"] == "refused":
    st.warning(vm["answer"] or "查询被拒绝。")
    if vm["error_text"]:
        st.caption(vm["error_text"])
else:
    st.error("%s / %s" % (vm["status"] or "error", vm["error_text"] or "未知错误"))
    if vm["answer"]:
        st.info(vm["answer"])

if vm["sql"]:
    with st.expander("受控 SQL（B 工具返回，只读）"):
        st.code(vm["sql"], language="sql")
if vm["trace"]:
    with st.expander("trace"):
        st.json(vm["trace"])
st.caption(
    "counts: %s"
    % json.dumps(vm["counts"], ensure_ascii=False, sort_keys=True)
)
