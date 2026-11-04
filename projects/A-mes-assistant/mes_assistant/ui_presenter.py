"""A-005 Streamlit 页面的纯处理/展示数据层。

这里不复制 M1 状态机、SQL 或业务指标：真实查询仍由 query_runtime.run_query
和 B 的 run_named_query 完成；本模块只负责场景参数、单次提交和展示数据整理。
"""

from __future__ import annotations

import copy
import json
import uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from mes_assistant.query_runtime import MonotonicClock, run_query

DATA_VERSION = "mes-a07-boundary-v1-pending"
DEFAULT_WINDOW_START = "2026-11-01T00:00:00+08:00"
DEFAULT_WINDOW_END = "2026-11-02T00:00:00+08:00"

SCENARIOS = (
    {
        "label": "产线产量快照",
        "query_id": "line_snapshot",
        "hint": "当前累计快照，不做日增量；柱状图直接展示 B 返回的 actual_qty。",
    },
    {
        "label": "工单数量完成比",
        "query_id": "work_order_progress",
        "hint": "输入 line_id；比率由 B 返回，NULL 显示未知，不在页面另算。",
    },
    {
        "label": "设备停机明细",
        "query_id": "downtime_details",
        "hint": "输入 [window_start, window_end)；原始事件可保留 NULL/重叠/反向/零长度。",
    },
)
SCENARIO_BY_LABEL = {item["label"]: item for item in SCENARIOS}
SCENARIO_BY_QUERY = {item["query_id"]: item for item in SCENARIOS}


def scenario(query_id: str) -> dict:
    """返回场景元数据；未知 query_id 直接 KeyError。"""
    return SCENARIO_BY_QUERY[query_id]


def new_request(base_request: dict, query_id: str) -> dict:
    """复制基础 request，并生成一次提交唯一的 request_id。"""
    request = copy.deepcopy(base_request)
    request["request_id"] = "ui-" + uuid.uuid4().hex
    request["case_id"] = query_id
    return request


def build_params(
    query_id: str,
    *,
    line_id: int = 1,
    window_start: str = DEFAULT_WINDOW_START,
    window_end: str = DEFAULT_WINDOW_END,
) -> dict:
    """按页面输入构造 G1 参数；只做类型/空白归一，合法性交给 run_query。"""
    if query_id == "line_snapshot":
        return {}
    if query_id == "work_order_progress":
        return {"line_id": int(line_id)}
    if query_id == "downtime_details":
        return {
            "window_start": str(window_start).strip(),
            "window_end": str(window_end).strip(),
        }
    raise KeyError(query_id)


def run_submission(query_fn, request: dict, query_id: str, params: dict, *, log_sink):
    """调用可注入的查询函数恰好一次；页面只在 form submit 时调用本函数。"""
    if not callable(query_fn):
        raise TypeError("query_fn must be callable")
    return query_fn(
        request,
        query_id,
        copy.deepcopy(params),
        log_sink=log_sink,
    )


def real_query(request: dict, query_id: str, params: dict, *, log_sink) -> dict:
    """真实 M1 接线：query_runtime.run_query + B run_named_query，mode=real。"""
    from mes_assistant.mysql_tools import run_named_query

    return run_query(
        request,
        query_id=query_id,
        params=params,
        tool=run_named_query,
        clock=MonotonicClock(),
        service_limits=request["budget"],
        remaining_timeout_ms=request["budget"]["timeout_ms"],
        mode="real",
        log_sink=log_sink,
    )


