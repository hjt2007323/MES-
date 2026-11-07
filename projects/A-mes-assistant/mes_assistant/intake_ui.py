"""A-010 offline intake page helpers and fakes.

The real page uses an injected model/store when B is ready; this module keeps
the first offline demonstration independent of MySQL and of the C answer bank.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import streamlit as st

from mes_assistant import query_catalog
from mes_assistant.intake_extraction import (
    FIELDS,
    _parse_time,
    classify_multi_event,
    validate_record,
)
from mes_assistant.intake_runtime import OfflineIntakeModel, run_intake_request
from mes_assistant.intake_store import DowntimeStore, PRODUCTION_DB_PATH

WORKSPACE = Path(__file__).resolve().parents[3]
DEMO_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "intake" / "offline_demo.json"
DEFAULT_REFERENCE = "2026-11-04T12:00:00+08:00"
DEFAULT_DB_PATH = PRODUCTION_DB_PATH


class IntakeUIError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class FakeDowntimeStore:
    """B-shaped in-memory store used before B-010 is ready."""

    def __init__(self):
        self.records = []
        self.submissions = {}
        self.next_id = 1

    def commit(self, record, *, submission_key):
        normalized, issues = validate_record(record)
        if issues:
            raise IntakeUIError("INVALID_RECORD")
        payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
        if submission_key in self.submissions:
            existing = self.submissions[submission_key]
            if existing["payload"] != payload:
                raise IntakeUIError("SUBMISSION_CONFLICT")
            return {
                "record_id": existing["record_id"],
                "replayed": True,
                "record": existing["record"],
            }
        record_id = self.next_id
        self.next_id += 1
        stored = dict(normalized)
        stored["downtime_id"] = record_id
        self.records.append(stored)
        self.submissions[submission_key] = {
            "payload": payload,
            "record_id": record_id,
            "record": dict(stored),
        }
        return {"record_id": record_id, "replayed": False, "record": dict(stored)}

    def list_window(self, window_start, window_end, *, limit=200):
        start = _parse_time(window_start)
        end = _parse_time(window_end)
        if start is None or end is None or end <= start:
            raise IntakeUIError("INVALID_TIME_ORDER")
        rows = []
        for record in sorted(self.records, key=lambda item: item["downtime_id"]):
            row_start = _parse_time(record["start_time"])
            row_end = _parse_time(record["end_time"])
            if row_start is None or row_end is None:
                continue
            if row_start < end and row_end > start:
                rows.append(
                    [
                        record["downtime_id"],
                        record["machine_no"],
                        record["start_time"],
                        record["end_time"],
                        record["reason"],
                        record["order_id"],
                    ]
                )
        columns = query_catalog.describe_query("downtime_details")["columns"]
        truncated = len(rows) > limit
        return {
            "columns": columns,
            "rows": rows[:limit],
            "truncated": truncated,
            "truncation_reason": "limit" if truncated else None,
        }


def load_demo(path=DEMO_PATH):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data["text"], data["reference_time"]


def _session_store():
    if "intake_store" not in st.session_state:
        st.session_state["intake_store"] = DowntimeStore()
    return st.session_state["intake_store"]


def _session_model():
    if "intake_model" not in st.session_state:
        st.session_state["intake_model"] = OfflineIntakeModel()
    return st.session_state["intake_model"]


def _field_values(record):
    return {field: record.get(field) or "" for field in FIELDS}


def _readback(store, record):
    try:
        result = store.list_window(record["start_time"], record["end_time"])
        return {"state": "ok", "result": result}
    except Exception as exc:
        return {"state": "error", "code": getattr(exc, "code", "READBACK_FAILED")}


def _render_draft(draft):
    st.markdown("**原文引用与缺项**")
    st.write("state：%s；mode：%s" % (draft["state"], draft["mode"]))
    if draft["issues"]:
        st.warning("issues：%s" % ", ".join(draft["issues"]))
    else:
        st.success("字段证据与时间校验通过，等待人工确认。")
    for field in FIELDS:
        evidence = draft["evidence"].get(field)
        if evidence:
            st.caption(
                "%s：%s [%d,%d) 原文=%s"
                % (
                    field,
                    evidence["quote"],
                    evidence["start"],
                    evidence["end"],
                    evidence["quote"],
                )
            )
        elif draft["record"].get(field) is None:
            st.caption("%s：缺失（null）" % field)


def render_intake_entry(*, model_client=None, store=None):
    st.subheader("停机录入（合成沙箱）")
    st.caption(
        "独立离线沙箱：模型只在点击“生成草稿”时调用；确认前不写库。"
        "本批只处理一条已结束的合成停机事件，不代表真实业务数据。"
    )
    store = store or _session_store()
    demo_text, demo_reference = load_demo()

    if "intake_mode" not in st.session_state:
        st.session_state["intake_mode"] = "offline"
    mode = st.radio(
        "运行模式",
        ["offline", "real"],
        horizontal=True,
        key="intake_mode",
    )
    model = model_client or (
        _session_model() if mode == "offline" else None
    )

    if "intake_text" not in st.session_state:
        st.session_state["intake_text"] = demo_text
    if "intake_reference" not in st.session_state:
        st.session_state["intake_reference"] = demo_reference

    if st.button("加载预置演示", key="intake_load_demo"):
        st.session_state["intake_text"] = demo_text
        st.session_state["intake_reference"] = demo_reference
        st.session_state.pop("intake_draft", None)
        st.session_state.pop("intake_receipt", None)
        st.session_state.pop("intake_readback", None)

    text = st.text_area(
        "停机原文（最多 2000 字符）",
        key="intake_text",
        height=150,
    )
    reference_time = st.text_input("reference_time", key="intake_reference")
    if st.button("生成草稿", key="intake_extract"):
        try:
            result = run_intake_request(
                text,
                reference_time=reference_time,
                mode=mode,
                model_client=model,
            )
            draft = result["draft"]
            draft["run_meta"] = result["run"]
            draft["source_mode"] = mode
            draft["source_text"] = text
            draft["source_reference"] = reference_time
            draft["sources"] = {
                field: ("model" if draft["record"].get(field) else None)
                for field in FIELDS
            }
            draft["pending_fields"] = list(draft.get("pending_fields") or [])
            st.session_state["intake_draft"] = draft
            st.session_state.pop("intake_receipt", None)
            st.session_state.pop("intake_readback", None)
        except Exception as exc:
            code = getattr(exc, "code", "INPUT_ERROR")
            st.session_state["intake_draft"] = {
                "draft_id": uuid.uuid4().hex,
                "state": "error",
                "record": {field: None for field in FIELDS},
                "evidence": {field: None for field in FIELDS},
                "issues": [str(code)],
                "mode": "model",
                "terminal": True,
                "pending_fields": [],
                "sources": {field: None for field in FIELDS},
                "source_text": text,
                "source_reference": reference_time,
                "source_mode": mode,
                "run_meta": {
                    "mode": mode,
                    "execution_source": "test_double",
                    "provider": None,
                    "model": None,
                    "client_calls": 0,
                    "elapsed_ms": 0,
                    "usage": None,
                    "error_code": str(code),
                },
            }

    draft = st.session_state.get("intake_draft")
    if not isinstance(draft, dict):
        st.info("加载预置演示或粘贴一条合成文本，再点击“生成草稿”。")
        return

    if (
        draft.get("source_text") != text
        or draft.get("source_reference") != reference_time
        or draft.get("source_mode") != mode
    ):
        st.warning(
            "原文、reference_time 或运行模式已变更，旧草稿不可确认；"
            "请重新点击“生成草稿”。"
        )
        return

    meta = draft.get("run_meta") or {}
    st.caption(
        "生成来源：mode=%s；source=%s；provider=%s；model=%s；"
        "client_calls=%s；usage=%s；error=%s"
        % (
            meta.get("mode"),
            meta.get("execution_source"),
            meta.get("provider"),
            meta.get("model"),
            meta.get("client_calls"),
            json.dumps(meta.get("usage"), ensure_ascii=False),
            meta.get("error_code"),
        )
    )

    _render_draft(draft)
    if draft.get("terminal"):
        issues = draft.get("issues") or []
        if "MULTIPLE_EVENTS" in issues:
            message = "该草稿已关闭提交；请把原文拆成单条已结束事件后重新生成草稿。"
        elif "OUT_OF_SCOPE" in issues:
            message = "该输入超出本沙箱范围；请改用单条停机事件后重新生成草稿。"
        else:
            message = "该草稿因解析或模型错误关闭提交；请修正后重新生成草稿。"
        st.error(message)
        return
    if draft["state"] not in ("ready", "needs_clarification"):
        return

    draft_id = draft["draft_id"]
    st.markdown("**可编辑字段（人工修改会标记为 user）**")
    values = {}
    for field in FIELDS:
        values[field] = st.text_input(
            field,
            value=draft["record"].get(field) or "",
            key="intake_field_%s_%s" % (draft_id, field),
        )
    order_blank = st.checkbox(
        "工单明确留空（可选）",
        value=False,
        key="intake_order_blank_%s" % draft_id,
    )
    if order_blank:
        values["order_id"] = ""

    current_form = {
        field: (values[field].strip() or None) for field in FIELDS
    }
    normalized_form, form_issues = validate_record(current_form)
    form_dirty = bool(form_issues) or normalized_form != draft["record"]
    if form_dirty:
        st.warning(
            "检测到尚未重新校验的字段修改；请先点击“重新校验”，"
            "否则确认会被阻止。"
        )

    if st.button("重新校验", key="intake_validate_%s" % draft_id):
        current = {
            field: (values[field].strip() or None) for field in FIELDS
        }
        edited = {
            field
            for field in FIELDS
            if current[field] != (draft["record"].get(field) or None)
        }
        if order_blank:
            edited.add("order_id")
        normalized, issues = validate_record(current)
        pending = [
            field
            for field in draft.get("pending_fields", [])
            if field not in edited
        ]
        if pending:
            issues.append("EVIDENCE_MISMATCH")
            issues = list(dict.fromkeys(issues))
        sources = dict(draft.get("sources") or {})
        evidence = dict(draft.get("evidence") or {})
        for field in edited:
            sources[field] = "user"
            evidence[field] = None
        draft.update(
            {
                "state": "ready" if not issues else "needs_clarification",
                "record": normalized,
                "issues": issues,
                "evidence": evidence,
                "sources": sources,
                "pending_fields": pending,
            }
        )
        st.session_state["intake_draft"] = draft

    draft = st.session_state.get("intake_draft")
    if draft.get("sources"):
        edited = [field for field, source in draft["sources"].items() if source == "user"]
        if edited:
            st.caption("人工修改字段：%s" % ", ".join(edited))
    if draft.get("pending_fields"):
        st.caption("待处理字段（引用不成立或未确认）：%s" % ", ".join(draft["pending_fields"]))
    multi_original = classify_multi_event(text)
    if multi_original:
        st.error("原文包含多条独立停机事件；必须修改原文后重新生成，不能只修字段确认。")
    if draft["state"] != "ready" or draft["issues"]:
        st.warning("当前草稿不可确认；请补齐缺项/修正时间后再校验。")
        return

    if st.button("确认并写入", key="intake_commit_%s" % draft_id):
        if multi_original:
            st.session_state["intake_receipt"] = None
            st.session_state["intake_readback"] = None
            st.error("确认被阻止：原文包含多条独立停机事件；请改原文并重新生成。")
        else:
            current_form = {
                field: (values[field].strip() or None) for field in FIELDS
            }
            normalized_form, form_issues = validate_record(current_form)
            if form_issues or normalized_form != draft["record"]:
                st.session_state["intake_receipt"] = None
                st.session_state["intake_readback"] = None
                st.error(
                    "确认被阻止：表单与最后已校验草稿不一致；"
                    "请先点击“重新校验”。"
                )
            else:
                try:
                    receipt = store.commit(
                        draft["record"], submission_key=draft["draft_id"]
                    )
                    receipt = dict(receipt)
                    receipt["draft_id"] = draft["draft_id"]
                    receipt["run"] = draft.get("run_meta") or {}
                    st.session_state["intake_receipt"] = receipt
                    st.session_state["intake_readback"] = _readback(store, draft["record"])
                except Exception as exc:
                    st.session_state["intake_receipt"] = {
                        "error": getattr(exc, "code", "STORE_UNAVAILABLE"),
                        "draft_id": draft["draft_id"],
                    }
                    st.session_state["intake_readback"] = None

    receipt = st.session_state.get("intake_receipt")
    if isinstance(receipt, dict):
        if "error" in receipt:
            st.error(
                "写入失败：%s；draft=%s"
                % (receipt["error"], receipt.get("draft_id"))
            )
        else:
            st.success(
                "写入：record_id=%s；replayed=%s；draft=%s；source=%s；record=%s"
                % (
                    receipt.get("record_id"),
                    receipt.get("replayed"),
                    receipt.get("draft_id"),
                    (receipt.get("run") or {}).get("execution_source"),
                    json.dumps(
                        receipt.get("record") or draft["record"],
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                )
            )
    st.markdown("**按可编辑时间窗查回**")
    window_start = st.text_input(
        "window_start",
        value=draft["record"].get("start_time") or "",
        key="intake_window_%s_start" % draft_id,
    )
    window_end = st.text_input(
        "window_end",
        value=draft["record"].get("end_time") or "",
        key="intake_window_%s_end" % draft_id,
    )
    if st.button("查回", key="intake_readback_button_%s" % draft_id):
        try:
            result = store.list_window(window_start, window_end)
            st.session_state["intake_readback"] = {"state": "ok", "result": result}
        except Exception as exc:
            st.session_state["intake_readback"] = {
                "state": "error",
                "code": getattr(exc, "code", "READBACK_FAILED"),
            }

    readback = st.session_state.get("intake_readback")
    if isinstance(readback, dict):
        if readback["state"] == "error":
            st.warning("查回失败：%s；可使用已有 ID 重试读操作。" % readback["code"])
        else:
            result = readback["result"]
            st.caption(
                "查回状态：%s；rows=%d；truncated=%s"
                % ("ok", len(result["rows"]), result["truncated"])
            )
            if result["rows"]:
                st.dataframe(
                    [dict(zip([c["name"] for c in result["columns"]], row)) for row in result["rows"]],
                    width="stretch",
                    hide_index=True,
                )
