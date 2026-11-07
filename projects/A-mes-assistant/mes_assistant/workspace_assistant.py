"""A2/A3 workspace service: route history query, intake query and preparation.

The service is deliberately deterministic in offline mode.  It performs no
write except the explicit ``confirm`` method; history questions delegate to the
existing controlled M2 runtime, and intake queries use B's read-only
``search_window``.
"""

from __future__ import annotations

import copy
import io
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from mes_assistant import ui_presenter as presenter
from mes_assistant.intake_extraction import (
    _machine_tokens,
    classify_multi_event,
    validate_record,
)
from mes_assistant.intake_runtime import run_intake_request
from mes_assistant.intake_store import DowntimeStore

WORKSPACE = Path(__file__).resolve().parents[3]
BASE_REQUEST = Path(__file__).resolve().parents[1] / "fixtures" / "m2" / "request.json"
LOCAL = timezone(timedelta(hours=8))
MAX_QUEUE_LINES = 10


class WorkspaceError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


def _result(request, *, status, action, answer="", table=None, source=None,
            filters=None, record_id=None, draft=None, issues=None, trace=None,
            model_calls=0, query_calls=0, usage=None, error=None,
            execution_source=None):
    return {
        "schema_version": "m5-workspace-v2",
        "request_id": request.get("request_id") or uuid.uuid4().hex,
        "session_id": request.get("session_id"),
        "mode": request.get("mode", "offline"),
        "status": status,
        "action": action,
        "answer": answer,
        "table": table,
        "source": source,
        "execution_source": execution_source,
        "filters": filters or {},
        "record_id": record_id,
        "draft": draft,
        "issues": issues or [],
        "trace": trace or [],
        "model_calls": model_calls,
        "query_calls": query_calls,
        "usage": usage,
        "error": error,
    }


def _reference(request):
    value = request.get("reference_time")
    if not isinstance(value, str):
        raise WorkspaceError("INPUT_INVALID")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise WorkspaceError("INPUT_INVALID")
    if parsed.tzinfo is None:
        raise WorkspaceError("INPUT_INVALID")
    if parsed.utcoffset() != timedelta(hours=8):
        raise WorkspaceError("INPUT_INVALID")
    if request.get("timezone") not in (None, "Asia/Shanghai"):
        raise WorkspaceError("INPUT_INVALID")
    return parsed


def _window_from_question(question, reference):
    text = question.strip()
    exact = re.search(
        r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}):(\d{2})\s*(?:到|至|-)\s*(\d{1,2}):(\d{2})",
        text,
    )
    if exact:
        day = date.fromisoformat(exact.group(1))
        start = datetime(day.year, day.month, day.day, int(exact.group(2)), int(exact.group(3)), tzinfo=LOCAL)
        end = datetime(day.year, day.month, day.day, int(exact.group(4)), int(exact.group(5)), tzinfo=LOCAL)
        if end <= start:
            return None
        return start.isoformat(), end.isoformat()
    bare = re.search(r"(?<!\d)(\d{1,2}):(\d{2})\s*(?:到|至|-)\s*(\d{1,2}):(\d{2})(?!\d)", text)
    if bare:
        start = reference.replace(
            hour=int(bare.group(1)), minute=int(bare.group(2)), second=0, microsecond=0
        )
        end = reference.replace(
            hour=int(bare.group(3)), minute=int(bare.group(4)), second=0, microsecond=0
        )
        if end <= start:
            return None
        return start.isoformat(), end.isoformat()
    if "今天" in text:
        start = reference.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.isoformat(), (start + timedelta(days=1)).isoformat()
    if "昨天" in text:
        start = (reference - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return start.isoformat(), (start + timedelta(days=1)).isoformat()
    if "本周" in text:
        start = reference.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            days=reference.weekday()
        )
        return start.isoformat(), (start + timedelta(days=7)).isoformat()
    dates = re.findall(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    if len(dates) == 2:
        start_day = date.fromisoformat(dates[0])
        end_day = date.fromisoformat(dates[1])
        if end_day < start_day:
            return None
        start = datetime.combine(start_day, datetime.min.time(), LOCAL)
        end = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), LOCAL)
        return start.isoformat(), end.isoformat()
    if len(dates) == 1:
        day = date.fromisoformat(dates[0])
        start = datetime.combine(day, datetime.min.time(), LOCAL)
        return start.isoformat(), (start + timedelta(days=1)).isoformat()
    return None


