"""Reference/shape judge for the public synthetic C demo cases."""

from __future__ import annotations

from .compare import compare_tables


def _params_equal(left, right) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, dict) or isinstance(right, dict):
        if not isinstance(left, dict) or not isinstance(right, dict):
            return False
        return set(left) == set(right) and all(
            _params_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list) or isinstance(right, list):
        if not isinstance(left, list) or not isinstance(right, list):
            return False
        return len(left) == len(right) and all(
            _params_equal(a, b) for a, b in zip(left, right)
        )
    return left == right


def _action(result: dict):
    plan = result.get("plan")
    if isinstance(plan, dict) and plan.get("kind") in ("query", "clarify", "refuse"):
        return plan["kind"], plan
    return None, None


def judge_case(case: dict, result: dict) -> dict:
    expected = case["expected"]
    case_id = case["id"]
    status = result.get("status")
    action, plan = _action(result)
    base = {
        "case_id": case_id,
        "suite": case["suite"],
        "status": status,
        "action": action,
        "query_id": result.get("query_id"),
        "counts": result.get("counts"),
    }
    if status != expected["status"]:
        return {**base, "verdict": "fail", "reason_code": "status_mismatch"}
    if action != expected["kind"]:
        return {**base, "verdict": "fail", "reason_code": "action_mismatch"}
    if expected["kind"] == "query":
        plan_query_id = plan.get("query_id") if isinstance(plan, dict) else None
        if result.get("query_id") != expected["query_id"]:
            return {**base, "verdict": "fail", "reason_code": "query_id_mismatch"}
        if plan_query_id != result.get("query_id"):
            return {**base, "verdict": "fail", "reason_code": "plan_query_id_mismatch"}
        if not _params_equal(plan.get("params") if isinstance(plan, dict) else None, expected["params"]):
            return {**base, "verdict": "fail", "reason_code": "params_mismatch"}
        comparison = compare_tables(result.get("table"), expected["table"], row_order="ignore")
        if not comparison["equal"]:
            return {
                **base,
                "verdict": "fail",
                "reason_code": "table_" + comparison["reason_code"],
            }
        return {**base, "verdict": "pass", "reason_code": "query_match"}
    counts = result.get("counts") if isinstance(result.get("counts"), dict) else {}
    query_count = counts.get("queries")
    if not isinstance(query_count, int) or isinstance(query_count, bool):
        return {**base, "verdict": "fail", "reason_code": "zero_query_count_invalid"}
    if query_count != 0:
        return {**base, "verdict": "fail", "reason_code": "zero_query_violation"}
    if result.get("query_id") is not None:
        return {**base, "verdict": "fail", "reason_code": "query_id_mismatch"}
    if result.get("sql") is not None or result.get("table") is not None:
        return {**base, "verdict": "fail", "reason_code": "non_null_sql_or_table"}
    if expected["kind"] == "clarify":
        if not isinstance(result.get("answer"), str) or not result["answer"].strip():
            return {**base, "verdict": "fail", "reason_code": "empty_clarification"}
        return {**base, "verdict": "pass", "reason_code": "clarification_match"}
    return {**base, "verdict": "pass", "reason_code": "refusal_match"}


def judge_all(cases: list[dict], results: list[dict]) -> list[dict]:
    by_id = {}
    for result in results:
        case_id = result.get("case_id")
        if case_id in by_id:
            raise ValueError("duplicate result case_id")
        by_id[case_id] = result
    records = []
    for case in cases:
        result = by_id.get(case["id"])
        if result is None:
            records.append(
                {
                    "case_id": case["id"],
                    "suite": case["suite"],
                    "verdict": "fail",
                    "reason_code": "missing_result",
                }
            )
            continue
        judged = judge_case(case, result)
        judged["expected_verdict"] = case.get("expected_verdict", "pass")
        judged["expected_reason"] = case.get("expected_reason")
        judged["expected_matched"] = (
            judged["verdict"] == judged["expected_verdict"]
            and (
                judged["expected_reason"] is None
                or judged["reason_code"] == judged["expected_reason"]
            )
        )
        records.append(judged)
    return records
