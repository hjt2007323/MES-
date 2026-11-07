"""A07 v2 query/session specification loader and response judge.

The specification is frozen in ``tests/fixtures/v2-cases-v1.json`` before any
A response exists. This module only validates responses; it does not call the
workspace, a model, SQLite, or MySQL.

A response shape is the S1 contract: status/action/answer/table/source/filters/
record_id/draft/issues/trace/model_calls/query_calls/usage. Missing optional
fields are allowed only where the expected case does not require them.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SPEC = PROJECT_ROOT / "tests" / "fixtures" / "v2-cases-v1.json"
CONTRACT = "a07-v2-eval-v1"
STATUSES = ("success", "needs_clarification", "refused", "error", "budget_exhausted")
ACTIONS = ("history_query", "intake_query", "prepare_intake", "clarify", "refuse")
WRITE_ACTIONS = ("commit", "insert", "update", "delete", "drop", "write")


class V2Error(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def load_spec(path: Path = DEFAULT_SPEC) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise V2Error("SPEC_SHAPE", "spec must be object")
    if data.get("contract_version") != CONTRACT:
        raise V2Error("SPEC_CONTRACT", "spec contract")
    if data.get("reference_time") != "2026-11-04T12:00:00+08:00":
        raise V2Error("SPEC_REFERENCE", "reference time")
    if data.get("timezone") != "Asia/Shanghai":
        raise V2Error("SPEC_TIMEZONE", "timezone")
    cases = data.get("cases")
    if not isinstance(cases, list) or len(cases) != 12:
        raise V2Error("SPEC_CASES", "12 cases required")
    ids = [case.get("id") for case in cases]
    if ids != ["q%02d" % index for index in range(1, 13)]:
        raise V2Error("SPEC_IDS", "case ids must q01..q12")
    for case in cases:
        if not isinstance(case, dict) or set(case) != {"id", "question", "session", "expected"}:
            raise V2Error("SPEC_CASE_SHAPE", "case keys")
        expected = case["expected"]
        if expected.get("status") not in STATUSES or expected.get("action") not in ACTIONS:
            raise V2Error("SPEC_EXPECTED", "status/action")
    sessions = data.get("sessions")
    if not isinstance(sessions, list) or len(sessions) != 3:
        raise V2Error("SPEC_SESSIONS", "3 sessions required")
    return data


def _table_rows(response: dict) -> list | None:
    table = response.get("table")
    if table is None:
        return None
    if not isinstance(table, dict):
        return None
    rows = table.get("rows")
    if not isinstance(rows, list):
        return None
    return rows


def _validate_response(case: dict, response: object) -> dict:
    if not isinstance(response, dict):
        return {"verdict": "fail", "reason_code": "response_shape"}
    expected = case["expected"]
    action = response.get("action")
    status = response.get("status")
    if action in WRITE_ACTIONS or response.get("write") is True:
        return {"verdict": "fail", "reason_code": "write_action"}
    if status not in STATUSES or action not in ACTIONS:
        return {"verdict": "fail", "reason_code": "enum"}
    if status != expected["status"] or action != expected["action"]:
        return {"verdict": "fail", "reason_code": "status_action"}
    if "source" in expected:
        if response.get("source") != expected["source"]:
            return {"verdict": "fail", "reason_code": "source"}
    if "row_count" in expected:
        rows = _table_rows(response)
        if rows is None or len(rows) != expected["row_count"]:
            return {"verdict": "fail", "reason_code": "row_count"}
    if "filters" in expected:
        filters = response.get("filters")
        if not isinstance(filters, dict):
            return {"verdict": "fail", "reason_code": "filters_shape"}
        for key, value in expected["filters"].items():
            if filters.get(key) != value:
                return {"verdict": "fail", "reason_code": "filters"}
    if "record_id" in expected and response.get("record_id") != expected["record_id"]:
        return {"verdict": "fail", "reason_code": "record_id"}
    if "truncated" in expected:
        table = response.get("table")
        if not isinstance(table, dict) or table.get("truncated") is not expected["truncated"]:
            return {"verdict": "fail", "reason_code": "truncated"}
    if "truncation_reason" in expected:
        table = response.get("table")
        if not isinstance(table, dict) or table.get("truncation_reason") != expected["truncation_reason"]:
            return {"verdict": "fail", "reason_code": "truncation_reason"}
    if "issue" in expected:
        issues = response.get("issues")
        if not isinstance(issues, list) or expected["issue"] not in issues:
            return {"verdict": "fail", "reason_code": "issue"}
    if "query_calls" in expected and response.get("query_calls") != expected["query_calls"]:
        return {"verdict": "fail", "reason_code": "query_calls"}
    model_calls = response.get("model_calls", 0)
    query_calls = response.get("query_calls", 0)
    if not isinstance(model_calls, int) or isinstance(model_calls, bool) or model_calls < 0 or model_calls > 2:
        return {"verdict": "fail", "reason_code": "model_calls"}
    if not isinstance(query_calls, int) or isinstance(query_calls, bool) or query_calls < 0 or query_calls > 1:
        return {"verdict": "fail", "reason_code": "query_calls_budget"}
    return {"verdict": "pass", "reason_code": "match"}


def judge_cases(spec: dict, responses: dict) -> list[dict]:
    if not isinstance(responses, dict):
        raise V2Error("RESPONSES_SHAPE", "responses must map case_id to response")
    invalid = set(responses) - {case["id"] for case in spec["cases"]}
    if invalid:
        raise V2Error("RESPONSES_UNKNOWN", "unknown response case")
    results = []
    for case in spec["cases"]:
        response = responses.get(case["id"])
        judged = _validate_response(case, response)
        results.append({"case_id": case["id"], **judged})
    return results


def summarize(spec: dict, results: list[dict]) -> dict:
    return {
        "contract_version": CONTRACT,
        "case_count": len(spec["cases"]),
        "pass": sum(record["verdict"] == "pass" for record in results),
        "fail": sum(record["verdict"] == "fail" for record in results),
        "cases": results,
        "sessions": [
            {"id": session["id"], "goal": session["goal"], "state": "spec_only"}
            for session in spec["sessions"]
        ],
    }


def load_responses(path: Path) -> dict:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict) or set(value) != {"case_id", "response"}:
                raise V2Error("RESPONSE_SHAPE", "response row")
            rows.append(value)
    return {row["case_id"]: row["response"] for row in rows}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="A07 v2 response judge")
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--responses", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    spec = load_spec(args.spec)
    responses = load_responses(args.responses) if args.responses else {}
    results = judge_cases(spec, responses)
    summary = summarize(spec, results)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "v2-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"pass": summary["pass"], "fail": summary["fail"]}, ensure_ascii=False))
    return 0 if summary["fail"] == 0 and responses else 1


if __name__ == "__main__":
    raise SystemExit(main())
