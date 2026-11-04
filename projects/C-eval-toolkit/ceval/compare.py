"""Self-contained exact table comparison for the offline C demo.

This is a small, dependency-free copy of the public C type-normalization rules:
Decimal strings as Decimal, floats through Decimal(str(value)), dates as date,
and offset date-times as UTC instants. No tolerance or fuzzy matching is used.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal

COLUMN_TYPES = ("string", "integer", "decimal", "float", "date", "datetime", "boolean")


def _valid_table(table: object) -> bool:
    if not isinstance(table, dict):
        return False
    if set(table) != {"columns", "rows", "truncated", "truncation_reason"}:
        return False
    columns = table.get("columns")
    rows = table.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        return False
    if table.get("truncated") is not False or table.get("truncation_reason") is not None:
        return False
    for column in columns:
        if not isinstance(column, dict) or set(column) != {"name", "type", "nullable"}:
            return False
        if not isinstance(column["name"], str) or column["type"] not in COLUMN_TYPES:
            return False
        if not isinstance(column["nullable"], bool):
            return False
    for row in rows:
        if not isinstance(row, list) or len(row) != len(columns):
            return False
        for value, column in zip(row, columns):
            if value is None:
                if not column["nullable"]:
                    return False
            elif column["type"] in ("string", "date", "datetime"):
                if not isinstance(value, str):
                    return False
            elif column["type"] in ("integer", "decimal", "float"):
                if not isinstance(value, (int, str)) or isinstance(value, bool):
                    return False
            elif not isinstance(value, bool):
                return False
    return True


def _datetime_key(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(normalized).astimezone(timezone.utc)


def _normalize(cell: object, column_type: str):
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


def compare_tables(actual: object, expected: object, *, row_order: str = "ignore") -> dict:
    if row_order not in ("ignore", "preserve"):
        return {"equal": False, "reason_code": "invalid_options"}
    if not _valid_table(actual) or not _valid_table(expected):
        return {"equal": False, "reason_code": "invalid_table"}
    actual_columns = [
        (column["name"], column["type"], column["nullable"])
        for column in actual["columns"]
    ]
    expected_columns = [
        (column["name"], column["type"], column["nullable"])
        for column in expected["columns"]
    ]
    if actual_columns != expected_columns:
        return {"equal": False, "reason_code": "columns_mismatch"}
    if actual["truncated"] or expected["truncated"]:
        return {"equal": False, "reason_code": "incomplete_result"}
    if len(actual["rows"]) != len(expected["rows"]):
        return {"equal": False, "reason_code": "row_count_mismatch"}
    try:
        actual_rows = [
            tuple(_normalize(cell, column["type"]) for cell, column in zip(row, actual["columns"]))
            for row in actual["rows"]
        ]
        expected_rows = [
            tuple(_normalize(cell, column["type"]) for cell, column in zip(row, expected["columns"]))
            for row in expected["rows"]
        ]
    except (ValueError, ArithmeticError, TypeError, OverflowError):
        return {"equal": False, "reason_code": "invalid_table"}
    if row_order == "preserve":
        equal = actual_rows == expected_rows
    else:
        equal = Counter(actual_rows) == Counter(expected_rows)
    return {"equal": equal, "reason_code": "equal" if equal else "rows_mismatch"}
