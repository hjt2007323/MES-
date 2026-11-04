"""A-001 共用 MES 问答服务（确定性离线 mock）。

实现范围：schema 0.1 r3 的请求校验、有效预算取 min、六终态、一次纠错、
表结构校验与超时处理；结果 mode 固定 mock、usage 固定 null、metrics 恒空。

明确未实现：真实模型/数据库调用、SQL 解析与安全策略、指标计算、真实取消
入口与清理宽限、运行器/网络入口、业务黄金答案。本包所有测试都是 mock
工程验证，不能作为 MES 业务答案、SQL 安全或恢复能力的真实证据。
"""

import json
import math
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

SCHEMA_VERSION = "0.1"
MODE = "mock"

_MODEL_CALL_LIMIT_MS = 10000
_QUERY_CALL_LIMIT_MS = 3000
_MAX_EXACT_INTEGER = 9007199254740991

_TABLE_TYPES = ("string", "integer", "decimal", "float", "boolean", "date", "datetime")
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_OFFSET_PATTERN = re.compile(r"(?:Z|[+-]\d{2}:\d{2})$")

_STAGE_INPUT = "input"
_STAGE_MODEL = "model"
_STAGE_QUERY = "query"
_STAGE_FORMAT = "format"

_BUDGET_KEYS = ("timeout_ms", "max_model_calls", "max_queries", "max_corrections")

_FIXED_MESSAGES = {
    "MODEL_OUTPUT_INVALID": "模型输出不符合约定协议",
    "QUERY_RESULT_INVALID": "查询结果不符合表协议",
    "MODEL_FAILED": "模型调用失败",
    "QUERY_FAILED": "查询执行失败",
    "FAKE_SCRIPT_EXHAUSTED": "脚本事件已耗尽，未生成默认结果",
    "OPERATION_TIMEOUT": "超过允许时限",
    "BUDGET_EXHAUSTED": "预算不足，无法发起必要步骤",
    "INTERNAL_ERROR": "内部错误",
}


class ContractInputError(ValueError):
    """冻结请求、服务限额或剩余时间不符合 schema 0.1 时的输入错误。"""


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_non_empty_text(value):
    return isinstance(value, str) and value.strip() != ""


def _validate_reference_time(request):
    value = request.get("reference_time")
    if not isinstance(value, str):
        raise ContractInputError("reference_time 必须为字符串")
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, OverflowError):
        raise ContractInputError("reference_time 必须为 ISO8601 日期时间") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractInputError("reference_time 必须带 UTC 偏移")
    timezone = request.get("timezone")
    if timezone != "Asia/Shanghai" or parsed.utcoffset() != timedelta(hours=8):
        raise ContractInputError(
            "本 mock 仅接受 timezone=Asia/Shanghai 且 reference_time 偏移 +08:00；"
            "公共契约未修改，其他时区拒绝执行"
        )


def _validate_required_capabilities(request):
    capabilities = request.get("required_capabilities")
    if not isinstance(capabilities, list):
        raise ContractInputError("required_capabilities 必须为字符串数组")
    for item in capabilities:
        if not isinstance(item, str):
            raise ContractInputError("required_capabilities 只允许字符串")
    if capabilities:
        raise ContractInputError(
            "required_capabilities 必须为空数组：本包不承担能力适用性分流，"
            "非空能力列表拒绝执行（公共契约未修改）"
        )


def _validate_budget(request):
    budget = request.get("budget")
    if not isinstance(budget, dict):
        raise ContractInputError("budget 必须为对象")
    for key in _BUDGET_KEYS:
        if key not in budget:
            raise ContractInputError("budget.%s 缺失" % key)
        if not _is_int(budget[key]):
            raise ContractInputError("budget.%s 必须为整数（不接受 bool）" % key)
    if budget["timeout_ms"] <= 0:
        raise ContractInputError("budget.timeout_ms 必须大于 0")
    for key in ("max_model_calls", "max_queries", "max_corrections"):
        if budget[key] < 0:
            raise ContractInputError("budget.%s 必须大于等于 0" % key)