@contextmanager
def open_log_sink(path):
    """页面日志打开点；默认行为不变，测试可 patch 此函数定向 test-tmp。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as sink:
        yield sink


def _decimal_for_chart(value):
    if value is None:
        return None
    try:
        number = float(Decimal(str(value)))
    except Exception:
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def present_result(response: dict) -> dict:
    """把 M1 r3 响应整理成页面需要的展示数据；不计算新业务指标。"""
    if not isinstance(response, dict):
        return {
            "status": "error",
            "mode": "unknown",
            "query_id": "",
            "request_id": "",
            "data_version": DATA_VERSION,
            "answer": "",
            "sql": "",
            "columns": [],
            "display_rows": [],
            "empty": True,
            "truncated": False,
            "truncation_reason": None,
            "chart": [],
            "trace": [],
            "counts": {},
            "error": {
                "code": "UI_RESPONSE_INVALID",
                "stage": "ui",
                "message": "页面收到非对象响应",
                "retryable": False,
            },
            "error_text": "UI_RESPONSE_INVALID: 页面收到非对象响应",
            "review_status": "pending",
            "semantic_policy_version": None,
            "context_mode": None,
            "context_evidence": None,
            "plan": None,
            "synthetic": True,
            "is_real": False,
            "source": "invalid",
        }

    status = response.get("status")
    query_id = response.get("query_id") or ""
    table = response.get("table")
    columns = []
    rows = []
    truncated = False
    reason = None
    if isinstance(table, dict):
        raw_columns = table.get("columns")
        if isinstance(raw_columns, list):
            columns = [
                column.get("name")
                for column in raw_columns
                if isinstance(column, dict) and isinstance(column.get("name"), str)
            ]
        raw_rows = table.get("rows")
        if isinstance(raw_rows, list):
            rows = [row for row in raw_rows if isinstance(row, list)]
        truncated = bool(table.get("truncated"))
        reason = table.get("truncation_reason")
    display_rows = [
        ["未知" if value is None else value for value in row]
        for row in rows
    ]

    chart = []
    if query_id == "line_snapshot" and status == "ok" and columns:
        try:
            line_index = columns.index("line_no")
            qty_index = columns.index("actual_qty")
        except ValueError:
            line_index = qty_index = None
        if line_index is not None and qty_index is not None:
            for row in rows:
                if len(row) <= max(line_index, qty_index):
                    continue
                value = _decimal_for_chart(row[qty_index])
                if value is None:
                    continue
                chart.append(
                    {"line_no": row[line_index], "actual_qty": value}
                )

    error = response.get("error")
    if not isinstance(error, dict):
        error = None
    error_text = ""
    if error:
        error_text = "%s: %s" % (
            error.get("code", "ERROR"),
            error.get("message", ""),
        )

    review_status = response.get("review_status")
    data_version = response.get("data_version") or DATA_VERSION
    synthetic = review_status == "pending" or str(data_version).endswith("pending")
    mode = response.get("mode")
    if status == "ok" and mode == "real":
        source = "real_success"
    elif mode == "real":
        source = "real_configured"
    else:
        source = "non_real"

    return {
        "status": status,
        "mode": mode,
        "source": source,
        "query_id": query_id,
        "request_id": response.get("request_id", ""),
        "data_version": data_version,
        "answer": response.get("answer") or "",
        "sql": response.get("sql") or "",
        "columns": columns,
        "display_rows": display_rows,
        "empty": len(display_rows) == 0,
        "truncated": truncated,
        "truncation_reason": reason,
        "chart": chart,
        "trace": response.get("trace") if isinstance(response.get("trace"), list) else [],
        "counts": response.get("counts") if isinstance(response.get("counts"), dict) else {},
        "error": error,
        "error_text": error_text,
        "review_status": review_status,
        "semantic_policy_version": response.get("semantic_policy_version"),
        "context_mode": response.get("context_mode"),
        "context_evidence": (
            response.get("context_evidence")
            if isinstance(response.get("context_evidence"), dict)
            else None
        ),
        "plan": response.get("plan") if isinstance(response.get("plan"), dict) else None,
        "synthetic": synthetic,
        "is_real": status == "ok" and mode == "real",
    }


def build_question_request(base_request: dict, question: str, reference_time: str) -> dict:
    """复制 M2 基础 request，写入唯一 question request_id 与参考时间。"""
    question = str(question).strip()
    if not question:
        raise ValueError("question must not be empty")
    request = copy.deepcopy(base_request)
    request["request_id"] = "q-" + uuid.uuid4().hex
    request["case_id"] = "m2-ui"
    request["question"] = question
    request["reference_time"] = str(reference_time).strip()
    request["context_mode"] = "full"
    return request


def run_question_submission(query_fn, request: dict, *, log_sink):
    """页面只在 form submit 时经此调用查询/规划函数一次。"""
    if not callable(query_fn):
        raise TypeError("query_fn must be callable")
    return query_fn(request, log_sink=log_sink)


def mock_question(request: dict, *, log_sink) -> dict:
    """离线 mock：关键字脚本模型 + B 形 DTO 工具，不访问 DB/网络。"""
    from mes_assistant.fakes import FakeClock
    from mes_assistant.question_fakes import KeywordMockModel, MockTool
    from mes_assistant.question_runtime import run_question

    return run_question(
        request,
        model_client=KeywordMockModel(),
        tool=MockTool(),
        clock=FakeClock(),
        service_limits=request["budget"],
        remaining_timeout_ms=request["budget"]["timeout_ms"],
        mode="mock",
        log_sink=log_sink,
    )


def real_question(request: dict, *, log_sink) -> dict:
    """真实模式：统一模型工厂 + B planner + B run_named_query。"""
    from mes_assistant.llm_client import ChatModelClient
    from mes_assistant.model_factory import build_model_config
    from mes_assistant.mysql_tools import run_named_query
    from mes_assistant.query_runtime import MonotonicClock
    from mes_assistant.question_runtime import run_question

    config = build_model_config()
    return run_question(
        request,
        model_client=ChatModelClient(config=config),
        tool=run_named_query,
        clock=MonotonicClock(),
        service_limits=request["budget"],
        remaining_timeout_ms=request["budget"]["timeout_ms"],
        mode="real",
        log_sink=log_sink,
    )


def format_plan(plan) -> str:
    if not isinstance(plan, dict):
        return "—"
    kind = plan.get("kind")
    if kind == "query":
        return "query / %s / %s" % (
            plan.get("query_id") or "—",
            json.dumps(plan.get("params") or {}, ensure_ascii=False, sort_keys=True),
        )
    return str(kind) if kind else "—"


def question_view(response: dict) -> dict:
    """NL 页面视图：复用 present_result，并加上澄清/拒绝/用量标记。"""
    vm = present_result(response)
    vm["question"] = True
    vm["clarification"] = vm["status"] == "needs_clarification"
    vm["refused"] = vm["status"] == "refused"
    usage = response.get("usage") if isinstance(response, dict) else None
    vm["usage"] = usage if isinstance(usage, dict) else None
    counts = vm.get("counts") or {}
    vm["model_calls"] = counts.get("model_calls")
    vm["queries"] = counts.get("queries")
    vm["corrections"] = counts.get("corrections")
    vm["semantic_policy_version"] = (
        response.get("semantic_policy_version") if isinstance(response, dict) else None
    )
    vm["context_mode"] = response.get("context_mode") if isinstance(response, dict) else None
    vm["context_evidence"] = (
        response.get("context_evidence")
        if isinstance(response, dict) and isinstance(response.get("context_evidence"), dict)
        else None
    )
    vm["plan"] = (
        response.get("plan")
        if isinstance(response, dict) and isinstance(response.get("plan"), dict)
        else None
    )
    vm["plan_text"] = format_plan(vm["plan"])
    vm["demo_policy_confirmed"] = bool(vm["semantic_policy_version"])
    return vm

