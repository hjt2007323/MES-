"""A-004 M1 命名查询运行时。

只调用一次 B-004 公开命名工具，把工具 DTO 映射为 schema 0.1 r3 结果，
生成确定性解释并写一条脱敏 JSONL 日志；不接模型、不执行任意 SQL、不重试。
"""

import copy
import re
import time
from datetime import datetime

from mes_assistant import query_catalog
from mes_assistant.service import ContractInputError, _valid_cell, validate_request
from mes_assistant.structured_log import write_event

DATA_VERSION = query_catalog.DATA_VERSION
MODES = ("mock", "real")

_SERVICE_LIMIT_KEYS = (
    "timeout_ms",
    "max_model_calls",
    "max_queries",
    "max_corrections",
)
_WINDOW_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$"
)

_FIXED_MESSAGES = {
    "OPERATION_TIMEOUT": "超过允许时限",
    "BUDGET_EXHAUSTED": "预算不足，无法完成两次必要查询",
    "TOOL_FAILED": "查询工具调用失败",
    "TOOL_RESULT_INVALID": "查询工具返回结构不符合约定",
    "DATA_VERSION_MISMATCH": "查询结果的数据版本与请求不一致",
    "LOG_WRITE_FAILED": "结构化日志写入失败",
}
_REFUSED_ANSWER = (
    "查询被拒绝：命名查询参数或数据范围未被允许（synthetic，"
    "review_status=pending）。"
)


class MonotonicClock:
    """生产用单调毫秒时钟；测试可注入带 now_ms() 的 fake。"""

    def now_ms(self) -> int:
        return time.monotonic_ns() // 1_000_000


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _fixed_error(code, stage):
    return {
        "code": code,
        "stage": stage,
        "retryable": False,
        "message": _FIXED_MESSAGES[code],
    }


def _validate_service_limits(service_limits):
    if not isinstance(service_limits, dict):
        raise ContractInputError("service_limits must be an object")
    for key in _SERVICE_LIMIT_KEYS:
        if key not in service_limits:
            raise ContractInputError("missing service limit: %s" % key)
        value = service_limits[key]
        if not _is_int(value):
            raise ContractInputError("service limit must be an integer")
        if key == "timeout_ms":
            if value <= 0:
                raise ContractInputError("service timeout must be positive")
        elif value < 0:
            raise ContractInputError("service limit must be non-negative")


def _parse_window(value):
    if not isinstance(value, str) or _WINDOW_PATTERN.match(value) is None:
        raise ContractInputError("window time must be exact ISO +08:00")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S+08:00")
    except ValueError:
        raise ContractInputError("window time is not a valid calendar value") from None


def _validate_params(description, params):
    if not isinstance(params, dict):
        raise ContractInputError("params must be an object")
    if set(params) != set(description["params"]):
        raise ContractInputError("params keys do not match the named query")
    query_id = description["query_id"]
    if query_id == "work_order_progress":
        line_id = params["line_id"]
        if not _is_int(line_id) or not 1 <= line_id <= 2147483647:
            raise ContractInputError("line_id must be an integer in 1..2147483647")
    elif query_id == "downtime_details":
        start = _parse_window(params["window_start"])
        end = _parse_window(params["window_end"])
        if not start < end:
            raise ContractInputError("window_start must be earlier than window_end")


def validate_query_input(
    request,
    *,
    query_id,
    params,
    service_limits,
    remaining_timeout_ms,
    mode,
):
    """校验 A-004 命名查询输入；返回目录说明，不调用工具、不写日志。"""
    if mode not in MODES:
        raise ContractInputError("mode must be mock or real")
    if not isinstance(request, dict):
        raise ContractInputError("request must be an object")
    validate_request(request)
    if request.get("data_version") != DATA_VERSION:
        raise ContractInputError("request data_version must be %s" % DATA_VERSION)
    description = query_catalog.describe_query(query_id)
    _validate_params(description, params)
    _validate_service_limits(service_limits)
    if not _is_int(remaining_timeout_ms):
        raise ContractInputError("remaining_timeout_ms must be an integer")
    return description


def _extract_queries(dto):
    if isinstance(dto, dict):
        counts = dto.get("counts")
        if counts is None:
            return None
        if isinstance(counts, dict):
            value = counts.get("queries")
            if value is None:
                return None
            if _is_int(value) and value in (0, 1, 2):
                return value
    return None


