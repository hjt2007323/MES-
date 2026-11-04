"""A-006 M2 controlled question orchestration.

The model only proposes a strict action (query/clarify/refuse); SQL execution
is the existing M1 named-query runtime.  This module owns the shared deadline,
budget, one correction, usage/counts and the single outer JSONL event.
"""

from __future__ import annotations

import io

from mes_assistant import query_catalog, query_planner, semantics
from mes_assistant.llm_client import ModelClientError
from mes_assistant.query_runtime import (
    DATA_VERSION,
    run_query,
    validate_query_input,
)
from mes_assistant.service import ContractInputError, validate_request
from mes_assistant.structured_log import write_event

DEFAULT_TIMEOUT_MS = 30_000
HARD_TIMEOUT_MS = 30_000
DEFAULT_MAX_MODEL_CALLS = 2
DEFAULT_MAX_QUERIES = 2
DEFAULT_MAX_CORRECTIONS = 1
MAX_MODEL_CALLS = 2
MAX_QUERIES = 2
MAX_CORRECTIONS = 1
MODES = ("mock", "real")

_FIXED_MESSAGES = {
    "MODEL_CONFIG_MISSING": "模型配置缺失或不可用，未发起真实模型请求。",
    "PLAN_INVALID": "查询计划无法解析或参数不符合命名查询约束，未执行数据库查询。",
    "MODEL_FAILED": "模型调用失败，未重试。",
    "OPERATION_TIMEOUT": "问答在总预算内未完成。",
    "BUDGET_EXHAUSTED": "问答预算不足，未继续派发。",
    "LOG_WRITE_FAILED": "日志写入失败。",
}
_ERROR_STAGE = {
    "MODEL_CONFIG_MISSING": "model",
    "PLAN_INVALID": "plan",
    "MODEL_FAILED": "model",
    "OPERATION_TIMEOUT": "query",
    "BUDGET_EXHAUSTED": "query",
    "LOG_WRITE_FAILED": "log",
}


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _fixed_error(code: str, *, stage: str | None = None) -> dict:
    return {
        "code": code,
        "stage": _ERROR_STAGE[code] if stage is None else stage,
        "message": _FIXED_MESSAGES[code],
        "retryable": False,
    }


def _validate_service_limits(service_limits: dict) -> None:
    if not isinstance(service_limits, dict):
        raise ContractInputError("service_limits must be an object")
    for key in ("timeout_ms", "max_model_calls", "max_queries", "max_corrections"):
        if key not in service_limits:
            raise ContractInputError("service_limits missing %s" % key)
        value = service_limits[key]
        if not _is_int(value):
            raise ContractInputError("service_limits.%s must be an integer" % key)
        if key == "timeout_ms":
            if value <= 0:
                raise ContractInputError("service_limits.timeout_ms must be positive")
        elif value < 0:
            raise ContractInputError("service_limits.%s must be non-negative" % key)


def _attempts(model_client) -> int:
    value = getattr(model_client, "attempts", 0)
    return value if _is_int(value) and value >= 0 else 0


def _plan_error_code(exc):
    code = getattr(exc, "code", None)
    if code in ("PLAN_INVALID", "PLAN_FORBIDDEN"):
        return code
    return "PLAN_INVALID"


def _merge_usage(parts) -> dict | None:
    if not parts:
        return None
    input_values = []
    output_values = []
    for usage in parts:
        if not isinstance(usage, dict):
            input_values.append(None)
            output_values.append(None)
            continue
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        input_values.append(prompt if _is_int(prompt) and prompt >= 0 else None)
        output_values.append(
            completion if _is_int(completion) and completion >= 0 else None
        )
    input_known = [value for value in input_values if value is not None]
    output_known = [value for value in output_values if value is not None]
    input_total = None if len(input_known) != len(input_values) else sum(input_known)
    output_total = None if len(output_known) != len(output_values) else sum(output_known)
    if input_total is None and output_total is None:
        return None
    return {"input_tokens": input_total, "output_tokens": output_total}


def _build_result(
    request,
    *,
    mode,
    status,
    answer,
    table,
    sql,
    metrics,
    error,
    counts,
    usage,
    trace,
    internal_ms,
    query_id,
    context_meta=None,
    plan=None,
):
    context_meta = context_meta if isinstance(context_meta, dict) else {}
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
        "metrics": [] if metrics is None else metrics,
        "error": error,
        "timing": {"internal_ms": internal_ms},
        "counts": {
            "model_calls": counts["model_calls"],
            "queries": counts["queries"],
            "corrections": counts["corrections"],
        },
        "usage": usage,
        "config_id": request["config_id"],
        "query_id": query_id,
        "review_status": "pending",
        "semantic_policy_version": context_meta.get("semantic_policy_version"),
        "context_mode": context_meta.get("context_mode"),
        "context_evidence": context_meta.get("context_evidence"),
        "plan": plan,
        "trace": list(trace),
    }


