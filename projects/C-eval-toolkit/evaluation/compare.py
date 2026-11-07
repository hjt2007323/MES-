"""Exact table comparison for deterministic mock results.

The comparator reuses the public :func:`contracts.validation.validate_response`
for table shape validation; it does not duplicate field validation.  Values are
compared exactly after type-directed normalization: Decimal strings as
``Decimal``, floats through ``Decimal(str(value))``, dates as ``date`` and
offset date-times as UTC instants.  No tolerance or fuzzy matching is used.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal

from contracts.validation import validate_response

_ROW_ORDERS = ("ignore", "preserve")

_MINIMAL_REQUEST = {
    "schema_version": "0.1",
    "run_id": "compare-run",
    "request_id": "compare-request",
    "case_id": "compare-case",
    "question": "synthetic table comparison",
    "data_version": "compare-fixture-v1",
    "reference_time": "2026-11-02T12:00:00+08:00",
    "timezone": "Asia/Shanghai",
    "required_capabilities": [],
    "budget": {
        "timeout_ms": 1000,
        "max_model_calls": 0,
        "max_queries": 0,
        "max_corrections": 0,
    },
    "config_id": "compare-v1",
}


def _wrap_table(table: dict) -> dict:
    return {
        "schema_version": _MINIMAL_REQUEST["schema_version"],
        "run_id": _MINIMAL_REQUEST["run_id"],
        "request_id": _MINIMAL_REQUEST["request_id"],
        "case_id": _MINIMAL_REQUEST["case_id"],
        "data_version": _MINIMAL_REQUEST["data_version"],
        "reference_time": _MINIMAL_REQUEST["reference_time"],
        "timezone": _MINIMAL_REQUEST["timezone"],
        "mode": "mock",
        "status": "ok",
        "answer": None,
        "table": table,
        "sql": None,
        "metrics": [],
        "error": None,
        "timing": {"internal_ms": 0},
        "counts": {"model_calls": 0, "queries": 0, "corrections": 0},
        "usage": None,
        "trace": [],
        "config_id": _MINIMAL_REQUEST["config_id"],
    }


def _table_is_valid(table: object) -> bool:
    if not isinstance(table, dict):
        return False
    return not validate_response(
        _wrap_table(table),
        request=_MINIMAL_REQUEST,
        expected_mode="mock",
    )


def _datetime_key(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(normalized).astimezone(timezone.utc)


def _normalize_cell(cell: object, column_type: str) -> object:
    if cell is None:
        return None
    if column_type in ("string", "integer", "boolean"):
        return cell
    if column_type == "decimal":
        return Decimal(cell)
    if column_type == "float":
        return Decimal(str(cell))
    if column_type == "date":
        return date.fromisoformat(cell)
    if column_type == "datetime":
        return _datetime_key(cell)
    raise ValueError("unknown column type")


def _normalize_rows(table: dict) -> list[tuple]:
    columns = table["columns"]
    normalized: list[tuple] = []
    for row in table["rows"]:
        normalized.append(
            tuple(
                _normalize_cell(cell, column["type"])
                for cell, column in zip(row, columns)
            )
        )
    return normalized


def _result(equal: bool, reason_code: str) -> dict:
    return {"equal": equal, "reason_code": reason_code}


def compare_tables(
    actual: object, expected: object, *, row_order: str = "ignore"
) -> dict:
    """Compare two deterministic mock tables exactly.

    Returns ``{"equal": bool, "reason_code": str}``.  Bad options return
    ``invalid_options``; bad tables return ``invalid_table``.  Payload values
    never raise out of this function.
    """
    if row_order not in _ROW_ORDERS:
        return _result(False, "invalid_options")
    if not _table_is_valid(actual) or not _table_is_valid(expected):
        return _result(False, "invalid_table")

    actual_columns = [
        (column["name"], column["type"], column["nullable"])
        for column in actual["columns"]
    ]
    expected_columns = [
        (column["name"], column["type"], column["nullable"])
        for column in expected["columns"]
    ]
    if actual_columns != expected_columns:
        return _result(False, "columns_mismatch")

    if actual["truncated"] or expected["truncated"]:
        return _result(False, "incomplete_result")

    if len(actual["rows"]) != len(expected["rows"]):
        return _result(False, "row_count_mismatch")

    try:
        actual_rows = _normalize_rows(actual)
        expected_rows = _normalize_rows(expected)
    except (ValueError, ArithmeticError, TypeError, OverflowError):
        return _result(False, "invalid_table")

    if row_order == "preserve":
        equal = actual_rows == expected_rows
    else:
        equal = Counter(actual_rows) == Counter(expected_rows)
    return _result(equal, "equal" if equal else "rows_mismatch")
