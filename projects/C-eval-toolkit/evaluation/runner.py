"""Serial engineering runner for C-003 mock cases.

The runner validates request/response protocol with the public contracts
validators, invokes an injected callable for request-valid cases, and records
engineering assertion status.  It never writes files and never treats
``pending`` business cases as formal quality evidence.
"""

from __future__ import annotations

import copy
from collections.abc import Callable

from contracts.validation import validate_request, validate_response

from .compare import compare_tables
from .reporting import summarize

FORMAT_VERSION = "c-cases-0.1"
SUITES = ("engineering", "query", "safety", "recovery", "clarification")
EXPECT_KINDS = ("protocol", "table", "invoke_error", "none")
MODES = ("mock", "real")
TARGET_KINDS = ("named", "question")
QUERY_IDS = ("line_snapshot", "work_order_progress", "downtime_details")
QUESTION_STATUSES = (
    "ok",
    "needs_clarification",
    "refused",
    "error",
    "timeout",
    "budget_exhausted",
)
MODEL_SCRIPT_CODES = (
    "MODEL_TIMEOUT",
    "HTTP_ERROR",
    "NETWORK_ERROR",
    "OUTPUT_INVALID",
    "RESPONSE_TOO_LARGE",
    "CONFIG_MISSING",
    "INPUT_INVALID",
)
INVOKE_ERROR = "INVOKE_ERROR"
_INVALID_CASE = "invalid case input"


def _invalid() -> ValueError:
    return ValueError(_INVALID_CASE)


def _is_pair_list(value: object) -> bool:
    if not isinstance(value, list):
        return False
    for item in value:
        if not isinstance(item, dict):
            return False
        if set(item) != {"path", "code"}:
            return False
        if not isinstance(item["path"], str) or not isinstance(item["code"], str):
            return False
    return True


def _validate_case_shell(case: object, seen_ids: set[str], mode: str) -> None:
    if not isinstance(case, dict):
        raise _invalid()
    for key in ("id", "suite", "review_status", "request", "expect"):
        if key not in case:
            raise _invalid()

    case_id = case["id"]
    if not isinstance(case_id, str) or case_id == "" or case_id in seen_ids:
        raise _invalid()
    seen_ids.add(case_id)

    if case["suite"] not in SUITES:
        raise _invalid()
    if case["review_status"] != "pending":
        raise _invalid()
    if "notes" in case and not isinstance(case["notes"], str):
        raise _invalid()

    if mode == "mock":
        if "fixture" not in case or "target" in case:
            raise _invalid()
        fixture = case["fixture"]
        if not isinstance(fixture, dict):
            raise _invalid()
        fixture_kind = fixture.get("kind")
        if fixture_kind == "return":
            if set(fixture) != {"kind", "response"}:
                raise _invalid()
        elif fixture_kind == "raise":
            if set(fixture) != {"kind"}:
                raise _invalid()
        else:
            raise _invalid()
    else:
        if "fixture" in case or "target" not in case:
            raise _invalid()
        target = case["target"]
        if not isinstance(target, dict) or set(target) != {"query_id", "params"}:
            raise _invalid()
        if target["query_id"] not in QUERY_IDS:
            raise _invalid()
        if not isinstance(target["params"], dict):
            raise _invalid()

    expect = case["expect"]
    if not isinstance(expect, dict):
        raise _invalid()
    expect_kind = expect.get("kind")
    if expect_kind not in EXPECT_KINDS:
        raise _invalid()
    if expect_kind == "protocol":
        if not _is_pair_list(expect.get("request_errors")):
            raise _invalid()
        if not _is_pair_list(expect.get("response_errors")):
            raise _invalid()
    elif expect_kind == "table":
        if not isinstance(expect.get("table"), dict):
            raise _invalid()
        if expect.get("row_order") not in ("ignore", "preserve"):
            raise _invalid()

    if case["suite"] != "engineering" and expect_kind != "none":
        raise _invalid()


