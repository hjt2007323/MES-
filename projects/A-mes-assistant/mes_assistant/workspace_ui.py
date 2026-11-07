"""A2/A3 Streamlit workspace: one entry for history query, intake query and drafts."""

from __future__ import annotations

import json
import uuid

import streamlit as st

from mes_assistant.intake_store import DowntimeStore
from mes_assistant.workspace_assistant import WorkspaceService, WorkspaceError

REFERENCE = "2026-11-04T12:00:00+08:00"


def _session_store():
    if "workspace_store" not in st.session_state:
        st.session_state["workspace_store"] = DowntimeStore()
    return st.session_state["workspace_store"]


def _session_id():
    if "workspace_session_id" not in st.session_state:
        st.session_state["workspace_session_id"] = "ws-" + uuid.uuid4().hex
    return st.session_state["workspace_session_id"]


def _session_service():
    if "workspace_service" not in st.session_state:
        st.session_state["workspace_service"] = WorkspaceService(_session_store())
    return st.session_state["workspace_service"]


def _confirm_source(current_question):
    last = st.session_state.get("workspace_last_source")
    if last == current_question:
        return last
    if last and last.startswith("录入 " + str(current_question) + "\n"):
        return last
    return current_question


def _render_result(result, service, confirm_source):
    st.caption(
        "status=%s；action=%s；source=%s；execution_source=%s；"
        "model_calls=%s；query_calls=%s"
        % (
            result.get("status"),
            result.get("action"),
            result.get("source"),
            result.get("execution_source"),
            result.get("model_calls"),
            result.get("query_calls"),
        )
    )
    if result.get("answer"):
        st.write(result["answer"])
    if result.get("issues"):
        st.warning("issues：%s" % ", ".join(result["issues"]))
    table = result.get("table")
    if isinstance(table, dict) and table.get("columns") and table.get("rows") is not None:
        st.dataframe(
            [dict(zip([c["name"] for c in table["columns"]], row)) for row in table["rows"]],
            width="stretch",
            hide_index=True,
        )
        st.caption(
            "table=%d rows；truncated=%s；source=%s"
            % (len(table["rows"]), table.get("truncated"), table.get("source"))
        )
    draft = result.get("draft")
    if isinstance(draft, dict):
        st.markdown("**草稿**")
        st.json(draft, expanded=False)
        if result.get("status") == "success" and draft.get("state") == "ready":
            if st.button("确认写入", key="workspace-confirm-%s" % draft["draft_id"]):
                try:
                    receipt = service.confirm(
                        draft,
                        session_id=st.session_state.get("workspace_session_id"),
                        source_text=confirm_source,
                    )
                    st.session_state["workspace_last_record"] = receipt["record_id"]
                    st.success(
                        "已写入：record_id=%s；replayed=%s"
                        % (receipt["record_id"], receipt["replayed"])
                    )
                except Exception as exc:
                    st.error("写入失败：%s" % getattr(exc, "code", "STORE_UNAVAILABLE"))
    with st.expander("trace"):
        st.json(result.get("trace") or [], expanded=False)