def _machine(question):
    # Any explicit machine-looking token must be known; never silently drop it.
    unknown = re.findall(r"(?<![A-Za-z0-9])M(\d{1,3})(?![A-Za-z0-9])", question, re.IGNORECASE)
    unknown += re.findall(r"(?<!\d)(\d{1,3})\s*号机", question)
    machines = sorted(set(_machine_tokens(question)))
    if any(value.lstrip("0") not in ("1", "2", "3") for value in unknown):
        return None, "UNKNOWN_MACHINE"
    if len(machines) > 1:
        return None, "MULTIPLE_MACHINES"
    return (machines[0] if machines else None), None


def _record_id(question):
    match = re.search(r"(?:record_id\s*=\s*|记录\s*#?|ID\s*#?)(\d+)", question, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _kind(question):
    text = question.lower()
    if any(
        word in question
        for word in ("DROP TABLE", "drop table", "忽略规则", "把库文件", "删除数据库")
    ):
        return "refuse"
    if (
        any(word in question for word in ("历史", "工单", "产量"))
        and any(word in question for word in ("刚录入", "刚才", "刚刚", "加在一起", "总数"))
    ):
        return "cross_source"
    if any(word in question for word in ("刚录入", "刚确认", "刚才", "刚刚", "已录入")):
        return "intake_query"
    if any(word in text for word in ("录入", "登记", "新增", "新建")):
        if not any(word in text for word in ("查询", "查回", "看看", "列出", "记录")):
            return "prepare_intake"
    if any(
        word in question
        for word in (
            "刚录入",
            "刚确认",
            "刚才",
            "刚刚",
            "已录入",
            "录入记录",
            "停机记录",
            "停机",
            "有哪些",
            "所有",
            "有停机吗",
        )
    ):
        return "intake_query"
    if any(word in text for word in ("历史", "产量", "工单", "良率", "累计")) and not any(
        word in text for word in ("录入", "登记")
    ):
        return "history_query"
    if any(word in text for word in ("查询", "查回", "列出", "看看", "显示", "查")):
        return "intake_query"
    return "history_query"


def _history(request, question, model_client, history_tool=None):
    if request.get("mode") == "real":
        return _real_history(request, question, model_client, history_tool)

    from mes_assistant.fakes import FakeClock
    from mes_assistant.question_fakes import MockTool
    from mes_assistant.query_runtime import run_query

    mock_tool = MockTool()

    def one_query_tool(query_id, params, **kwargs):
        dto = mock_tool(query_id, params, **kwargs)
        if isinstance(dto, dict) and isinstance(dto.get("counts"), dict):
            dto = dict(dto)
            dto["counts"] = dict(dto["counts"])
            dto["counts"]["queries"] = 1
        return dto

    base = json.loads(BASE_REQUEST.read_text(encoding="utf-8"))
    qrequest = presenter.build_question_request(
        base, question, request["reference_time"]
    )
    if any(word in question for word in ("产量", "快照")):
        query_id, params = "line_snapshot", {}
    elif "工单" in question:
        line_match = re.search(r"(?:产线\s*(\d+)|(\d+)\s*号产线)", question)
        line_id = int(line_match.group(1) or line_match.group(2)) if line_match else None
        if line_id not in (1, 2, 3):
            return _result(
                request,
                status="needs_clarification",
                action="history_query",
                answer="请给出 1/2/3 号产线，不能默认成 1 号产线。",
                issues=["MISSING_LINE_NO"],
                source="history_mysql",
                execution_source="test_double",
                trace=[{"action": "history_query", "status": "needs_clarification"}],
            )
        query_id, params = "work_order_progress", {"line_id": line_id}
    else:
        return _result(
            request,
            status="needs_clarification",
            action="history_query",
            answer="历史受控问答仅支持产量快照、工单进度或停机明细；请明确指标。",
            issues=["AMBIGUOUS_HISTORY_QUERY"],
            source="history_mysql",
            execution_source="test_double",
            trace=[{"action": "history_query", "status": "needs_clarification"}],
        )
    response = run_query(
        qrequest,
        query_id=query_id,
        params=params,
        tool=one_query_tool,
        clock=FakeClock(),
        service_limits=qrequest["budget"],
        remaining_timeout_ms=qrequest["budget"]["timeout_ms"],
        mode="mock",
        log_sink=io.StringIO(),
    )
    status = response.get("status", "error")
    if status == "ok":
        status = "success"
    return _result(
        request,
        status=status,
        action="history_query",
        answer=response.get("answer") or "",
        table=response.get("table"),
        source="history_mysql",
        execution_source="test_double",
        filters=params,
        trace=[{"action": "history_query", "status": status}],
        model_calls=0,
        query_calls=(response.get("counts") or {}).get("queries") or 0,
        usage=response.get("usage"),
        error=response.get("error"),
    )


def _real_history(request, question, model_client, history_tool):
    """Real history route: strict JSON plan, then the real named-query tool.

    There is deliberately no MockTool/FakeClock fallback.  Missing config,
    invalid plans, and database errors are returned as errors.
    """
    if model_client is None:
        try:
            from mes_assistant.llm_client import ChatModelClient
            from mes_assistant.model_factory import build_model_config

            model_client = ChatModelClient(config=build_model_config())
        except Exception as exc:
            code = getattr(exc, "code", None) or "MODEL_CONFIG_MISSING"
            return _result(
                request,
                status="error",
                action="history_query",
                answer="real 历史查询缺少模型配置，未执行；不会回退离线替身。",
                source="history_mysql",
                execution_source=None,
                issues=[code],
                error={"code": code},
                trace=[{"action": "history_query", "status": "error"}],
            )
    prompt = (
        "只输出一个 JSON 对象，不要 markdown。允许字段：action/query_id/params/issue。\n"
        "action 只能是 query 或 clarify。query_id 只能是 line_snapshot、"
        "work_order_progress、downtime_details。\n"
        "work_order_progress 必须从用户原文明确 line_id（1/2/3），不得默认1。\n"
        "line_snapshot 的 params 为空。downtime_details 只在用户明确停机明细时使用。\n"
        "用户问题：" + question
    )
    try:
        reply = model_client.complete(
            [
                {"role": "system", "content": "你是受控查询路由器，只输出严格 JSON。"},
                {"role": "user", "content": prompt},
            ],
            timeout_ms=10000,
        )
        plan = json.loads(reply.content)
    except Exception as exc:
        code = getattr(exc, "code", None) or "MODEL_ROUTE_FAILED"
        return _result(
            request,
            status="error",
            action="history_query",
            answer="real 历史路由失败；未执行 mock 回退。",
            source="history_mysql",
            execution_source=None,
            issues=[code],
            error={"code": code},
            trace=[{"action": "history_query", "status": "error"}],
        )
    if not isinstance(plan, dict) or plan.get("action") not in ("query", "clarify"):
        code = "PLAN_INVALID"
        return _result(
            request,
            status="error",
            action="history_query",
            answer="real 路由计划无效；未执行 mock 回退。",
            source="history_mysql",
            execution_source=None,
            issues=[code],
            error={"code": code},
            model_calls=1,
            usage=getattr(reply, "usage", None),
            trace=[{"action": "history_query", "status": "error"}],
        )
    if plan.get("action") == "clarify":
        return _result(
            request,
            status="needs_clarification",
            action="history_query",
            answer="请补充指标或明确产线/时间范围。",
            issues=[str(plan.get("issue") or "CLARIFY")],
            source="history_mysql",
            execution_source="model_router",
            model_calls=1,
            usage=getattr(reply, "usage", None),
            trace=[{"action": "history_query", "status": "needs_clarification"}],
        )
    query_id = plan.get("query_id")
    params = plan.get("params")
    if query_id not in ("line_snapshot", "work_order_progress", "downtime_details"):
        code = "PLAN_INVALID"
        return _result(
            request,
            status="error",
            action="history_query",
            answer="real 路由计划 query_id 无效。",
            source="history_mysql",
            execution_source=None,
            issues=[code],
            error={"code": code},
            model_calls=1,
            usage=getattr(reply, "usage", None),
            trace=[{"action": "history_query", "status": "error"}],
        )
    if not isinstance(params, dict):
        code = "PLAN_INVALID"
        return _result(
            request,
            status="error",
            action="history_query",
            answer="real 路由计划 params 无效。",
            source="history_mysql",
            execution_source=None,
            issues=[code],
            error={"code": code},
            model_calls=1,
            usage=getattr(reply, "usage", None),
            trace=[{"action": "history_query", "status": "error"}],
        )
    if query_id == "work_order_progress" and params.get("line_id") not in (1, 2, 3):
        return _result(
            request,
            status="needs_clarification",
            action="history_query",
            answer="请给出明确的 1/2/3 号产线。",
            issues=["MISSING_LINE_NO"],
            source="history_mysql",
            execution_source="model_router",
            model_calls=1,
            usage=getattr(reply, "usage", None),
            trace=[{"action": "history_query", "status": "needs_clarification"}],
        )
    try:
        from mes_assistant.query_runtime import MonotonicClock, run_query

        tool = history_tool
        if tool is None:
            from mes_assistant.mysql_tools import run_named_query

            tool = run_named_query
        base = json.loads(BASE_REQUEST.read_text(encoding="utf-8"))
        qrequest = presenter.build_question_request(
            base, question, request["reference_time"]
        )
        qrequest["budget"] = dict(qrequest["budget"])
        qrequest["budget"]["max_queries"] = 2
        response = run_query(
            qrequest,
            query_id=query_id,
            params=params,
            tool=tool,
            clock=MonotonicClock(),
            service_limits=qrequest["budget"],
            remaining_timeout_ms=qrequest["budget"]["timeout_ms"],
            mode="real",
            log_sink=io.StringIO(),
        )
    except Exception as exc:
        code = getattr(exc, "code", None) or "QUERY_FAILED"
        return _result(
            request,
            status="error",
            action="history_query",
            answer="real 历史查询失败；不会回退 mock。",
            source="history_mysql",
            execution_source=None,
            issues=[code],
            error={"code": code},
            model_calls=1,
            usage=getattr(reply, "usage", None),
            trace=[{"action": "history_query", "status": "error"}],
        )
    status = response.get("status", "error")
    if status == "ok":
        status = "success"
    return _result(
        request,
        status=status,
        action="history_query",
        answer=response.get("answer") or "",
        table=response.get("table"),
        source="history_mysql",
        execution_source="http_model",
        filters=params,
        trace=[{"action": "history_query", "status": status}],
        model_calls=1,
        query_calls=(response.get("counts") or {}).get("queries") or 0,
        usage=response.get("usage") or getattr(reply, "usage", None),
        error=response.get("error"),
    )


def _prepare(request, question, store, model_client, mode):
    cleaned = re.sub(r"^(请|帮我)?\s*(录入|登记|新增|新建)\s*(一条|停机)?\s*", "", question).strip()
    if not cleaned:
        cleaned = question
    result = run_intake_request(
        cleaned,
        reference_time=request["reference_time"],
        mode=mode,
        model_client=model_client,
        env=request.get("env"),
    )
    draft = result["draft"]
    run = result["run"]
    status = {
        "ready": "success",
        "needs_clarification": "needs_clarification",
        "refused": "refused",
        "error": "error",
    }.get(draft.get("state"), "error")
    return _result(
        request,
        status=status,
        action="prepare_intake",
        answer="已生成停机草稿；确认前不写库。" if status == "success" else "草稿需补充或已拒绝。",
        draft=draft,
        source="intake_extraction",
        execution_source=run.get("execution_source"),
        issues=draft.get("issues") or [],
        trace=[{"action": "prepare_intake", "status": status, "run": run}],
        model_calls=run.get("client_calls") or 0,
        query_calls=0,
        usage=run.get("usage"),
        error={"code": run.get("error_code")} if run.get("error_code") else None,
    )


def _intake_query(request, question, store, reference):
    record = _record_id(question)
    if any(word in question for word in ("刚录入", "刚确认", "刚才", "刚刚")):
        record = request.get("last_confirmed_record_id")
        if record is None:
            issue = (
                "UNCONFIRMED_DRAFT"
                if request.get("draft_record_id") is not None or "草稿" in question
                else "NO_CONFIRMED_RECORD"
            )
            return _result(
                request,
                status="needs_clarification",
                action="clarify",
                answer="当前会话还没有已确认的录入记录；请先确认写入，或给出明确时间窗。",
                issues=[issue],
                source=None,
                execution_source="program_guard",
                trace=[{"action": "clarify", "status": "needs_clarification"}],
            )
    machine, machine_error = _machine(question)
    if machine_error:
        return _result(
            request,
            status="needs_clarification",
            action="clarify",
            answer="设备不唯一或不在受控白名单（M01/M02/M03），请明确一个设备。",
            issues=[machine_error],
            source=None,
            execution_source="program_guard",
            trace=[{"action": "clarify", "status": "needs_clarification"}],
        )
    window = None
    if record is None:
        window = _window_from_question(question, reference)
        if window is None:
            return _result(
                request,
                status="needs_clarification",
                action="clarify",
                answer="请给出完整时间窗（今天/本周或明确日期）或已确认的记录 ID。",
                issues=["MISSING_WINDOW"],
                source=None,
                execution_source="program_guard",
                trace=[{"action": "clarify", "status": "needs_clarification"}],
            )
    try:
        limit_match = re.search(r"(?:最多(?:展示)?|limit)\s*(\d+)\s*条?", question, re.IGNORECASE)
        limit = int(limit_match.group(1)) if limit_match else 200
        if record is not None:
            table = store.search_window(record_id=record, machine_no=machine, limit=limit)
            filters = {"record_id": record, "machine_no": machine, "limit": limit}
        else:
            table = store.search_window(
                window_start=window[0], window_end=window[1], machine_no=machine, limit=limit
            )
            filters = {
                "window_start": window[0],
                "window_end": window[1],
                "machine_no": machine,
                "limit": limit,
            }
    except Exception as exc:
        return _result(
            request,
            status="error",
            action="intake_query",
            answer="录入记录查询失败。",
            source="intake_sqlite",
            execution_source="offline_sqlite",
            error={"code": getattr(exc, "code", "STORE_UNAVAILABLE")},
            trace=[{"action": "intake_query", "status": "error"}],
        )
    return _result(
        request,
        status="success",
        action="intake_query",
        answer="查到 %d 条录入记录。" % len(table.get("rows") or []),
        table=table,
        source="intake_sqlite",
        execution_source="offline_sqlite",
        filters=filters,
        record_id=record,
        trace=[{"action": "intake_query", "status": "success"}],
        query_calls=1,
    )


class WorkspaceService:
    def __init__(self, store=None, *, model_client=None, history_tool=None):
        self.store = store
        self.model_client = model_client
        self.history_tool = history_tool
        self.drafts = {}

    def run(self, request):
        if not isinstance(request, dict):
            raise WorkspaceError("INPUT_INVALID")
        if not isinstance(request.get("session_id"), str) or not request["session_id"].strip():
            raise WorkspaceError("INPUT_INVALID")
        if not isinstance(request.get("question"), str) or not request["question"].strip():
            raise WorkspaceError("INPUT_INVALID")
        mode = request.get("mode", "offline")
        if mode not in ("offline", "real"):
            raise WorkspaceError("INPUT_INVALID")
        reference = _reference(request)
        question = request["question"].strip()
        kind = _kind(question)
        if kind == "refuse":
            return _result(
                request,
                status="refused",
                action="refuse",
                answer="该请求超出只读、受控范围，不能执行。",
                issues=["OUT_OF_SCOPE"],
                source=None,
                execution_source="program_guard",
                trace=[{"action": "refuse", "status": "refused"}],
                query_calls=0,
            )
        if kind == "cross_source":
            return _result(
                request,
                status="needs_clarification",
                action="clarify",
                answer="历史 MySQL 与刚录入 SQLite 不能直接相加；请分开查询。",
                issues=["CROSS_SOURCE_MIX"],
                source=None,
                execution_source="program_guard",
                trace=[{"action": "clarify", "status": "needs_clarification"}],
            )
        if kind == "prepare_intake":
            result = _prepare(request, question, self.store, self.model_client, mode)
            draft = result.get("draft")
            if isinstance(draft, dict) and draft.get("draft_id"):
                snapshot = copy.deepcopy(draft)
                version = uuid.uuid4().hex
                snapshot["version"] = version
                result["draft"] = copy.deepcopy(snapshot)
                self.drafts[draft["draft_id"]] = {
                    "draft": snapshot,
                    "source": question,
                    "session_id": request["session_id"],
                    "version": version,
                }
            return result
        if kind == "intake_query":
            return _intake_query(request, question, self.store, reference)
        return _history(request, question, self.model_client, self.history_tool)

    def confirm(self, draft, *, session_id=None, source_text=None, submission_key=None):
        draft_id = draft.get("draft_id") if isinstance(draft, dict) else draft
        entry = self.drafts.get(draft_id)
        if not isinstance(entry, dict):
            raise WorkspaceError("DRAFT_NOT_REGISTERED")
        if session_id is not None and session_id != entry["session_id"]:
            raise WorkspaceError("DRAFT_SESSION_MISMATCH")
        if source_text is not None and source_text != entry["source"]:
            raise WorkspaceError("STALE_DRAFT")
        snapshot = entry["draft"]
        if isinstance(draft, dict):
            if draft.get("version") != entry["version"]:
                raise WorkspaceError("DRAFT_VERSION_MISMATCH")
            if draft.get("record") != snapshot.get("record"):
                raise WorkspaceError("DRAFT_VERSION_MISMATCH")
            if draft.get("evidence") != snapshot.get("evidence"):
                raise WorkspaceError("DRAFT_VERSION_MISMATCH")
        if snapshot.get("state") != "ready":
            raise WorkspaceError("DRAFT_NOT_READY")
        if classify_multi_event(entry["source"]):
            raise WorkspaceError("MULTIPLE_EVENTS")
        record = snapshot.get("record") or {}
        normalized, issues = validate_record(record)
        if issues:
            raise WorkspaceError("DRAFT_INVALID")
        store = self.store or DowntimeStore()
        receipt = store.commit(normalized, submission_key=submission_key or draft_id)
        entry["confirmed"] = True
        return receipt

    def prepare_queue(self, text, *, request):
        lines = [line.strip() for line in str(text).splitlines() if line.strip()]
        if len(lines) > MAX_QUEUE_LINES:
            raise WorkspaceError("QUEUE_LIMIT")
        drafts = []
        for index, line in enumerate(lines, 1):
            sub = dict(request)
            sub["request_id"] = "%s-q%d" % (request.get("request_id", "queue"), index)
            sub["question"] = "录入 " + line
            result = self.run(sub)
            drafts.append(
                {
                    "index": index,
                    "text": line,
                    "source": sub["question"],
                    "status": result["status"],
                    "draft": result.get("draft"),
                    "issues": result.get("issues") or [],
                }
            )
        return drafts


def confirm_request(service, draft, *, session_id=None, source_text=None, submission_key=None):
    return service.confirm(
        draft,
        session_id=session_id,
        source_text=source_text,
        submission_key=submission_key,
    )