def _validate_model_script(value: object) -> bool:
    if not isinstance(value, list):
        return False
    for action in value:
        if not isinstance(action, dict):
            return False
        kind = action.get("kind")
        if kind == "reply":
            allowed = {"kind", "content", "usage", "advance_ms"}
            if not set(action) <= allowed or "content" not in action:
                return False
            if not isinstance(action.get("content"), str):
                return False
            if "usage" in action and action["usage"] is not None and not isinstance(action["usage"], dict):
                return False
        elif kind == "error":
            allowed = {"kind", "code", "advance_ms"}
            if not set(action) <= allowed or "code" not in action:
                return False
            if action["code"] not in MODEL_SCRIPT_CODES:
                return False
        else:
            return False
        if "advance_ms" in action:
            advance = action["advance_ms"]
            if isinstance(advance, bool) or not isinstance(advance, int) or advance < 0:
                return False
    return True


def _validate_tool_script(value: object) -> bool:
    if not isinstance(value, list):
        return False
    for action in value:
        if not isinstance(action, dict):
            return False
        kind = action.get("kind")
        if kind == "return":
            if set(action) != {"kind", "dto"}:
                return False
        elif kind == "timeout":
            if set(action) != {"kind"}:
                return False
        else:
            return False
    return True


def _validate_question_expect(expect: dict) -> bool:
    if expect.get("kind") != "question":
        return False
    if "status" not in expect or expect["status"] not in QUESTION_STATUSES:
        return False
    allowed = {"kind", "status", "query_id", "counts", "error_code"}
    if not set(expect) <= allowed:
        return False
    if "query_id" in expect and expect["query_id"] is not None and not isinstance(expect["query_id"], str):
        return False
    if "error_code" in expect and expect["error_code"] is not None and not isinstance(expect["error_code"], str):
        return False
    if "counts" in expect:
        counts = expect["counts"]
        if not isinstance(counts, dict) or not set(counts) <= {"model_calls", "queries", "corrections"}:
            return False
        for value in counts.values():
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                return False
    return True


def _validate_question_case_shell(case: object, seen_ids: set[str], mode: str) -> None:
    if not isinstance(case, dict):
        raise _invalid()
    for key in ("id", "suite", "review_status", "request", "target", "expect"):
        if key not in case:
            raise _invalid()
    case_id = case["id"]
    if not isinstance(case_id, str) or case_id == "" or case_id in seen_ids:
        raise _invalid()
    seen_ids.add(case_id)
    if case["suite"] not in SUITES or case["review_status"] != "pending":
        raise _invalid()
    if "notes" in case and not isinstance(case["notes"], str):
        raise _invalid()
    if "fixture" in case:
        raise _invalid()
    target = case["target"]
    if not isinstance(target, dict) or target.get("kind") != "question":
        raise _invalid()
    if mode == "mock":
        if set(target) != {"kind", "model_script", "tool_script"}:
            raise _invalid()
        if not _validate_model_script(target["model_script"]):
            raise _invalid()
        if not _validate_tool_script(target["tool_script"]):
            raise _invalid()
    else:
        if set(target) != {"kind"}:
            raise _invalid()
    if not isinstance(case["expect"], dict) or not _validate_question_expect(case["expect"]):
        raise _invalid()


def _validate_cases(cases: object, mode: str, target_kind: str) -> None:
    if not isinstance(cases, list):
        raise _invalid()
    seen_ids: set[str] = set()
    for case in cases:
        if target_kind == "question":
            _validate_question_case_shell(case, seen_ids, mode)
        else:
            _validate_case_shell(case, seen_ids, mode)


def _pairs(errors: list[dict]) -> list[tuple[str, str]]:
    return sorted((item["path"], item["code"]) for item in errors)


def _judge(
    case: dict,
    request_errors: list[dict],
    response_errors: list[dict],
    execution_result: object,
    runner_error: str | None,
) -> tuple[str, str]:
    expect = case["expect"]
    kind = expect["kind"]

    if kind == "none":
        return "unjudged", "no_formal_verdict"

    if kind == "invoke_error":
        if request_errors:
            return "fail", "request_invalid"
        if runner_error == INVOKE_ERROR:
            return "pass", "invoke_error_observed"
        return "fail", "invoke_error_missing"

    if kind == "protocol":
        if runner_error is not None:
            return "fail", "protocol_mismatch"
        expected_request = _pairs(expect["request_errors"])
        expected_response = _pairs(expect["response_errors"])
        if (
            _pairs(request_errors) == expected_request
            and _pairs(response_errors) == expected_response
        ):
            return "pass", "protocol_match"
        return "fail", "protocol_mismatch"

    # kind == "table"
    if request_errors:
        return "fail", "request_invalid"
    if runner_error is not None:
        return "fail", "invoke_error"
    if response_errors:
        return "fail", "response_invalid"
    status = execution_result.get("status") if isinstance(execution_result, dict) else None
    if status != "ok":
        return "fail", "status_not_ok"
    comparison = compare_tables(
        execution_result.get("table") if isinstance(execution_result, dict) else None,
        expect["table"],
        row_order=expect["row_order"],
    )
    if comparison["equal"]:
        return "pass", "table_equal"
    return "fail", comparison["reason_code"]