def render_workspace(*, store=None, model_client=None):
    st.subheader("统一工作台（M5 录入 + 受控问答）")
    st.caption(
        "同一请求可路由到历史 MySQL 受控问答或新增 SQLite 停机记录；"
        "写入只经人工确认，不进入模型工具。默认 offline。"
    )
    if store is not None or model_client is not None:
        service = WorkspaceService(store or _session_store(), model_client=model_client)
    else:
        service = _session_service()
    session_id = _session_id()
    for key in ("workspace_last_record", "workspace_response", "workspace_last_source"):
        if key not in st.session_state:
            st.session_state[key] = None
    if "workspace_followups" not in st.session_state:
        st.session_state["workspace_followups"] = []

    question = st.text_area(
        "问题或录入原文",
        value="查询今天的停机记录",
        height=100,
        key="workspace_question",
    )
    mode = st.radio("运行模式", ["offline", "real"], horizontal=True, key="workspace_mode")
    reference = st.text_input("reference_time", value=REFERENCE, key="workspace_reference")

    def run_request(text, request_id):
        request = {
            "request_id": request_id,
            "session_id": session_id,
            "question": text,
            "reference_time": reference,
            "timezone": "Asia/Shanghai",
            "mode": mode,
            "last_confirmed_record_id": st.session_state.get("workspace_last_record"),
        }
        response = service.run(request)
        st.session_state["workspace_response"] = response
        if response.get("action") == "prepare_intake":
            st.session_state["workspace_last_source"] = text
        return response

    if st.button("运行", key="workspace-run"):
        st.session_state["workspace_followups"] = []
        try:
            run_request(question, "ui-" + uuid.uuid4().hex)
        except WorkspaceError as exc:
            st.session_state["workspace_response"] = {
                "status": "error",
                "action": "input",
                "answer": "请求无法执行。",
                "issues": [exc.code],
                "source": None,
                "execution_source": None,
            }
    if st.session_state["workspace_response"] is not None:
        _render_result(
            st.session_state["workspace_response"],
            service,
            _confirm_source(question),
        )
        current = st.session_state["workspace_response"]
        if (
            current.get("action") == "prepare_intake"
            and current.get("status") == "needs_clarification"
            and isinstance(current.get("draft"), dict)
        ):
            followup = st.text_input(
                "补充说明（追加到原文，不覆盖原证据）",
                key="workspace-followup",
            )
            if st.button("补充并重新提取", key="workspace-followup-run"):
                if not followup.strip():
                    st.warning("补充说明不能为空。")
                elif len(st.session_state["workspace_followups"]) >= 3:
                    st.warning("最多补充3轮；请重新打开问题或修改原文。")
                else:
                    history = list(st.session_state["workspace_followups"])
                    combined = "录入 " + question + "\n" + "\n".join(history + [followup.strip()])
                    try:
                        response = run_request(combined, "ui-" + uuid.uuid4().hex)
                        if response.get("action") == "prepare_intake":
                            st.session_state["workspace_followups"] = history + [followup.strip()]
                    except WorkspaceError as exc:
                        st.error(exc.code)

    st.divider()
    st.markdown("**多行草稿队列**")
    st.caption(
        "每个非空行一条草稿，最多10条；一行多事件仍按单事件护栏处理；逐条确认，不自动写入。"
    )
    queue_text = st.text_area("每行一条停机原文", height=100, key="workspace_queue_text")
    if st.button("生成队列草稿", key="workspace-queue"):
        try:
            drafts = service.prepare_queue(
                queue_text,
                request={
                    "request_id": "queue-" + uuid.uuid4().hex,
                    "session_id": session_id,
                    "question": "queue",
                    "reference_time": reference,
                    "timezone": "Asia/Shanghai",
                    "mode": mode,
                    "last_confirmed_record_id": st.session_state.get("workspace_last_record"),
                },
            )
            st.session_state["workspace_queue"] = drafts
        except WorkspaceError as exc:
            st.session_state["workspace_queue"] = []
            st.error(exc.code)
    queue = st.session_state.get("workspace_queue") or []
    for item in queue:
        with st.expander("#%d %s" % (item["index"], item["text"])):
            st.caption("status=%s；issues=%s" % (item["status"], ", ".join(item["issues"])))
            draft = item.get("draft") or {}
            if draft.get("state") == "ready":
                if st.button("确认此条", key="queue-confirm-%s" % draft.get("draft_id")):
                    try:
                        receipt = service.confirm(
                            draft,
                            session_id=session_id,
                            source_text=item.get("source"),
                        )
                        st.session_state["workspace_last_record"] = receipt["record_id"]
                        st.success(
                            "record_id=%s；replayed=%s"
                            % (receipt["record_id"], receipt["replayed"])
                        )
                    except Exception as exc:
                        st.error("写入失败：%s" % getattr(exc, "code", "STORE_UNAVAILABLE"))