def _counts_valid(dto):
    if not isinstance(dto, dict):
        return False
    counts = dto.get("counts")
    if counts is None:
        return True
    if not isinstance(counts, dict):
        return False
    value = counts.get("queries")
    return value is None or (_is_int(value) and value in (0, 1, 2))


def _valid_error(value):
    if not isinstance(value, dict):
        return False
    for key in ("code", "stage", "message", "retryable"):
        if key not in value:
            return False
    if not isinstance(value["code"], str) or value["code"] == "":
        return False
    if value["stage"] != "query":
        return False
    if not isinstance(value["message"], str) or value["message"] == "":
        return False
    return value["retryable"] is False


def _copy_error(value):
    return {
        "code": value["code"],
        "stage": "query",
        "retryable": False,
        "message": value["message"],
    }


def _valid_table_for_query(table, expected_columns):
    """按该 query_id 冻结的列定义和 r3 值规则校验成功表。"""
    if not isinstance(table, dict):
        return False
    for key in ("columns", "rows", "truncated", "truncation_reason"):
        if key not in table:
            return False
    columns = table["columns"]
    rows = table["rows"]
    truncated = table["truncated"]
    reason = table["truncation_reason"]
    if not isinstance(columns, list) or not isinstance(rows, list):
        return False
    if not isinstance(truncated, bool):
        return False
    if truncated:
        if not isinstance(reason, str) or reason.strip() == "":
            return False
    elif reason is not None and not isinstance(reason, str):
        return False
    if len(columns) != len(expected_columns):
        return False
    for column, expected in zip(columns, expected_columns):
        if not isinstance(column, dict):
            return False
        if column.get("name") != expected["name"]:
            return False
        if column.get("type") != expected["type"]:
            return False
        if column.get("nullable") is not expected["nullable"]:
            return False
    for row in rows:
        if not isinstance(row, list) or len(row) != len(expected_columns):
            return False
        for value, expected in zip(row, expected_columns):
            if value is None:
                if not expected["nullable"]:
                    return False
            elif not _valid_cell(expected["type"], value):
                return False
    return True


def _format_value(value):
    if value is None:
        return "未知"
    return str(value)


def _build_answer(description, table):
    formula = description["formula"].rstrip("。")
    parts = [
        "%s。" % description["title"],
        "候选公式：%s；单位：%s。" % (formula, description["unit"]),
        "数据版本：%s（synthetic）；review_status=pending，非已审核业务答案。"
        % DATA_VERSION,
    ]
    rows = table["rows"]
    count = len(rows)
    if count == 0:
        parts.append("返回 0 行（空表，属 ok）。")
    elif table["truncated"]:
        parts.append("已返回 %d 行，结果被截断（非完整结果）。" % count)
    else:
        parts.append("返回 %d 行。" % count)
    if rows:
        names = [column["name"] for column in table["columns"]]
        pairs = [
            "%s=%s" % (name, _format_value(value))
            for name, value in zip(names, rows[0])
        ]
        parts.append("首行：" + "；".join(pairs) + "。")
    if description.get("answer_note"):
        parts.append(description["answer_note"])
    return "".join(parts)


def _trace_entry(clock, dispatch_start_ms, status, error_code=None):
    entry = {
        "event_id": "1",
        "stage": "query",
        "operation": "query",
        "status": status,
        "duration_ms": clock.now_ms() - dispatch_start_ms,
    }
    if error_code is not None:
        entry["error_code"] = error_code
    return [entry]


def _build_result(
    request,
    *,
    mode,
    status,
    answer,
    table,
    sql,
    error,
    counts,
    trace,
    description,
    internal_ms,
):
    return {
        "schema_version": request["schema_version"],
        "run_id": request["run_id"],
        "request_id": request["request_id"],
        "case_id": request["case_id"],
        "data_version": request["data_version"],
        "reference_time": request["reference_time"],
        "timezone": request["timezone"],
        "mode": mode,
        "status": status,
        "answer": answer,
        "table": table,
        "sql": sql,
        "metrics": [
            {
                "id": description["metric_id"],
                "definition_version": description["definition_version"],
            }
        ],
        "error": error,
        "timing": {"internal_ms": internal_ms},
        "counts": dict(counts),
        "usage": None,
        "config_id": request["config_id"],
        "query_id": description["query_id"],
        "review_status": "pending",
        "trace": trace,
    }


