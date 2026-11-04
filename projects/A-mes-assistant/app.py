"""A07 公开离线候选：受控自然语言问答（mock-only）。

页面只提供离线 mock；不包含 real、独立命名查询、模型 key 或数据库入口。
模型只产生 query/clarify/refuse，查询走命名目录的演示参数；表/SQL/计数均为
离线模拟，不是真实查询结果。
"""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from mes_assistant import ui_presenter as presenter

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[1]
M2_REQUEST_PATH = ROOT / "fixtures" / "m2" / "request.json"
LOG_PATH = WORKSPACE / "runtime" / "a07-app" / "ui-events.jsonl"

st.set_page_config(
    page_title="A07 MES 受控数据问答",
    page_icon="🏭",
    layout="wide",
)

st.title("A07 MES 受控数据问答")
st.caption(
    "公开离线候选：模型只产生 query/clarify/refuse，程序使用三个命名查询的"
    "合成演示参数；synthetic/pending；不是通用自由 Text2SQL，不开放任意 SQL。"
)

with st.sidebar:
    st.header("范围与边界")
    st.markdown(
        "- 仅自然语言问答；离线 mock，无网络、无 DB、无 API key\n"
        "- 模型只产出 query/clarify/refuse；表/SQL/计数为模拟\n"
        "- NULL 显示“未知”；空表不画成 0；澄清/拒绝/预算会提示\n"
        "- 不计算新指标、不把停机条数当分钟，不自动启停数据库\n"
        "- real、独立命名查询和真实数据库不在本公开候选"
    )
    st.caption("A07 offline candidate · schema 0.1 r3")

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
        st.caption(
            "离线 mock：表/SQL/计数均为模拟；预算 30s / 模型 2 次 / 查询 2 条 / 纠错 1 次。"
        )
        submitted = st.form_submit_button("提问")

    if submitted:
        request = None
        mode = "mock"
        try:
            base = json.loads(M2_REQUEST_PATH.read_text(encoding="utf-8"))
            request = presenter.build_question_request(
                base, question, reference_time
            )
            with presenter.open_log_sink(LOG_PATH) as log_sink:
                response = presenter.run_question_submission(
                    presenter.mock_question, request, log_sink=log_sink
                )
            st.session_state["last_question_response"] = response
            st.session_state["last_question_request_id"] = request["request_id"]
        except Exception as exc:
            request_id = request["request_id"] if isinstance(request, dict) else ""
            code = getattr(exc, "code", None)
            if not isinstance(code, str) or not code:
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
                "answer": "问答未能完成（mock 入口未产生结果）。",
                "table": None,
                "sql": None,
                "metrics": [],
                "error": {
                    "code": code,
                    "stage": "ui",
                    "message": "问答未能完成；未产生真实结果。",
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
        st.info("输入中文问题并提交；公开候选只提供离线 mock。")
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
    st.caption("公开候选仅离线 mock：表、SQL、计数为模拟，不是真实模型或数据库。")

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
        with st.expander("模拟 SQL（离线占位，不是真实查询）"):
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


_render_question_entry()