def run_question(
    request: dict,
    *,
    model_client,
    tool,
    clock,
    service_limits: dict,
    remaining_timeout_ms: int,
    mode: str,
    log_sink,
) -> dict:
    """Run one question through planner/model/M1 named query, one outer log event."""
    if mode not in MODES:
        raise ContractInputError("mode must be mock or real")
    if not isinstance(request, dict):
        raise ContractInputError("request must be an object")
    validate_request(request)
    if request.get("data_version") != DATA_VERSION:
        raise ContractInputError("request data_version must be %s" % DATA_VERSION)
    _validate_service_limits(service_limits)
    if not _is_int(remaining_timeout_ms):
        raise ContractInputError("remaining_timeout_ms must be an integer")
    if not callable(getattr(model_client, "complete", None)):
        raise ContractInputError("model_client must provide complete()")
    if not callable(tool):
        raise ContractInputError("tool must be callable")
    if not callable(getattr(clock, "now_ms", None)):
        raise ContractInputError("clock must provide now_ms()")

    context_mode = request.get("context_mode", "full")
    if context_mode not in ("full", "focused"):
        raise ContractInputError("context_mode must be full or focused")
    try:
        semantic_policy = semantics.get_semantic_policy()
        context_evidence = semantics.select_context(request, mode=context_mode)
    except Exception:
        raise ContractInputError("semantic policy/context selection failed") from None
    context_meta = {
        "semantic_policy_version": semantic_policy.get("semantic_policy_version"),
        "context_mode": context_mode,
        "context_evidence": context_evidence,
    }

    budget = request["budget"]
    effective_timeout_ms = min(
        budget["timeout_ms"],
        service_limits["timeout_ms"],
        remaining_timeout_ms,
        HARD_TIMEOUT_MS,
    )
    max_model_calls = min(
        budget["max_model_calls"], service_limits["max_model_calls"], MAX_MODEL_CALLS
    )
    max_queries = min(
        budget["max_queries"], service_limits["max_queries"], MAX_QUERIES
    )
    max_corrections = min(
        budget["max_corrections"],
        service_limits["max_corrections"],
        MAX_CORRECTIONS,
    )

    start_ms = clock.now_ms()
    deadline = start_ms + effective_timeout_ms
    counts = {"model_calls": 0, "queries": 0, "corrections": 0}
    usage_parts = []
    model_traces = []
    query_id = None
    adopted_plan = None

    def build(
        status,
        *,
        answer=None,
        table=None,
        sql=None,
        metrics=None,
        error=None,
        query_id=None,
        internal_ms=None,
    ):
        return _build_result(
            request,
            mode=mode,
            status=status,
            answer=answer,
            table=table,
            sql=sql,
            metrics=metrics,
            error=error,
            counts=counts,
            usage=_merge_usage(usage_parts),
            trace=model_traces,
            internal_ms=clock.now_ms() - start_ms if internal_ms is None else internal_ms,
            query_id=query_id,
            context_meta=context_meta,
            plan=adopted_plan,
        )

    def finish(result):
        # A006-R3: final result formatting also consumes the shared deadline.
        if clock.now_ms() >= deadline and result["status"] in (
            "ok",
            "needs_clarification",
            "refused",
        ):
            format_trace = list(result["trace"])
            format_trace.append(
                {
                    "event_id": "format-1",
                    "stage": "format",
                    "operation": "finalize",
                    "status": "timeout",
                    "duration_ms": clock.now_ms() - start_ms,
                    "error_code": "OPERATION_TIMEOUT",
                }
            )
            result = _build_result(
                request,
                mode=mode,
                status="timeout",
                answer=_FIXED_MESSAGES["OPERATION_TIMEOUT"],
                table=None,
                sql=None,
                metrics=[],
                error=_fixed_error("OPERATION_TIMEOUT", stage="format"),
                counts=result["counts"],
                usage=result["usage"],
                trace=format_trace,
                internal_ms=clock.now_ms() - start_ms,
                query_id=result["query_id"],
                context_meta=context_meta,
                plan=result.get("plan"),
            )
        event = {
            "run_id": request["run_id"],
            "request_id": request["request_id"],
            "query_id": result["query_id"],
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
            return _build_result(
                request,
                mode=mode,
                status="error",
                answer=_FIXED_MESSAGES["LOG_WRITE_FAILED"],
                table=None,
                sql=None,
                metrics=[],
                error=_fixed_error("LOG_WRITE_FAILED"),
                counts=result["counts"],
                usage=result["usage"],
                trace=result["trace"],
                internal_ms=clock.now_ms() - start_ms,
                query_id=result["query_id"],
                context_meta=context_meta,
                plan=result.get("plan"),
            )
        return result

    def terminal(status, code, *, answer=None, query_id=None, stage=None):
        return finish(
            build(
                status,
                answer=answer or _FIXED_MESSAGES[code],
                error=None if code is None else _fixed_error(code, stage=stage),
                query_id=query_id,
            )
        )

    if effective_timeout_ms <= 0 or clock.now_ms() >= deadline:
        return terminal("timeout", "OPERATION_TIMEOUT", stage="input")
    if max_model_calls < 1:
        return terminal("budget_exhausted", "BUDGET_EXHAUSTED", stage="budget")

    planner = query_planner

    def model_call(messages, number):
        now = clock.now_ms()
        if now >= deadline:
            return terminal("timeout", "OPERATION_TIMEOUT", stage="model")
        if counts["model_calls"] >= max_model_calls:
            return terminal("budget_exhausted", "BUDGET_EXHAUSTED", stage="budget")
        remaining = deadline - now
        if remaining <= 0:
            return terminal("timeout", "OPERATION_TIMEOUT", stage="model")
        before = _attempts(model_client)
        call_start = clock.now_ms()
        try:
            reply = model_client.complete(messages, timeout_ms=remaining)
        except Exception as exc:
            after = _attempts(model_client)
            delta = max(0, after - before)
            counts["model_calls"] += delta
            if number == 2 and delta > 0:
                counts["corrections"] = 1
            timed_out = clock.now_ms() >= deadline
            client_code = getattr(exc, "code", None)
            if client_code == "MODEL_TIMEOUT":
                code = "OPERATION_TIMEOUT"
                status = "timeout"
                stage = "model"
            elif isinstance(exc, ModelClientError) and client_code == "CONFIG_MISSING":
                code = "MODEL_CONFIG_MISSING"
                status = "error"
                stage = "model"
            elif timed_out:
                code = "OPERATION_TIMEOUT"
                status = "timeout"
                stage = "model"
            else:
                code = "MODEL_FAILED"
                status = "error"
                stage = "model"
            underlying = client_code if isinstance(client_code, str) else code
            model_traces.append(
                {
                    "event_id": "model-%d" % number,
                    "stage": "model",
                    "operation": "complete",
                    "status": "timeout" if status == "timeout" else "error",
                    "duration_ms": clock.now_ms() - call_start,
                    "error_code": underlying,
                }
            )
            return finish(
                build(
                    status,
                    answer=_FIXED_MESSAGES[code],
                    error=_fixed_error(code, stage=stage),
                )
            )
        after = _attempts(model_client)
        delta = max(0, after - before)
        counts["model_calls"] += delta
        if number == 2 and delta > 0:
            counts["corrections"] = 1
        usage_parts.append(getattr(reply, "usage", None))
        model_traces.append(
            {
                "event_id": "model-%d" % number,
                "stage": "model",
                "operation": "complete",
                "status": "ok",
                "duration_ms": clock.now_ms() - call_start,
            }
        )
        if clock.now_ms() >= deadline:
            return terminal("timeout", "OPERATION_TIMEOUT", stage="model")
        return ("reply", reply)

    previous_error = None
    plan = None
    for attempt in (1, 2):
        try:
            messages = planner.build_messages(
                request, previous_error=previous_error
            )
        except Exception:
            return terminal("error", "MODEL_FAILED", stage="plan")
        outcome = model_call(messages, attempt)
        if isinstance(outcome, dict):
            return outcome
        reply = outcome[1]
        try:
            plan = planner.parse_plan(reply.content)
        except Exception as exc:
            code = _plan_error_code(exc)
            if code == "PLAN_FORBIDDEN":
                return terminal(
                    "refused",
                    None,
                    answer="该问题超出当前受控命名查询范围，无法执行。",
                )
            if attempt == 2:
                if clock.now_ms() >= deadline:
                    return terminal("timeout", "OPERATION_TIMEOUT", stage="format")
                return terminal("error", "PLAN_INVALID", stage="plan")
            if (
                clock.now_ms() >= deadline
                or counts["model_calls"] >= max_model_calls
                or counts["corrections"] >= max_corrections
                or max_corrections < 1
            ):
                if clock.now_ms() >= deadline:
                    return terminal("timeout", "OPERATION_TIMEOUT")
                return terminal("budget_exhausted", "BUDGET_EXHAUSTED")
            previous_error = "PLAN_INVALID"
            continue
        if not isinstance(plan, dict):
            if attempt == 2:
                return terminal("error", "PLAN_INVALID", stage="plan")
            previous_error = "PLAN_INVALID"
            continue
        kind = plan.get("kind")
        if kind in ("clarify", "refuse"):
            message = plan.get("message")
            if not isinstance(message, str) or not message.strip() or len(message) > 512:
                if attempt == 2:
                    return terminal("error", "PLAN_INVALID", stage="plan")
                previous_error = "PLAN_INVALID"
                continue
            if clock.now_ms() >= deadline:
                return terminal("timeout", "OPERATION_TIMEOUT", stage="format")
            status = "needs_clarification" if kind == "clarify" else "refused"
            adopted_plan = {"kind": kind}
            return terminal(status, None, answer=message.strip())
        if kind != "query":
            if attempt == 2:
                return terminal("error", "PLAN_INVALID", stage="plan")
            previous_error = "PLAN_INVALID"
            continue
        query_id = plan.get("query_id")
        params = plan.get("params")
        try:
            description = validate_query_input(
                request,
                query_id=query_id,
                params=params,
                service_limits=service_limits,
                remaining_timeout_ms=max(0, deadline - clock.now_ms()),
                mode=mode,
            )
        except (ContractInputError, KeyError, ValueError, TypeError):
            if attempt == 2:
                if clock.now_ms() >= deadline:
                    return terminal("timeout", "OPERATION_TIMEOUT", stage="format")
                return terminal("error", "PLAN_INVALID", stage="plan", query_id=query_id)
            if (
                clock.now_ms() >= deadline
                or counts["model_calls"] >= max_model_calls
                or counts["corrections"] >= max_corrections
                or max_corrections < 1
            ):
                if clock.now_ms() >= deadline:
                    return terminal("timeout", "OPERATION_TIMEOUT", stage="format")
                return terminal("budget_exhausted", "BUDGET_EXHAUSTED", stage="budget")
            previous_error = "PLAN_INVALID"
            continue
        break
    else:
        return terminal("error", "PLAN_INVALID", stage="plan")

    if plan is None:
        return terminal("error", "PLAN_INVALID", stage="plan")
    if clock.now_ms() >= deadline:
        return terminal("timeout", "OPERATION_TIMEOUT", stage="format", query_id=query_id)
    if max_queries < 2:
        return terminal("budget_exhausted", "BUDGET_EXHAUSTED", stage="query", query_id=query_id)

    adopted_plan = {"kind": "query", "query_id": query_id, "params": plan.get("params")}
    inner_log = io.StringIO()
    counts["queries"] = None
    try:
        query_result = run_query(
            request,
            query_id=query_id,
            params=params,
            tool=tool,
            clock=clock,
            service_limits=service_limits,
            remaining_timeout_ms=max(0, deadline - clock.now_ms()),
            mode=mode,
            log_sink=inner_log,
        )
    except ContractInputError:
        counts["queries"] = 0
        return terminal("error", "PLAN_INVALID", stage="plan", query_id=query_id)
    except Exception:
        return terminal("error", "MODEL_FAILED", stage="query", query_id=query_id)

    query_counts = query_result.get("counts")
    queries = None
    if isinstance(query_counts, dict):
        value = query_counts.get("queries")
        if _is_int(value) and value in (0, 1, 2):
            queries = value
        elif value is None:
            queries = None
    counts["queries"] = queries
    query_trace = query_result.get("trace")
    if not isinstance(query_trace, list):
        query_trace = []
    trace = list(query_trace) + list(model_traces)

    status = query_result.get("status")
    if not isinstance(status, str):
        return terminal("error", "MODEL_FAILED", stage="query", query_id=query_id)
    if clock.now_ms() >= deadline and status != "timeout":
        return finish(
            _build_result(
                request,
                mode=mode,
                status="timeout",
                answer=_FIXED_MESSAGES["OPERATION_TIMEOUT"],
                table=None,
                sql=None,
                metrics=[],
                error=_fixed_error("OPERATION_TIMEOUT", stage="format"),
                counts=counts,
                usage=_merge_usage(usage_parts),
                trace=trace,
                internal_ms=clock.now_ms() - start_ms,
                query_id=query_id,
                context_meta=context_meta,
                plan=adopted_plan,
            )
        )
    if status not in ("ok", "refused", "error", "timeout", "budget_exhausted"):
        return terminal("error", "MODEL_FAILED", stage="query", query_id=query_id)
    result = _build_result(
        request,
        mode=mode,
        status=status,
        answer=query_result.get("answer"),
        table=query_result.get("table"),
        sql=query_result.get("sql"),
        metrics=query_result.get("metrics") if status == "ok" else [],
        error=query_result.get("error"),
        counts=counts,
        usage=_merge_usage(usage_parts),
        trace=trace,
        internal_ms=clock.now_ms() - start_ms,
        query_id=query_id,
        context_meta=context_meta,
        plan=adopted_plan,
    )
    return finish(result)