def run_query(
    request: dict,
    *,
    query_id: str,
    params: dict,
    tool,
    clock,
    service_limits: dict,
    remaining_timeout_ms: int,
    mode: str,
    log_sink,
) -> dict:
    """一次命名查询：一次工具调用、r3 映射、确定性解释与一条日志。"""
    description = validate_query_input(
        request,
        query_id=query_id,
        params=params,
        service_limits=service_limits,
        remaining_timeout_ms=remaining_timeout_ms,
        mode=mode,
    )
    if not callable(tool):
        raise ContractInputError("tool must be callable")

    budget = request["budget"]
    effective_timeout_ms = min(budget["timeout_ms"], service_limits["timeout_ms"])
    max_queries = min(budget["max_queries"], service_limits["max_queries"])
    start_ms = clock.now_ms()
    tool_dispatch_start_ms = None

    def build(
        status,
        *,
        answer=None,
        table=None,
        sql=None,
        error=None,
        queries=0,
        trace=None,
    ):
        return _build_result(
            request,
            mode=mode,
            status=status,
            answer=answer,
            table=table,
            sql=sql,
            error=error,
            counts={
                "model_calls": 0,
                "queries": queries,
                "corrections": 0,
            },
            trace=[] if trace is None else trace,
            description=description,
            internal_ms=clock.now_ms() - start_ms,
        )

    def log_and_return(result):
        # F1：解释/校验/封装也消耗总预算；写终态日志前复检 deadline。
        if tool_dispatch_start_ms is not None and clock.now_ms() >= deadline:
            result = build(
                "timeout",
                error=_fixed_error("OPERATION_TIMEOUT", "query"),
                queries=result["counts"]["queries"],
                trace=_trace_entry(
                    clock, tool_dispatch_start_ms, "timeout", "OPERATION_TIMEOUT"
                ),
            )
        event = {
            "run_id": request["run_id"],
            "request_id": request["request_id"],
            "query_id": query_id,
            "config_id": request["config_id"],
            "data_version": result["data_version"],
            "status": result["status"],
            "error_code": result["error"]["code"] if result["error"] else None,
            "counts": result["counts"],
            "duration_ms": result["timing"]["internal_ms"],
            "review_status": "pending",
        }
        try:
            write_event(log_sink, event)
        except Exception:
            return build(
                "error",
                error=_fixed_error("LOG_WRITE_FAILED", "query"),
                queries=result["counts"]["queries"],
                trace=result["trace"],
            )
        return result

    if remaining_timeout_ms <= 0:
        return log_and_return(
            build(
                "timeout",
                error=_fixed_error("OPERATION_TIMEOUT", "input"),
                queries=0,
            )
        )
    deadline = start_ms + min(effective_timeout_ms, remaining_timeout_ms)
    if clock.now_ms() >= deadline:
        return log_and_return(
            build(
                "timeout",
                error=_fixed_error("OPERATION_TIMEOUT", "input"),
                queries=0,
            )
        )
    if max_queries < 2:
        return log_and_return(
            build(
                "budget_exhausted",
                error=_fixed_error("BUDGET_EXHAUSTED", "query"),
                queries=0,
            )
        )

    dispatch_start_ms = clock.now_ms()
    remaining_ms = deadline - dispatch_start_ms
    if remaining_ms <= 0:
        return log_and_return(
            build(
                "timeout",
                error=_fixed_error("OPERATION_TIMEOUT", "input"),
                queries=0,
            )
        )
    tool_dispatch_start_ms = dispatch_start_ms
    try:
        dto = tool(
            query_id,
            copy.deepcopy(params),
            remaining_timeout_ms=remaining_ms,
            max_rows=200,
        )
    except TimeoutError:
        return log_and_return(
            build(
                "timeout",
                error=_fixed_error("OPERATION_TIMEOUT", "query"),
                queries=None,
                trace=_trace_entry(
                    clock, dispatch_start_ms, "timeout", "OPERATION_TIMEOUT"
                ),
            )
        )
    except Exception:
        if clock.now_ms() >= deadline:
            return log_and_return(
                build(
                    "timeout",
                    error=_fixed_error("OPERATION_TIMEOUT", "query"),
                    queries=None,
                    trace=_trace_entry(
                        clock, dispatch_start_ms, "timeout", "OPERATION_TIMEOUT"
                    ),
                )
            )
        return log_and_return(
            build(
                "error",
                error=_fixed_error("TOOL_FAILED", "query"),
                queries=None,
                trace=_trace_entry(clock, dispatch_start_ms, "error", "TOOL_FAILED"),
            )
        )

    queries = _extract_queries(dto)
    if clock.now_ms() >= deadline:
        return log_and_return(
            build(
                "timeout",
                error=_fixed_error("OPERATION_TIMEOUT", "query"),
                queries=queries,
                trace=_trace_entry(
                    clock, dispatch_start_ms, "timeout", "OPERATION_TIMEOUT"
                ),
            )
        )
    if not isinstance(dto, dict):
        return log_and_return(
            build(
                "error",
                error=_fixed_error("TOOL_RESULT_INVALID", "query"),
                queries=None,
                trace=_trace_entry(
                    clock, dispatch_start_ms, "error", "TOOL_RESULT_INVALID"
                ),
            )
        )
    status = dto.get("status")
    if status not in ("ok", "refused", "error", "timeout"):
        return log_and_return(
            build(
                "error",
                error=_fixed_error("TOOL_RESULT_INVALID", "query"),
                queries=queries,
                trace=_trace_entry(
                    clock, dispatch_start_ms, "error", "TOOL_RESULT_INVALID"
                ),
            )
        )
    if dto.get("query_id") is not None and dto.get("query_id") != query_id:
        return log_and_return(
            build(
                "error",
                error=_fixed_error("TOOL_RESULT_INVALID", "query"),
                queries=queries,
                trace=_trace_entry(
                    clock, dispatch_start_ms, "error", "TOOL_RESULT_INVALID"
                ),
            )
        )
    dto_version = dto.get("data_version")
    if not isinstance(dto_version, str):
        return log_and_return(
            build(
                "error",
                error=_fixed_error("TOOL_RESULT_INVALID", "query"),
                queries=queries,
                trace=_trace_entry(
                    clock, dispatch_start_ms, "error", "TOOL_RESULT_INVALID"
                ),
            )
        )
    if dto_version != request["data_version"]:
        return log_and_return(
            build(
                "error",
                error=_fixed_error("DATA_VERSION_MISMATCH", "query"),
                queries=queries,
                trace=_trace_entry(
                    clock, dispatch_start_ms, "error", "DATA_VERSION_MISMATCH"
                ),
            )
        )
    if not _counts_valid(dto):
        return log_and_return(
            build(
                "error",
                error=_fixed_error("TOOL_RESULT_INVALID", "query"),
                queries=None,
                trace=_trace_entry(
                    clock, dispatch_start_ms, "error", "TOOL_RESULT_INVALID"
                ),
            )
        )

    if status == "ok":
        if dto.get("error") is not None:
            return log_and_return(
                build(
                    "error",
                    error=_fixed_error("TOOL_RESULT_INVALID", "query"),
                    queries=queries,
                    trace=_trace_entry(
                        clock, dispatch_start_ms, "error", "TOOL_RESULT_INVALID"
                    ),
                )
            )
        table = dto.get("table")
        sql = dto.get("sql")
        if (
            not _valid_table_for_query(table, description["columns"])
            or not isinstance(sql, str)
            or sql == ""
        ):
            return log_and_return(
                build(
                    "error",
                    error=_fixed_error("TOOL_RESULT_INVALID", "query"),
                    queries=queries,
                    trace=_trace_entry(
                        clock, dispatch_start_ms, "error", "TOOL_RESULT_INVALID"
                    ),
                )
            )
        return log_and_return(
            build(
                "ok",
                answer=_build_answer(description, table),
                table=table,
                sql=sql,
                queries=queries,
                trace=_trace_entry(clock, dispatch_start_ms, "ok"),
            )
        )

    if status == "refused":
        if dto.get("error") is not None:
            return log_and_return(
                build(
                    "error",
                    error=_fixed_error("TOOL_RESULT_INVALID", "query"),
                    queries=queries,
                    trace=_trace_entry(
                        clock, dispatch_start_ms, "error", "TOOL_RESULT_INVALID"
                    ),
                )
            )
        return log_and_return(
            build(
                "refused",
                answer=_REFUSED_ANSWER,
                queries=queries,
                trace=_trace_entry(clock, dispatch_start_ms, "ok"),
            )
        )

    error_value = dto.get("error")
    if not _valid_error(error_value):
        return log_and_return(
            build(
                "error",
                error=_fixed_error("TOOL_RESULT_INVALID", "query"),
                queries=queries,
                trace=_trace_entry(
                    clock, dispatch_start_ms, "error", "TOOL_RESULT_INVALID"
                ),
            )
        )
    mapped_error = _copy_error(error_value)
    trace_status = "error" if status == "error" else "timeout"
    return log_and_return(
        build(
            status,
            error=mapped_error,
            queries=queries,
            trace=_trace_entry(
                clock, dispatch_start_ms, trace_status, mapped_error["code"]
            ),
        )
    )
