"""C-007 M3 quality runner, dataset checks and read-only replay binding.

The quality layer is separate from the M2 engineering runner.  It judges the
frozen M3 demo reference set, validates the byte-identical dataset, and replays
A-008 experiment evidence without calling a model or a database.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
import time
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from contracts.validation import validate_response
from evaluation.compare import compare_tables

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROOT = PROJECT_ROOT.parent.parent
DATASET_PATH = PROJECT_ROOT / "tests" / "fixtures" / "cases-m3-demo-v1.json"
BUDGET_PATH = ROOT / "coordination" / "M3-实测预算.json"
A008_EXPERIMENT = ROOT / "execution" / "A" / "A-008" / "experiment"
OUTPUT_ROOT = ROOT / "execution" / "C" / "C-007" / "outputs"

DATASET_SHA256 = "18769a781d99db6e4399634fa0cd9250fea9c62b5dc4e5cb654517143e748a6c"
BUDGET_SHA256 = "2ab139f96b1128fc1c52c2f0ac093c8dd222a0001efd26bde9012888d2a59cd3"
CONTRACT_VERSION = "m3-demo-v1"
DATASET_ID = "mes-demo-questions-v1"
SEMANTIC_POLICY_VERSION = "demo-metrics-v1-user-reviewed-20261103"
DATA_VERSION = "mes-a07-boundary-v1-pending"
REFERENCE_TIME = "2026-11-02T00:00:00+08:00"
TIMEZONE = "Asia/Shanghai"
PILOT_IDS = (
    "m3q-01", "m3q-02", "m3q-07", "m3q-09", "m3q-11", "m3q-13",
    "m3q-14", "m3q-16", "m3q-19", "m3q-20", "m3q-25", "m3q-27",
)
CATALOG_IDS = ("downtime_details", "line_snapshot", "work_order_progress")
REFERENCE_QUERY_ORDER = ("line_snapshot", "work_order_progress", "downtime_details")
SUITES = ("query", "clarification", "safety")
VARIANTS = ("full", "focused")
COLUMNS = {
    "line_snapshot": [
        ("line_no", "string", False),
        ("actual_qty", "decimal", False),
    ],
    "work_order_progress": [
        ("order_no", "string", False),
        ("line_no", "string", False),
        ("plan_qty", "integer", False),
        ("actual_qty", "integer", False),
        ("quantity_ratio", "decimal", True),
        ("status", "string", False),
    ],
    "downtime_details": [
        ("downtime_id", "integer", False),
        ("machine_no", "string", False),
        ("start_time", "datetime", False),
        ("end_time", "datetime", True),
        ("reason", "string", False),
        ("order_id", "string", True),
    ],
}
PARAM_KEYS = {
    "line_snapshot": set(),
    "work_order_progress": {"line_id"},
    "downtime_details": {"window_start", "window_end"},
}
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_EXPERIMENT_FILE_BYTES = 32 * 1024 * 1024


class ReplayError(ValueError):
    """Fixed replay/read error; never includes evidence payloads."""


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("non-finite JSON constant")


def _has_non_finite(value) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        for key, item in value.items():
            if _has_non_finite(key) or _has_non_finite(item):
                return True
    elif isinstance(value, list):
        for item in value:
            if _has_non_finite(item):
                return True
    return False


def _strict_json(value: bytes):
    if len(value) > MAX_JSON_BYTES or value.startswith(b"\xef\xbb\xbf"):
        raise ValueError("invalid JSON bytes")
    try:
        text = value.decode("utf-8")
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise ValueError("invalid JSON")
    if _has_non_finite(parsed):
        raise ValueError("non-finite JSON")
    return parsed


def _load_json(path: Path):
    return _strict_json(path.read_bytes())


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path))).rstrip("\\/")


def _safe_file(root: Path, name: str, *, max_bytes: int = MAX_EXPERIMENT_FILE_BYTES) -> Path:
    """Return a regular file below ``root``, rejecting links and traversal."""
    try:
        root_path = root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ReplayError("invalid_path")
    if _norm(root) != _norm(root_path):
        raise ReplayError("invalid_path")
    candidate = root / name
    if candidate.is_symlink():
        raise ReplayError("invalid_path")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ReplayError("invalid_path")
    if _norm(candidate) != _norm(resolved) or not resolved.is_file():
        raise ReplayError("invalid_path")
    if resolved.stat().st_size > max_bytes:
        raise ReplayError("file_too_large")
    return candidate


def _parse_iso(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("+08:00"):
        raise ValueError("invalid +08 time")
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 8 * 3600:
        raise ValueError("invalid +08 time")
    return parsed


def expected_columns(query_id: str):
    return COLUMNS.get(query_id)


def params_equal(left, right) -> bool:
    if isinstance(left, dict) or isinstance(right, dict):
        if not isinstance(left, dict) or not isinstance(right, dict):
            return False
        if set(left) != set(right):
            return False
        return all(params_equal(left[key], right[key]) for key in left)
    if isinstance(left, list) or isinstance(right, list):
        if not isinstance(left, list) or not isinstance(right, list):
            return False
        return len(left) == len(right) and all(
            params_equal(a, b) for a, b in zip(left, right)
        )
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    return left == right


def _validate_table_shape(table, query_id: str) -> str | None:
    if not isinstance(table, dict):
        return "missing_table"
    if set(table) != {"columns", "rows", "truncated", "truncation_reason"}:
        return "table_fields"
    columns = table.get("columns")
    if not isinstance(columns, list) or len(columns) != len(COLUMNS[query_id]):
        return "columns"
    for column, expected in zip(columns, COLUMNS[query_id]):
        if not isinstance(column, dict) or set(column) != {"name", "type", "nullable"}:
            return "columns"
        if (column.get("name"), column.get("type"), column.get("nullable")) != expected:
            return "columns"
    rows = table.get("rows")
    if not isinstance(rows, list):
        return "rows"
    for row in rows:
        if not isinstance(row, list) or len(row) != len(columns):
            return "rows"
        for value, column in zip(row, columns):
            if value is None:
                if not column["nullable"]:
                    return "rows"
            elif column["type"] in ("integer", "decimal"):
                if not isinstance(value, (int, str)) or isinstance(value, bool):
                    return "rows"
            elif not isinstance(value, str):
                return "rows"
    if table.get("truncated") is not False:
        return "truncated"
    if table.get("truncation_reason") is not None:
        return "truncation_reason"
    return None


def validate_dataset(
    doc: object,
    *,
    source_bytes: bytes | None = None,
    budget: dict | None = None,
    a_fixture: Path | None = None,
) -> dict:
    """Validate the frozen dataset; returns checks/errors without raising."""
    checks: list[str] = []
    errors: list[str] = []

    def check(name: str, condition: bool) -> None:
        if condition:
            checks.append(name)
        else:
            errors.append(name)

    if not isinstance(doc, dict):
        return {"ok": False, "checks": checks, "errors": ["document"]}
    check("format_version", doc.get("format_version") == "m3-demo-cases-0.1")
    check("dataset_id", doc.get("dataset_id") == DATASET_ID)
    check("semantic_policy_version", doc.get("semantic_policy_version") == SEMANTIC_POLICY_VERSION)
    check("data_version", doc.get("data_version") == DATA_VERSION)
    check("reference_time", doc.get("reference_time") == REFERENCE_TIME)
    check("timezone", doc.get("timezone") == TIMEZONE)
    approval = doc.get("approval")
    check("approval_scope", isinstance(approval, dict) and approval.get("scope") == "synthetic_demo_only")
    check(
        "approval_reference_queries",
        isinstance(approval, dict)
        and approval.get("reference_crosscheck_queries") == list(REFERENCE_QUERY_ORDER),
    )
    cases = doc.get("cases")
    if not isinstance(cases, list):
        return {"ok": False, "checks": checks, "errors": errors + ["cases"]}
    check("case_count", len(cases) == 30)
    ids = [case.get("id") for case in cases if isinstance(case, dict)]
    check("unique_ids", len(ids) == 30 and len(set(ids)) == 30)
    check("id_order", ids == ["m3q-%02d" % i for i in range(1, 31)])
    check("suites", Counter(case.get("suite") for case in cases if isinstance(case, dict)) == {
        "query": 18, "clarification": 6, "safety": 6
    })
    check("expected_kinds", Counter(
        case.get("expected", {}).get("kind") for case in cases if isinstance(case, dict)
    ) == {"query": 18, "clarify": 6, "refuse": 6})
    check("expected_statuses", Counter(
        case.get("expected", {}).get("status") for case in cases if isinstance(case, dict)
    ) == {"ok": 18, "needs_clarification": 6, "refused": 6})
    check("review_status", all(case.get("review_status") == "approved_for_demo" for case in cases if isinstance(case, dict)))
    check("pilot_ids", doc.get("pilot_ids") == list(PILOT_IDS))
    if budget is not None:
        check("budget_case_ids", budget.get("case_ids") == list(PILOT_IDS))
        check("budget_dataset_sha", budget.get("dataset_sha256") == DATASET_SHA256)
        check("budget_state", budget.get("state") == "approved_by_user")
    if source_bytes is not None:
        check("source_sha256", _sha_bytes(source_bytes) == DATASET_SHA256)
    if a_fixture is not None:
        try:
            check("a_fixture_sha256", _sha_file(a_fixture) == doc.get("fixture", {}).get("sha256"))
        except OSError:
            errors.append("a_fixture_sha256")

    query_seen = {"line_snapshot": 0, "work_order_progress": 0, "downtime_details": 0}
    clarify_slots = set()
    for case in cases:
        if not isinstance(case, dict):
            continue
        expected = case.get("expected")
        if not isinstance(expected, dict):
            continue
        kind = expected.get("kind")
        if kind == "query":
            query_id = expected.get("query_id")
            if query_id in query_seen:
                query_seen[query_id] += 1
            else:
                errors.append("query_id")
                continue
            check("query_params_%s" % case["id"], isinstance(expected.get("params"), dict) and set(expected["params"]) == PARAM_KEYS[query_id])
            check("table_shape_%s" % case["id"], _validate_table_shape(expected.get("table"), query_id) is None)
            check("row_order_%s" % case["id"], expected.get("table_row_order") == "ignore")
            check("answer_not_scored_%s" % case["id"], expected.get("answer_text_scoring") == "not_scored")
            if query_id == "downtime_details" and isinstance(expected.get("params"), dict):
                try:
                    check(
                        "window_%s" % case["id"],
                        _parse_iso(expected["params"].get("window_start"))
                        < _parse_iso(expected["params"].get("window_end")),
                    )
                except (ValueError, TypeError):
                    errors.append("window_%s" % case["id"])
        elif kind == "clarify":
            slot = expected.get("clarification_slot")
            clarify_slots.add(slot)
            check("clarify_fields_%s" % case["id"], expected.get("status") == "needs_clarification" and expected.get("query_id") is None and expected.get("params") is None and expected.get("table") is None)
        elif kind == "refuse":
            check("refuse_fields_%s" % case["id"], expected.get("status") == "refused" and expected.get("query_id") is None and expected.get("params") is None and expected.get("table") is None)
        else:
            errors.append("kind_%s" % case.get("id"))
    check("query_coverage", all(value > 0 for value in query_seen.values()))
    check("clarification_slots", clarify_slots == {"line_id", "time_window", "metric_scope", "single_line"})
    by_id = {case.get("id"): case for case in cases if isinstance(case, dict)}
    try:
        boundary_ok = (
            by_id["m3q-01"]["expected"]["table"]["rows"]
            == [["L01", "205"], ["L02", "80"], ["L03", "0"]]
            and by_id["m3q-07"]["expected"]["table"]["rows"][1][2:] == [0, 5, None, "生产中"]
            and by_id["m3q-11"]["expected"]["table"]["rows"] == []
            and by_id["m3q-12"]["expected"]["table"]["rows"] == []
            and by_id["m3q-16"]["expected"]["table"]["rows"] == []
            and by_id["m3q-17"]["expected"]["table"]["rows"][0][3] is None
            and by_id["m3q-18"]["expected"]["table"]["rows"][0][2:4]
            == ["2026-11-01T12:00:00+08:00", "2026-11-01T12:00:00+08:00"]
            and by_id["m3q-19"]["expected"]["clarification_slot"] == "line_id"
            and by_id["m3q-20"]["expected"]["clarification_slot"] == "time_window"
            and by_id["m3q-21"]["expected"]["clarification_slot"] == "metric_scope"
            and by_id["m3q-22"]["expected"]["clarification_slot"] == "single_line"
        )
    except (KeyError, IndexError, TypeError):
        boundary_ok = False
    check("boundary_reference_tables", boundary_ok)
    try:
        pilot_suites = Counter(
            next(case.get("suite") for case in cases if case.get("id") == case_id)
            for case_id in PILOT_IDS
        )
    except (StopIteration, RuntimeError, TypeError):
        pilot_suites = Counter()
    check("pilot_suites", pilot_suites == {"query": 8, "clarification": 2, "safety": 2})
    check("pilot_subset", all(any(case.get("id") == case_id for case in cases) for case_id in PILOT_IDS))
    return {"ok": not errors, "checks": checks, "errors": errors}


def load_dataset(path: Path = DATASET_PATH) -> dict:
    doc = _load_json(path)
    if not isinstance(doc, dict):
        raise ValueError("dataset root must be an object")
    return doc


def load_budget(path: Path = BUDGET_PATH) -> dict:
    doc = _load_json(path)
    if not isinstance(doc, dict):
        raise ValueError("budget root must be an object")
    return doc


def dataset_sha(path: Path = DATASET_PATH) -> str:
    return _sha_file(path)


def build_request(
    case: dict,
    *,
    context_mode: str = "full",
    run_id: str = "c007-offline",
    config_id: str = "m3-offline-example",
) -> dict:
    if context_mode not in VARIANTS:
        raise ValueError("invalid context_mode")
    if not isinstance(case, dict) or not isinstance(case.get("id"), str):
        raise ValueError("invalid case")
    return {
        "schema_version": "0.1",
        "run_id": run_id,
        "request_id": case["id"] + "-" + context_mode,
        "case_id": case["id"],
        "question": case["question"],
        "data_version": DATA_VERSION,
        "reference_time": REFERENCE_TIME,
        "timezone": TIMEZONE,
        "required_capabilities": [],
        "budget": {
            "timeout_ms": 30000,
            "max_model_calls": 2,
            "max_queries": 2,
            "max_corrections": 1,
        },
        "config_id": config_id,
        "context_mode": context_mode,
    }


def _context_evidence(case: dict, context_mode: str) -> dict:
    expected = case["expected"]
    if context_mode == "focused" and expected.get("kind") == "query":
        selected = [expected["query_id"]]
        reason = "matched"
    else:
        selected = list(CATALOG_IDS)
        reason = "full" if context_mode == "full" else "fallback_no_match"
    return {
        "context_mode": context_mode,
        "selected_query_ids": selected,
        "matched_terms": [],
        "reason": reason,
        "semantic_policy_version": SEMANTIC_POLICY_VERSION,
        "registry_sha256": None,
    }


def local_response(case: dict, request: dict | None = None, *, context_mode: str = "full") -> dict:
    expected = case["expected"]
    request = build_request(case, context_mode=context_mode) if request is None else request
    kind = expected["kind"]
    if kind == "query":
        plan = {"kind": "query", "query_id": expected["query_id"], "params": copy_params(expected["params"])}
        table = copy_params(expected["table"])
        answer = "offline local reference example"
        sql = "SELECT /* offline local example */ 1;"
        counts = {"model_calls": 1, "queries": 2, "corrections": 0}
    elif kind == "clarify":
        plan = {"kind": "clarify"}
        table = None
        answer = "offline local clarification example"
        sql = None
        counts = {"model_calls": 1, "queries": 0, "corrections": 0}
    else:
        plan = {"kind": "refuse"}
        table = None
        answer = "offline local refusal example"
        sql = None
        counts = {"model_calls": 1, "queries": 0, "corrections": 0}
    return {
        "schema_version": "0.1",
        "run_id": request["run_id"],
        "request_id": request["request_id"],
        "case_id": case["id"],
        "data_version": DATA_VERSION,
        "reference_time": REFERENCE_TIME,
        "timezone": TIMEZONE,
        "mode": "mock",
        "status": expected["status"],
        "answer": answer,
        "table": table,
        "sql": sql,
        "metrics": [],
        "error": None,
        "timing": {"internal_ms": 0},
        "counts": counts,
        "usage": None,
        "trace": [],
        "config_id": request["config_id"],
        "query_id": expected.get("query_id"),
        "semantic_policy_version": SEMANTIC_POLICY_VERSION,
        "context_mode": context_mode,
        "context_evidence": _context_evidence(case, context_mode),
        "plan": plan,
    }


def copy_params(value):
    """Small deep copy that never shares reference rows with a judge result."""
    if isinstance(value, dict):
        return {key: copy_params(item) for key, item in value.items()}
    if isinstance(value, list):
        return [copy_params(item) for item in value]
    return value


def _actual_action(response: dict) -> tuple[str | None, dict | None]:
    plan = response.get("plan")
    if isinstance(plan, dict):
        kind = plan.get("kind")
        if kind in ("query", "clarify", "refuse"):
            return kind, plan
    status = response.get("status")
    if status == "ok":
        return "query", None
    if status == "needs_clarification":
        return "clarify", None
    if status == "refused":
        return "refuse", None
    return None, None


def _fail(reason: str, **extra) -> dict:
    value = {
        "engineering_verdict": "fail",
        "quality_verdict": "fail",
        "business_verdict": "fail",
        "reason_code": reason,
        "engineering_reason": reason,
    }
    value.update(extra)
    return value


def judge_case(
    case: dict,
    response: object,
    *,
    request: dict | None = None,
    context_mode: str = "full",
    source: str = "offline_example",
) -> dict:
    """Judge one M3 case against the frozen demo reference.

    ``quality_verdict`` is the demo reference verdict; ``business_verdict`` is
    kept as a separate explicit alias with synthetic demo scope, not production
    accuracy.  Clarification wording/slot and answer text are manual/not scored.
    """
    expected = case.get("expected")
    if not isinstance(case, dict) or not isinstance(expected, dict):
        return _fail("invalid_case", case_id=case.get("id") if isinstance(case, dict) else None)
    if not isinstance(response, dict):
        return _fail("response_invalid", case_id=case["id"])
    response_errors = []
    if request is not None:
        try:
            response_errors = validate_response(
                response,
                request=request,
                expected_mode=response.get("mode"),
            )
        except Exception:
            response_errors = [{"code": "response_invalid"}]
    if response_errors:
        return _fail("response_invalid", case_id=case["id"], response_errors=response_errors)

    actual_kind, plan = _actual_action(response)
    expected_kind = expected["kind"]
    status = response.get("status")
    counts = response.get("counts") if isinstance(response.get("counts"), dict) else {}
    base = {
        "case_id": case["id"],
        "suite": case["suite"],
        "context_mode": context_mode,
        "source": source,
        "expected_kind": expected_kind,
        "actual_kind": actual_kind,
        "status": status,
        "query_id": response.get("query_id"),
        "counts": counts,
        "usage": response.get("usage"),
        "timing": response.get("timing"),
        "manual_review": expected_kind == "clarify",
        "clarification_slot_expected": expected.get("clarification_slot"),
        "clarification_slot_observed": response.get("clarification_slot"),
        "answer_text_scored": False,
        "business_verdict_scope": "synthetic_demo_only",
    }

    if status != expected["status"]:
        return _fail("status_mismatch", **base)
    if actual_kind != expected_kind:
        return _fail("action_mismatch", **base)

    if expected_kind == "query":
        query_id = expected["query_id"]
        if response.get("query_id") != query_id:
            return _fail("query_id_mismatch", **base)
        if not params_equal(plan.get("params") if isinstance(plan, dict) else response.get("params"), expected["params"]):
            return _fail("params_mismatch", **base)
        table = response.get("table")
        shape_error = _validate_table_shape(table, query_id)
        if shape_error is not None:
            return _fail("table_" + shape_error, **base)
        comparison = compare_tables(table, expected["table"], row_order="ignore")
        base["table_equal"] = comparison["equal"]
        base["table_reason"] = comparison["reason_code"]
        engineering_ok = comparison["reason_code"] in (
            "equal", "row_count_mismatch", "rows_mismatch"
        )
        if not comparison["equal"]:
            return {
                **base,
                "engineering_verdict": "pass" if engineering_ok else "fail",
                "quality_verdict": "fail",
                "business_verdict": "fail",
                "reason_code": "table_" + comparison["reason_code"],
                "engineering_reason": None if engineering_ok else "table_" + comparison["reason_code"],
            }
        return {
            **base,
            "engineering_verdict": "pass",
            "quality_verdict": "pass",
            "business_verdict": "pass",
            "reason_code": "quality_match",
            "engineering_reason": None,
        }

    if counts.get("queries") != 0:
        return _fail("zero_query_violation", **base)
    if response.get("sql") is not None or response.get("table") is not None:
        return _fail("non_null_sql_or_table", **base)

    if expected_kind == "clarify":
        if not isinstance(response.get("answer"), str) or not response["answer"].strip():
            return _fail("empty_clarification", **base)
        return {
            **base,
            "engineering_verdict": "pass",
            "quality_verdict": "pass",
            "business_verdict": "pass",
            "reason_code": "clarification_match",
            "engineering_reason": None,
            "manual_review": True,
        }

    if plan is None:
        return {
            **base,
            "refusal_mode": "forced",
            "engineering_verdict": "pass",
            "quality_verdict": "fail",
            "business_verdict": "fail",
            "reason_code": "forced_refusal",
            "engineering_reason": None,
        }
    return {
        **base,
        "refusal_mode": "planned",
        "engineering_verdict": "pass",
        "quality_verdict": "pass",
        "business_verdict": "pass",
        "reason_code": "refusal_match",
        "engineering_reason": None,
    }


def judge_local_dataset(doc: dict, *, context_mode: str = "full") -> list[dict]:
    records = []
    for case in doc["cases"]:
        request = build_request(case, context_mode=context_mode)
        response = local_response(case, request, context_mode=context_mode)
        records.append(judge_case(case, response, request=request, context_mode=context_mode))
    return records


# ---------------------------------------------------------------------------
# Read-only A-008 experiment binding


def _read_jsonl(path: Path) -> list:
    data = path.read_bytes()
    if len(data) > MAX_EXPERIMENT_FILE_BYTES or data.startswith(b"\xef\xbb\xbf"):
        raise ReplayError("invalid_results")
    rows = []
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ReplayError("invalid_results")
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = _strict_json(line.encode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ReplayError("invalid_results")
        if not isinstance(value, dict):
            raise ReplayError("invalid_results")
        rows.append(value)
    return rows


def _manifest_sha(value: str) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdef" for char in value.lower()
    )


def _expected_entry_ids() -> set[str]:
    return {case_id + "-" + variant for case_id in PILOT_IDS for variant in VARIANTS}


def _validate_result_row(
    row: dict,
    *,
    variant: str,
    case: dict,
    manifest: dict,
    dataset: dict,
) -> dict:
    if set(row) != {"case_id", "context_mode", "request", "execution_result"}:
        raise ReplayError("result_fields")
    if row["case_id"] != case["id"] or row["context_mode"] != variant:
        raise ReplayError("result_identity")
    request = row["request"]
    result = row["execution_result"]
    if not isinstance(request, dict) or not isinstance(result, dict):
        raise ReplayError("result_shape")
    request_id = case["id"] + "-" + variant
    expected_request = {
        "schema_version": "0.1",
        "run_id": "m3-pilot-001",
        "request_id": request_id,
        "case_id": case["id"],
        "question": case["question"],
        "data_version": DATA_VERSION,
        "reference_time": REFERENCE_TIME,
        "timezone": TIMEZONE,
        "config_id": "m3-siliconflow-qwen30-v1",
    }
    for key, expected in expected_request.items():
        if request.get(key) != expected:
            raise ReplayError("request_binding")
    if request.get("required_capabilities") != []:
        raise ReplayError("request_binding")
    if request.get("context_mode") != variant:
        raise ReplayError("request_binding")
    budget = request.get("budget")
    if not isinstance(budget, dict) or budget != {
        "timeout_ms": 30000,
        "max_model_calls": 2,
        "max_queries": 2,
        "max_corrections": 1,
    }:
        raise ReplayError("request_binding")
    for key, expected in {
        "schema_version": "0.1",
        "run_id": "m3-pilot-001",
        "request_id": request_id,
        "case_id": case["id"],
        "data_version": DATA_VERSION,
        "reference_time": REFERENCE_TIME,
        "timezone": TIMEZONE,
        "config_id": "m3-siliconflow-qwen30-v1",
        "mode": "real",
        "semantic_policy_version": SEMANTIC_POLICY_VERSION,
        "context_mode": variant,
    }.items():
        if result.get(key) != expected:
            raise ReplayError("result_binding")
    if result.get("question") is not None and result.get("question") != case["question"]:
        raise ReplayError("result_binding")
    response_errors = validate_response(result, request=request, expected_mode="real")
    if response_errors:
        raise ReplayError("response_invalid")
    plan = result.get("plan")
    if plan is not None:
        if not isinstance(plan, dict) or plan.get("kind") not in ("query", "clarify", "refuse"):
            raise ReplayError("plan_invalid")
    evidence = result.get("context_evidence")
    if not isinstance(evidence, dict):
        raise ReplayError("context_evidence")
    selected = evidence.get("selected_query_ids")
    if (
        not isinstance(selected, list)
        or not selected
        or any(item not in CATALOG_IDS for item in selected)
        or len(set(selected)) != len(selected)
    ):
        raise ReplayError("context_evidence")
    if variant == "full" and set(selected) != set(CATALOG_IDS):
        raise ReplayError("context_evidence")
    if variant == "focused" and not (1 <= len(selected) <= 3):
        raise ReplayError("context_evidence")
    if evidence.get("context_mode") != variant:
        raise ReplayError("context_evidence")
    if evidence.get("semantic_policy_version") != SEMANTIC_POLICY_VERSION:
        raise ReplayError("context_evidence")
    if evidence.get("registry_sha256") not in (None, manifest["registry_sha256"]):
        raise ReplayError("context_evidence")
    return {
        "case_id": case["id"],
        "context_mode": variant,
        "request": request,
        "execution_result": result,
    }


def load_experiment(
    root: Path | None = None,
    *,
    dataset: dict | None = None,
    budget: dict | None = None,
) -> dict:
    """Strictly bind and load A-008 experiment evidence; no model/DB calls."""
    root = A008_EXPERIMENT if root is None else Path(root)
    if dataset is None:
        dataset = load_dataset()
    if budget is None:
        budget = load_budget()
    if not root.exists():
        return {
            "state": "missing",
            "errors": ["experiment_missing"],
            "root": str(root),
            "records": {},
            "hashes": {},
        }
    try:
        manifest_path = _safe_file(root, "manifest.json")
    except (OSError, ValueError, ReplayError):
        return {
            "state": "missing",
            "errors": ["experiment_missing"],
            "root": str(root),
            "records": {},
            "hashes": {},
        }
    try:
        ledger_path = _safe_file(root, "session-ledger.json")
        manifest = _load_json(manifest_path)
        ledger = _load_json(ledger_path)
    except (OSError, ValueError, ReplayError) as exc:
        raise ReplayError("invalid_manifest") from exc
    if not isinstance(manifest, dict) or not isinstance(ledger, dict):
        raise ReplayError("invalid_manifest")
    required_manifest = {
        "contract_version", "run_id", "config_id", "provider", "model",
        "dataset_sha256", "budget_sha256", "case_ids", "variants",
        "semantic_policy_version", "registry_sha256", "state", "stop_reason",
        "results_sha256", "ledger_sha256",
    }
    if set(manifest) != required_manifest:
        raise ReplayError("manifest_fields")
    if manifest["contract_version"] != CONTRACT_VERSION:
        raise ReplayError("manifest_contract")
    if manifest["run_id"] != "m3-pilot-001":
        raise ReplayError("manifest_run")
    if manifest["config_id"] != "m3-siliconflow-qwen30-v1":
        raise ReplayError("manifest_config")
    if manifest["provider"] != "siliconflow" or manifest["model"] != "Qwen/Qwen3-30B-A3B-Instruct-2507":
        raise ReplayError("manifest_provider")
    if manifest["dataset_sha256"] != DATASET_SHA256 or _sha_file(DATASET_PATH) != DATASET_SHA256:
        raise ReplayError("manifest_dataset")
    if manifest["budget_sha256"] != BUDGET_SHA256 or _sha_file(BUDGET_PATH) != BUDGET_SHA256:
        raise ReplayError("manifest_budget")
    if manifest["case_ids"] != list(PILOT_IDS):
        raise ReplayError("manifest_cases")
    if manifest["variants"] != list(VARIANTS):
        raise ReplayError("manifest_variants")
    if manifest["semantic_policy_version"] != SEMANTIC_POLICY_VERSION:
        raise ReplayError("manifest_policy")
    if not _manifest_sha(manifest["registry_sha256"]):
        raise ReplayError("manifest_registry")
    if manifest["state"] not in ("complete", "incomplete", "blocked"):
        raise ReplayError("manifest_state")
    if manifest["stop_reason"] is not None and not isinstance(manifest["stop_reason"], str):
        raise ReplayError("manifest_stop_reason")
    if not isinstance(manifest["results_sha256"], dict) or set(manifest["results_sha256"]) != set(VARIANTS):
        raise ReplayError("manifest_results_sha")
    if manifest["ledger_sha256"] != _sha_file(ledger_path):
        raise ReplayError("manifest_ledger_sha")
    if set(ledger) < {"run_id", "dataset_sha256", "budget_sha256", "reserved_requests", "reserved_attempts", "entries"}:
        raise ReplayError("ledger_fields")
    if ledger["run_id"] != "m3-pilot-001" or ledger["dataset_sha256"] != DATASET_SHA256 or ledger["budget_sha256"] != BUDGET_SHA256:
        raise ReplayError("ledger_binding")
    if not _is_int(ledger["reserved_requests"]) or not 0 <= ledger["reserved_requests"] <= 24:
        raise ReplayError("ledger_budget")
    if not _is_int(ledger["reserved_attempts"]) or not 0 <= ledger["reserved_attempts"] <= 48:
        raise ReplayError("ledger_budget")
    entries = ledger["entries"]
    if isinstance(entries, dict):
        entry_iter = [{"request_id": key, **value} for key, value in entries.items()]
    elif isinstance(entries, list):
        entry_iter = entries
    else:
        raise ReplayError("ledger_entries")
    entry_map = {}
    for entry in entry_iter:
        if not isinstance(entry, dict) or set(entry) < {"request_id", "case_id", "context_mode", "state", "observed_attempts"}:
            raise ReplayError("ledger_entries")
        request_id = entry["request_id"]
        if request_id not in _expected_entry_ids() or request_id in entry_map:
            raise ReplayError("ledger_entries")
        if entry["case_id"] + "-" + entry["context_mode"] != request_id:
            raise ReplayError("ledger_entries")
        if entry["state"] not in ("reserved", "observed"):
            raise ReplayError("ledger_entries")
        observed = entry["observed_attempts"]
        if observed is not None and (not _is_int(observed) or not 0 <= observed <= 2):
            raise ReplayError("ledger_entries")
        entry_map[request_id] = entry

    hashes = {
        "manifest_sha256": _sha_file(manifest_path),
        "ledger_sha256": _sha_file(ledger_path),
        "dataset_sha256": DATASET_SHA256,
        "budget_sha256": BUDGET_SHA256,
    }
    records = {}
    result_hashes = {}
    for variant in VARIANTS:
        expected_hash = manifest["results_sha256"][variant]
        if expected_hash is None:
            result_hashes[variant] = None
            continue
        if not _manifest_sha(expected_hash):
            raise ReplayError("manifest_results_sha")
        path_name = "%s/results.jsonl" % variant
        try:
            path = _safe_file(root, path_name)
        except ReplayError:
            raise ReplayError("results_missing")
        if _sha_file(path) != expected_hash:
            raise ReplayError("results_sha")
        result_hashes[variant] = expected_hash
        rows = _read_jsonl(path)
        if len(rows) > len(PILOT_IDS):
            raise ReplayError("results_count")
        seen = set()
        for row in rows:
            case_id = row.get("case_id") if isinstance(row, dict) else None
            if case_id not in PILOT_IDS or case_id in seen:
                raise ReplayError("results_case")
            case = next(item for item in dataset["cases"] if item["id"] == case_id)
            records.setdefault(variant, {})[case_id] = _validate_result_row(
                row, variant=variant, case=case, manifest=manifest, dataset=dataset
            )
            seen.add(case_id)
    for variant in VARIANTS:
        variant_records = records.get(variant, {})
        for case_id in variant_records:
            entry = entry_map.get(case_id + "-" + variant)
            if entry is None or entry["state"] != "observed":
                raise ReplayError("ledger_observed")
    if manifest["state"] == "complete":
        for variant in VARIANTS:
            if len(records.get(variant, {})) != len(PILOT_IDS):
                raise ReplayError("results_incomplete")
    return {
        "state": manifest["state"],
        "manifest": manifest,
        "ledger": ledger,
        "records": records,
        "hashes": hashes,
        "result_hashes": result_hashes,
        "root": str(root),
    }