def _validate_service_limits(service_limits):
    if not isinstance(service_limits, dict):
        raise ContractInputError("service_limits 必须为对象")
    for key in _BUDGET_KEYS:
        if key not in service_limits:
            raise ContractInputError("service_limits.%s 缺失" % key)
        value = service_limits[key]
        if not _is_int(value):
            raise ContractInputError("service_limits.%s 必须为整数（不接受 bool）" % key)
        if key == "timeout_ms":
            if value <= 0:
                raise ContractInputError("service_limits.timeout_ms 必须大于 0")
        elif value < 0:
            raise ContractInputError("service_limits.%s 必须大于等于 0" % key)


def validate_request(request: dict) -> None:
    """校验冻结请求字段与类型；不修改输入，不调用任何依赖。"""
    if not isinstance(request, dict):
        raise ContractInputError("request 必须为对象")
    if request.get("schema_version") != SCHEMA_VERSION:
        raise ContractInputError("schema_version 必须为 %s" % SCHEMA_VERSION)
    for field in ("run_id", "request_id", "case_id", "question", "data_version", "config_id"):
        if not _is_non_empty_text(request.get(field)):
            raise ContractInputError("请求字段 %s 必须为非空字符串" % field)
    _validate_reference_time(request)
    _validate_required_capabilities(request)
    _validate_budget(request)


def _parse_model_payload(payload):
    """把模型 payload 解析为三种精确动作之一；非法返回 None。"""
    if not isinstance(payload, str):
        return None
    try:
        action = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(action, dict):
        return None
    kind = action.get("kind")
    if kind == "query":
        if set(action) != {"kind", "sql"}:
            return None
        sql = action["sql"]
        if not isinstance(sql, str) or sql == "":
            return None
        return {"kind": "query", "sql": sql}
    if kind in ("clarify", "refuse"):
        if set(action) != {"kind", "message"}:
            return None
        message = action["message"]
        if not isinstance(message, str) or message == "":
            return None
        return {"kind": kind, "message": message}
    return None


def _valid_cell(column_type, value):
    if column_type == "string":
        return isinstance(value, str)
    if column_type == "integer":
        return _is_int(value) and abs(value) <= _MAX_EXACT_INTEGER
    if column_type == "decimal":
        if not isinstance(value, str):
            return False
        try:
            parsed = Decimal(value)
        except (InvalidOperation, ValueError):
            return False
        return parsed.is_finite()
    if column_type == "float":
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        return isinstance(value, float) and math.isfinite(value)
    if column_type == "boolean":
        return isinstance(value, bool)
    if column_type == "date":
        if not isinstance(value, str) or _DATE_PATTERN.fullmatch(value) is None:
            return False
        try:
            date.fromisoformat(value)
        except (ValueError, OverflowError):
            return False
        return True
    if column_type == "datetime":
        if not isinstance(value, str) or _DATETIME_OFFSET_PATTERN.search(value) is None:
            return False
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(normalized)
        except (ValueError, OverflowError):
            return False
        return parsed.tzinfo is not None and parsed.utcoffset() is not None
    return False


def _valid_table(table):
    """按契约校验表对象；合法返回 True。"""
    if not isinstance(table, dict):
        return False
    for key in ("columns", "rows", "truncated", "truncation_reason"):
        if key not in table:
            return False
    columns = table["columns"]
    rows = table["rows"]
    truncated = table["truncated"]
    truncation_reason = table["truncation_reason"]
    if not isinstance(columns, list) or not isinstance(rows, list):
        return False
    if not isinstance(truncated, bool):
        return False
    if truncated:
        if not isinstance(truncation_reason, str) or truncation_reason.strip() == "":
            return False
    elif truncation_reason is not None and not isinstance(truncation_reason, str):
        return False
    names = set()
    for column in columns:
        if not isinstance(column, dict):
            return False
        name = column.get("name")
        column_type = column.get("type")
        nullable = column.get("nullable")
        if not isinstance(name, str) or name == "" or name in names:
            return False
        names.add(name)
        if not isinstance(column_type, str) or column_type not in _TABLE_TYPES:
            return False
        if not isinstance(nullable, bool):
            return False
    for row in rows:
        if not isinstance(row, list) or len(row) != len(columns):
            return False
        for column, value in zip(columns, row):
            if value is None:
                if column["nullable"] is not True:
                    return False
                continue
            if not _valid_cell(column["type"], value):
                return False
    return True


def _runtime_error_code(exc, default_code, allowed_codes):
    """只接受脚本约定的固定错误码，绝不回传原始异常文本。"""
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], str) and args[0] in allowed_codes:
        return args[0]
    return default_code