def _judge_question(
    case: dict,
    request_errors: list[dict],
    response_errors: list[dict],
    execution_result: object,
    runner_error: str | None,
) -> tuple[str, str]:
    expect = case["expect"]
    if request_errors:
        return "fail", "request_invalid"
    if runner_error is not None:
        return "fail", "invoke_error"
    if response_errors:
        return "fail", "response_invalid"
    if not isinstance(execution_result, dict):
        return "fail", "response_invalid"
    if execution_result.get("status") != expect.get("status"):
        return "fail", "status_mismatch"
    if "query_id" in expect and execution_result.get("query_id") != expect["query_id"]:
        return "fail", "query_id_mismatch"
    if "counts" in expect:
        actual_counts = execution_result.get("counts")
        if not isinstance(actual_counts, dict):
            return "fail", "counts_mismatch"
        for key, value in expect["counts"].items():
            if actual_counts.get(key) != value:
                return "fail", "counts_mismatch"
    if "error_code" in expect:
        error = execution_result.get("error")
        actual_code = error.get("code") if isinstance(error, dict) else None
        if actual_code != expect["error_code"]:
            return "fail", "error_code_mismatch"
    return "pass", "question_expectation_match"


def run_cases(
    cases: list[dict],
    invoke: Callable[[dict], object],
    *,
    mode: str = "mock",
    target_kind: str = "named",
) -> tuple[list[dict], dict]:
    """Run cases serially in input order and return ``(records, summary)``.

    ``mode`` is explicit and is forwarded to response validation, every record,
    and the summary.  ``target_kind`` selects the original named-query cases or
    question-runtime cases.  In ``mock`` mode a named case must carry a fixture
    and a question case must carry scripted model/tool actions; in ``real``
    mode only the target descriptor is allowed.  The input cases are never
    mutated.  A request-invalid case is not sent to ``invoke`` and its response
    is not validated.  ``invoke`` receives a deep copy of the case and every
    ordinary ``Exception`` it raises is converted to the fixed ``INVOKE_ERROR``
    record marker.
    """
    if (
        mode not in MODES
        or target_kind not in TARGET_KINDS
        or not callable(invoke)
    ):
        raise _invalid()
    _validate_cases(cases, mode, target_kind)

    records: list[dict] = []
    for case in cases:
        request = case["request"]
        request_errors = validate_request(request)
        response_errors: list[dict] = []
        execution_result: object = None
        runner_error: str | None = None

        if request_errors:
            pass
        else:
            try:
                execution_result = invoke(copy.deepcopy(case))
            except Exception:
                runner_error = INVOKE_ERROR
                execution_result = None
            else:
                response_errors = validate_response(
                    execution_result,
                    request=request,
                    expected_mode=mode,
                )

        if target_kind == "question":
            check_status, reason_code = _judge_question(
                case,
                request_errors,
                response_errors,
                execution_result,
                runner_error,
            )
            infrastructure_error = runner_error is not None
        else:
            check_status, reason_code = _judge(
                case,
                request_errors,
                response_errors,
                execution_result,
                runner_error,
            )
            kind = case["expect"]["kind"]
            infrastructure_error = runner_error is not None and kind != "invoke_error"

        record = {
            "id": case["id"],
            "suite": case["suite"],
            "review_status": case["review_status"],
            "mode": mode,
            "request_errors": request_errors,
            "response_errors": response_errors,
            "execution_result": execution_result,
            "runner_error": runner_error,
            "infrastructure_error": infrastructure_error,
            "check_status": check_status,
            "reason_code": reason_code,
        }
        if target_kind == "question":
            record["engineering_verdict"] = check_status
            record["verdict"] = "unjudged"
            record["execution_source"] = "scripted" if mode == "mock" else "replay"
        records.append(record)

    return records, summarize(records, mode=mode)