def ask(
    request: dict,
    *,
    model,
    executor,
    clock,
    service_limits: dict,
    remaining_timeout_ms: int,
) -> dict:
    """单次请求的确定性 mock 执行；每次调用状态独立。"""
    validate_request(request)
    _validate_service_limits(service_limits)
    if not _is_int(remaining_timeout_ms):
        raise ContractInputError("remaining_timeout_ms 必须为整数（不接受 bool）")

    start_ms = clock.now_ms()
    counts = {"model_calls": 0, "queries": 0, "corrections": 0}
    trace = []
    stage = _STAGE_INPUT
    pending_call = None

    budget = request["budget"]
    effective_timeout_ms = min(budget["timeout_ms"], service_limits["timeout_ms"])
    max_model_calls = min(budget["max_model_calls"], service_limits["max_model_calls"])
    max_queries = min(budget["max_queries"], service_limits["max_queries"])
    max_corrections = min(budget["max_corrections"], service_limits["max_corrections"], 1)

    def result(status, *, answer=None, table=None, sql=None, error=None):
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": request["run_id"],
            "request_id": request["request_id"],
            "case_id": request["case_id"],
            "data_version": request["data_version"],
            "reference_time": request["reference_time"],
            "timezone": request["timezone"],
            "mode": MODE,
            "status": status,
            "answer": answer,
            "table": table,
            "sql": sql,
            "metrics": [],
            "error": error,
            "timing": {"internal_ms": clock.now_ms() - start_ms},
            "counts": dict(counts),
            "usage": None,
            "config_id": request["config_id"],
            "trace": [dict(item) for item in trace],
        }

    def fail(status, code, error_stage):
        return result(
            status,
            error={
                "code": code,
                "stage": error_stage,
                "retryable": False,
                "message": _FIXED_MESSAGES[code],
            },
        )

    def record(dispatch_start_ms, operation, status, error_code=None):
        nonlocal pending_call
        entry = {
            "event_id": str(len(trace) + 1),
            "stage": operation,
            "operation": operation,
            "status": status,
            "duration_ms": clock.now_ms() - dispatch_start_ms,
        }
        if error_code is not None:
            entry["error_code"] = error_code
        trace.append(entry)
        pending_call = None

    def exception_outcome(dispatch_start_ms, operation, code, error_stage):
        """异常路径先检查 deadline；未到期再保留原错误分类。"""
        if clock.now_ms() >= deadline:
            record(dispatch_start_ms, operation, "timeout", "OPERATION_TIMEOUT")
            return fail("timeout", "OPERATION_TIMEOUT", error_stage)
        record(dispatch_start_ms, operation, "error", code)
        return fail("error", code, error_stage)

    if remaining_timeout_ms <= 0:
        return fail("timeout", "OPERATION_TIMEOUT", _STAGE_INPUT)

    deadline = start_ms + min(effective_timeout_ms, remaining_timeout_ms)
    if clock.now_ms() >= deadline:
        return fail("timeout", "OPERATION_TIMEOUT", _STAGE_INPUT)

    try:
        history = []
        while True:
            stage = _STAGE_MODEL
            if clock.now_ms() >= deadline:
                return fail("timeout", "OPERATION_TIMEOUT", _STAGE_MODEL)
            if counts["model_calls"] >= max_model_calls:
                return fail("budget_exhausted", "BUDGET_EXHAUSTED", _STAGE_MODEL)
            if history and counts["corrections"] == 0:
                counts["corrections"] += 1
            remaining_ms = deadline - clock.now_ms()
            call_timeout_ms = min(_MODEL_CALL_LIMIT_MS, remaining_ms)
            counts["model_calls"] += 1
            dispatch_start_ms = clock.now_ms()
            pending_call = ("model", dispatch_start_ms)
            try:
                payload = model.next_action(request, history, timeout_ms=call_timeout_ms)
            except TimeoutError:
                record(dispatch_start_ms, "model", "timeout", "OPERATION_TIMEOUT")
                return fail("timeout", "OPERATION_TIMEOUT", _STAGE_MODEL)
            except RuntimeError as exc:
                code = _runtime_error_code(
                    exc, "MODEL_FAILED", ("MODEL_FAILED", "FAKE_SCRIPT_EXHAUSTED")
                )
                return exception_outcome(dispatch_start_ms, "model", code, _STAGE_MODEL)
            except Exception:
                return exception_outcome(
                    dispatch_start_ms, "model", "INTERNAL_ERROR", _STAGE_MODEL
                )

            if clock.now_ms() >= deadline:
                record(dispatch_start_ms, "model", "timeout", "OPERATION_TIMEOUT")
                return fail("timeout", "OPERATION_TIMEOUT", _STAGE_MODEL)

            action = _parse_model_payload(payload)
            if clock.now_ms() >= deadline:
                record(dispatch_start_ms, "model", "timeout", "OPERATION_TIMEOUT")
                return fail("timeout", "OPERATION_TIMEOUT", _STAGE_MODEL)
            if action is None:
                record(dispatch_start_ms, "model", "error", "MODEL_OUTPUT_INVALID")
                if counts["corrections"] > 0:
                    return fail("error", "MODEL_OUTPUT_INVALID", _STAGE_MODEL)
                if (
                    counts["corrections"] >= max_corrections
                    or counts["model_calls"] >= max_model_calls
                ):
                    return fail("budget_exhausted", "BUDGET_EXHAUSTED", _STAGE_MODEL)
                history.append({"stage": "model", "error_code": "MODEL_OUTPUT_INVALID"})
                continue

            record(dispatch_start_ms, "model", "ok")

            if action["kind"] == "clarify":
                return result("needs_clarification", answer=action["message"])
            if action["kind"] == "refuse":
                return result("refused", answer=action["message"])

            stage = _STAGE_QUERY
            if clock.now_ms() >= deadline:
                return fail("timeout", "OPERATION_TIMEOUT", _STAGE_QUERY)
            if counts["queries"] >= max_queries:
                return fail("budget_exhausted", "BUDGET_EXHAUSTED", _STAGE_QUERY)
            remaining_ms = deadline - clock.now_ms()
            call_timeout_ms = min(_QUERY_CALL_LIMIT_MS, remaining_ms)
            counts["queries"] += 1
            dispatch_start_ms = clock.now_ms()
            pending_call = ("query", dispatch_start_ms)
            try:
                table = executor.execute(action["sql"], timeout_ms=call_timeout_ms)
            except TimeoutError:
                record(dispatch_start_ms, "query", "timeout", "OPERATION_TIMEOUT")
                return fail("timeout", "OPERATION_TIMEOUT", _STAGE_QUERY)
            except RuntimeError as exc:
                code = _runtime_error_code(
                    exc, "QUERY_FAILED", ("QUERY_FAILED", "FAKE_SCRIPT_EXHAUSTED")
                )
                return exception_outcome(dispatch_start_ms, "query", code, _STAGE_QUERY)
            except Exception:
                return exception_outcome(
                    dispatch_start_ms, "query", "INTERNAL_ERROR", _STAGE_QUERY
                )

            if clock.now_ms() >= deadline:
                record(dispatch_start_ms, "query", "timeout", "OPERATION_TIMEOUT")
                return fail("timeout", "OPERATION_TIMEOUT", _STAGE_QUERY)

            stage = _STAGE_FORMAT
            table_is_valid = _valid_table(table)
            if clock.now_ms() >= deadline:
                record(dispatch_start_ms, "query", "timeout", "OPERATION_TIMEOUT")
                return fail("timeout", "OPERATION_TIMEOUT", _STAGE_QUERY)
            if not table_is_valid:
                record(dispatch_start_ms, "query", "error", "QUERY_RESULT_INVALID")
                return fail("error", "QUERY_RESULT_INVALID", _STAGE_FORMAT)

            record(dispatch_start_ms, "query", "ok")
            return result("ok", table=table, sql=action["sql"])
    except Exception:
        if pending_call is not None:
            operation, pending_start_ms = pending_call
            if clock.now_ms() >= deadline:
                record(pending_start_ms, operation, "timeout", "OPERATION_TIMEOUT")
                return fail("timeout", "OPERATION_TIMEOUT", stage)
            record(pending_start_ms, operation, "error", "INTERNAL_ERROR")
            return fail("error", "INTERNAL_ERROR", stage)
        if clock.now_ms() >= deadline:
            return fail("timeout", "OPERATION_TIMEOUT", stage)
        return fail("error", "INTERNAL_ERROR", stage)
