"""Field-level validation for the C evaluation toolkit protocol schema 0.1.

The module is intentionally limited to validating request and response fields
and their cross-field consistency.  It does not execute budgets, cancel
adapters, run SQL, grade answers, or compile reports, and it never mutates the
objects it receives.

Public API
----------
``validate_request(payload)``
    Validate one protocol request object.
``validate_response(payload, *, request, expected_mode)``
    Validate one protocol response object against a valid request and the mode
    selected by the runner.

Errors are dicts with exactly the keys ``path``, ``code`` and ``message``.
``path`` is a ``$``-rooted JSON path, ``code`` is one of ``missing``, ``type``,
``value``, ``mismatch`` or ``unsupported_timezone``, and the list is sorted by
``(path, code, message)``.  An empty list means the fields are valid.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = ["validate_request", "validate_response"]

SCHEMA_VERSION = "0.1"
MODES = ("mock", "real")
TERMINAL_STATUSES = (
    "ok",
    "needs_clarification",
    "refused",
    "error",
    "timeout",
    "budget_exhausted",
)
ERROR_REQUIRED_STATUSES = frozenset({"error", "timeout", "budget_exhausted"})
COLUMN_TYPES = (
    "string",
    "integer",
    "decimal",
    "float",
    "boolean",
    "date",
    "datetime",
)

_CONTEXT_FIELDS = (
    "schema_version",
    "run_id",
    "request_id",
    "case_id",
    "data_version",
    "reference_time",
    "timezone",
    "config_id",
)

_DATE_TEXT = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_OFFSET = re.compile(r"(?:Z|[+-]([0-9]{2}):([0-9]{2}))\Z")

# Sentinel distinguishing "timezone data unavailable" from a real offset.
_UNSUPPORTED_TIMEZONE = object()


def _error(path: str, code: str, message: str) -> dict[str, str]:
    return {"path": path, "code": code, "message": message}


def _result(errors: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(errors, key=lambda item: (item["path"], item["code"], item["message"]))


def _missing(path: str, name: str) -> dict[str, str]:
    return _error(path, "missing", f"required field {name!r} is missing")


def _wrong_type(path: str, message: str) -> dict[str, str]:
    return _error(path, "type", message)


def _is_int(value: object) -> bool:
    """True for Python integers only; ``bool`` never counts as an integer."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    """True for int/float only; ``bool`` and ``Decimal`` do not count."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_finite_number(value: object) -> bool:
    if not _is_number(value):
        return False
    if isinstance(value, int):
        return True
    return math.isfinite(value)


def _required_string(
    payload: dict,
    name: str,
    path: str,
    errors: list[dict[str, str]],
    *,
    allow_empty: bool = False,
) -> str | None:
    if name not in payload:
        errors.append(_missing(path, name))
        return None
    value = payload[name]
    if not isinstance(value, str):
        errors.append(_wrong_type(path, f"{name} must be a string"))
        return None
    if not allow_empty and value == "":
        errors.append(_error(path, "value", f"{name} must not be empty"))
        return None
    return value


def _required_integer(
    payload: dict,
    name: str,
    path: str,
    errors: list[dict[str, str]],
    *,
    minimum: int,
) -> None:
    if name not in payload:
        errors.append(_missing(path, name))
        return
    value = payload[name]
    if not _is_int(value):
        errors.append(
            _wrong_type(path, f"{name} must be an integer (bool is not an integer)")
        )
        return
    if value < minimum:
        errors.append(_error(path, "value", f"{name} must be >= {minimum}"))


def _nullable_nonnegative_integer(
    payload: dict,
    name: str,
    path: str,
    errors: list[dict[str, str]],
) -> None:
    if name not in payload:
        errors.append(_missing(path, name))
        return
    value = payload[name]
    if value is None:
        return
    if not _is_int(value):
        errors.append(
            _wrong_type(path, f"{name} must be a non-negative integer or null")
        )
        return
    if value < 0:
        errors.append(_error(path, "value", f"{name} must be >= 0"))


def _parse_datetime(text: object) -> datetime | None:
    """Parse an ISO 8601 date-time that must carry a UTC offset.

    ``Z`` and ``+/-HH:MM`` offsets are accepted.  Naive date-times and
    date-only strings are rejected.

    The trailing offset is range-checked before ``fromisoformat`` because the
    parser normalizes out-of-range offsets (for example ``+07:60`` becomes
    ``+08:00``); accepting that would hide an invalid protocol value.
    """
    if not isinstance(text, str):
        return None
    offset_match = _DATETIME_OFFSET.search(text)
    if offset_match is None:
        return None
    if offset_match.group(1) is not None:
        offset_hours = int(offset_match.group(1))
        offset_minutes = int(offset_match.group(2))
        if not (0 <= offset_hours <= 23 and 0 <= offset_minutes <= 59):
            return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _expected_offset(name: str, moment: datetime) -> object:
    """Return the offset expected for ``name`` at ``moment``.

    The C-001 card freezes Asia/Shanghai at UTC+08:00 for 2026 samples and
    UTC/Etc/UTC at UTC+00:00.  All other names, including Asia/Shanghai
    outside 2026, are resolved with :mod:`zoneinfo`; unavailable data is
    reported as ``unsupported_timezone`` instead of being guessed.
    """
    if name == "Asia/Shanghai" and moment.year == 2026:
        return timedelta(hours=8)
    if name in ("UTC", "Etc/UTC"):
        return timedelta(0)
    try:
        zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError, OSError):
        return _UNSUPPORTED_TIMEZONE
    try:
        localized = moment.astimezone(zone)
    except (ValueError, OverflowError):
        return _UNSUPPORTED_TIMEZONE
    return localized.utcoffset()


def _validate_schema_version(
    payload: dict, path: str, errors: list[dict[str, str]]
) -> None:
    value = _required_string(payload, "schema_version", path, errors)
    if value is not None and value != SCHEMA_VERSION:
        errors.append(
            _error(path, "value", f"schema_version must be {SCHEMA_VERSION!r}")
        )


def _validate_capabilities(payload: dict, errors: list[dict[str, str]]) -> None:
    if "required_capabilities" not in payload:
        errors.append(_missing("$.required_capabilities", "required_capabilities"))
        return
    capabilities = payload["required_capabilities"]
    if not isinstance(capabilities, list):
        errors.append(
            _wrong_type(
                "$.required_capabilities",
                "required_capabilities must be an array of non-empty strings",
            )
        )
        return
    seen: set[str] = set()
    for index, item in enumerate(capabilities):
        path = f"$.required_capabilities[{index}]"
        if not isinstance(item, str):
            errors.append(_wrong_type(path, "capability must be a string"))
        elif item == "":
            errors.append(_error(path, "value", "capability must not be empty"))
        elif item in seen:
            errors.append(_error(path, "value", f"duplicate capability {item!r}"))
        else:
            seen.add(item)


def _validate_budget(payload: dict, errors: list[dict[str, str]]) -> None:
    if "budget" not in payload:
        errors.append(_missing("$.budget", "budget"))
        return
    budget = payload["budget"]
    if not isinstance(budget, dict):
        errors.append(_wrong_type("$.budget", "budget must be an object"))
        return
    _required_integer(
        budget, "timeout_ms", "$.budget.timeout_ms", errors, minimum=1
    )
    for name in ("max_model_calls", "max_queries", "max_corrections"):
        _required_integer(
            budget, name, f"$.budget.{name}", errors, minimum=0
        )


def _validate_request_fields(payload: dict, errors: list[dict[str, str]]) -> None:
    _validate_schema_version(payload, "$.schema_version", errors)
    for name in (
        "run_id",
        "request_id",
        "case_id",
        "question",
        "data_version",
    ):
        _required_string(payload, name, f"$.{name}", errors)

    moment: datetime | None = None
    if "reference_time" not in payload:
        errors.append(_missing("$.reference_time", "reference_time"))
    elif not isinstance(payload["reference_time"], str):
        errors.append(
            _wrong_type(
                "$.reference_time",
                "reference_time must be an ISO 8601 date-time string with an offset",
            )
        )
    else:
        moment = _parse_datetime(payload["reference_time"])
        if moment is None:
            errors.append(
                _error(
                    "$.reference_time",
                    "value",
                    "reference_time must be a valid ISO 8601 date-time with a Z or +/-HH:MM offset",
                )
            )

    timezone_name = _required_string(payload, "timezone", "$.timezone", errors)
    if timezone_name is not None and moment is not None:
        expected = _expected_offset(timezone_name, moment)
        if expected is _UNSUPPORTED_TIMEZONE:
            errors.append(
                _error(
                    "$.timezone",
                    "unsupported_timezone",
                    f"timezone data for {timezone_name!r} is unavailable or unsupported",
                )
            )
        elif expected is None or moment.utcoffset() != expected:
            errors.append(
                _error(
                    "$.timezone",
                    "mismatch",
                    "timezone offset does not match reference_time",
                )
            )

    _validate_capabilities(payload, errors)
    _validate_budget(payload, errors)
    _required_string(payload, "config_id", "$.config_id", errors)


def validate_request(payload: object) -> list[dict[str, str]]:
    """Validate one protocol request object.

    Returns a sorted list of errors; ``[]`` means every checked field is valid.
    The input is never modified and user input never raises.
    """
    if not isinstance(payload, dict):
        return [_wrong_type("$", "request must be a JSON object")]
    errors: list[dict[str, str]] = []
    _validate_request_fields(payload, errors)
    return _result(errors)


def _validate_column(
    column: dict,
    base: str,
    names: set[str],
    errors: list[dict[str, str]],
) -> tuple[str, bool] | None:
    if "name" not in column:
        errors.append(_missing(f"{base}.name", "name"))
    elif not isinstance(column["name"], str):
        errors.append(_wrong_type(f"{base}.name", "column name must be a string"))
    elif column["name"] == "":
        errors.append(_error(f"{base}.name", "value", "column name must not be empty"))
    elif column["name"] in names:
        errors.append(
            _error(f"{base}.name", "value", f"duplicate column name {column['name']!r}")
        )
    else:
        names.add(column["name"])

    column_type: str | None = None
    if "type" not in column:
        errors.append(_missing(f"{base}.type", "type"))
    elif not isinstance(column["type"], str):
        errors.append(_wrong_type(f"{base}.type", "column type must be a string"))
    elif column["type"] not in COLUMN_TYPES:
        errors.append(
            _error(f"{base}.type", "value", f"unsupported column type {column['type']!r}")
        )
    else:
        column_type = column["type"]

    nullable: bool | None = None
    if "nullable" not in column:
        errors.append(_missing(f"{base}.nullable", "nullable"))
    elif not isinstance(column["nullable"], bool):
        errors.append(_wrong_type(f"{base}.nullable", "nullable must be a boolean"))
    else:
        nullable = column["nullable"]

    if column_type is None or nullable is None:
        return None
    return (column_type, nullable)


def _validate_cell(
    cell: object,
    column_type: str,
    nullable: bool,
    path: str,
    errors: list[dict[str, str]],
) -> None:
    if cell is None:
        if not nullable:
            errors.append(
                _error(path, "value", "null is only allowed in a nullable column")
            )
        return

    if column_type == "string":
        if not isinstance(cell, str):
            errors.append(_wrong_type(path, "cell must be a string"))
    elif column_type == "integer":
        if not _is_int(cell):
            errors.append(_error(path, "type", "cell must be an integer (bool is not an integer)"))
    elif column_type == "decimal":
        if not isinstance(cell, str):
            errors.append(_error(path, "type", "decimal cell must be a string"))
            return
        try:
            parsed = Decimal(cell)
        except (InvalidOperation, ValueError):
            errors.append(_error(path, "value", "decimal cell is not a parsable decimal string"))
            return
        if not parsed.is_finite():
            errors.append(_error(path, "value", "decimal cell must be finite"))
    elif column_type == "float":
        if not _is_number(cell):
            errors.append(_error(path, "type", "cell must be a finite int or float"))
        elif not _is_finite_number(cell):
            errors.append(_error(path, "value", "float cell must be finite"))
    elif column_type == "boolean":
        if not isinstance(cell, bool):
            errors.append(_wrong_type(path, "cell must be a boolean"))
    elif column_type == "date":
        if not isinstance(cell, str):
            errors.append(_error(path, "type", "date cell must be a string"))
        elif not _DATE_TEXT.fullmatch(cell):
            errors.append(_error(path, "value", "date cell must use YYYY-MM-DD"))
        else:
            try:
                date.fromisoformat(cell)
            except ValueError:
                errors.append(_error(path, "value", "date cell is not a valid calendar date"))
    elif column_type == "datetime":
        if not isinstance(cell, str):
            errors.append(_error(path, "type", "datetime cell must be a string"))
        elif _parse_datetime(cell) is None:
            errors.append(
                _error(
                    path,
                    "value",
                    "datetime cell must be ISO 8601 with a Z or +/-HH:MM offset",
                )
            )


def _validate_table(table: dict, errors: list[dict[str, str]]) -> None:
    columns: list | None = None
    column_specs: list[tuple[str, bool] | None] = []
    if "columns" not in table:
        errors.append(_missing("$.table.columns", "columns"))
    elif not isinstance(table["columns"], list):
        errors.append(_wrong_type("$.table.columns", "columns must be an array"))
    else:
        columns = table["columns"]
        names: set[str] = set()
        for index, column in enumerate(columns):
            base = f"$.table.columns[{index}]"
            if not isinstance(column, dict):
                errors.append(_wrong_type(base, "column must be an object"))
                column_specs.append(None)
                continue
            column_specs.append(_validate_column(column, base, names, errors))

    if "rows" not in table:
        errors.append(_missing("$.table.rows", "rows"))
    elif not isinstance(table["rows"], list):
        errors.append(_wrong_type("$.table.rows", "rows must be an array"))
    else:
        for row_index, row in enumerate(table["rows"]):
            row_path = f"$.table.rows[{row_index}]"
            if not isinstance(row, list):
                errors.append(_wrong_type(row_path, "row must be an array"))
                continue
            if columns is not None and len(row) != len(columns):
                errors.append(
                    _error(
                        row_path,
                        "value",
                        f"row has {len(row)} cells but there are {len(columns)} columns",
                    )
                )
            if columns is None:
                continue
            for cell_index, cell in enumerate(row):
                if cell_index >= len(column_specs):
                    break
                spec = column_specs[cell_index]
                if spec is None:
                    continue
                _validate_cell(
                    cell,
                    spec[0],
                    spec[1],
                    f"{row_path}[{cell_index}]",
                    errors,
                )

    truncated: bool | None = None
    if "truncated" not in table:
        errors.append(_missing("$.table.truncated", "truncated"))
    elif not isinstance(table["truncated"], bool):
        errors.append(_wrong_type("$.table.truncated", "truncated must be a boolean"))
    else:
        truncated = table["truncated"]

    reason_valid = False
    reason_value: str | None = None
    if "truncation_reason" not in table:
        errors.append(_missing("$.table.truncation_reason", "truncation_reason"))
    elif table["truncation_reason"] is None:
        reason_valid = True
    elif not isinstance(table["truncation_reason"], str):
        errors.append(
            _wrong_type(
                "$.table.truncation_reason",
                "truncation_reason must be a string or null",
            )
        )
    else:
        reason_value = table["truncation_reason"]
        reason_valid = True

    if truncated is True and reason_valid and not reason_value:
        errors.append(
            _error(
                "$.table.truncation_reason",
                "value",
                "a non-empty truncation_reason is required when truncated is true",
            )
        )


def _validate_metrics(payload: dict, errors: list[dict[str, str]]) -> None:
    if "metrics" not in payload:
        errors.append(_missing("$.metrics", "metrics"))
        return
    metrics = payload["metrics"]
    if not isinstance(metrics, list):
        errors.append(_wrong_type("$.metrics", "metrics must be an array"))
        return
    for index, item in enumerate(metrics):
        base = f"$.metrics[{index}]"
        if not isinstance(item, dict):
            errors.append(_wrong_type(base, "metric entry must be an object"))
            continue
        for name in ("id", "definition_version"):
            if name not in item:
                errors.append(_missing(f"{base}.{name}", name))
            elif not isinstance(item[name], str):
                errors.append(_wrong_type(f"{base}.{name}", f"{name} must be a string"))


def _validate_error(
    payload: dict, status: str | None, errors: list[dict[str, str]]
) -> None:
    if "error" not in payload:
        errors.append(_missing("$.error", "error"))
        return
    value = payload["error"]
    if value is None:
        if status in ERROR_REQUIRED_STATUSES:
            errors.append(
                _error(
                    "$.error",
                    "value",
                    f"error must be a non-null object when status is {status!r}",
                )
            )
        return
    if not isinstance(value, dict):
        errors.append(_wrong_type("$.error", "error must be an object or null"))
        return
    for name in ("code", "stage", "message"):
        if name not in value:
            errors.append(_missing(f"$.error.{name}", name))
        elif not isinstance(value[name], str):
            errors.append(_wrong_type(f"$.error.{name}", f"{name} must be a string"))
    if "retryable" not in value:
        errors.append(_missing("$.error.retryable", "retryable"))
    elif not isinstance(value["retryable"], bool):
        errors.append(_wrong_type("$.error.retryable", "retryable must be a boolean"))


def _validate_timing(payload: dict, errors: list[dict[str, str]]) -> None:
    if "timing" not in payload:
        errors.append(_missing("$.timing", "timing"))
        return
    timing = payload["timing"]
    if not isinstance(timing, dict):
        errors.append(_wrong_type("$.timing", "timing must be an object"))
        return
    if "internal_ms" not in timing:
        errors.append(_missing("$.timing.internal_ms", "internal_ms"))
        return
    value = timing["internal_ms"]
    if value is None:
        return
    if not _is_number(value):
        errors.append(
            _wrong_type(
                "$.timing.internal_ms",
                "internal_ms must be a non-negative number or null",
            )
        )
        return
    if not _is_finite_number(value):
        errors.append(_error("$.timing.internal_ms", "value", "internal_ms must be finite"))
        return
    if value < 0:
        errors.append(_error("$.timing.internal_ms", "value", "internal_ms must be >= 0"))


def _validate_counts(payload: dict, errors: list[dict[str, str]]) -> None:
    if "counts" not in payload:
        errors.append(_missing("$.counts", "counts"))
        return
    counts = payload["counts"]
    if not isinstance(counts, dict):
        errors.append(_wrong_type("$.counts", "counts must be an object"))
        return
    for name in ("model_calls", "queries", "corrections"):
        _nullable_nonnegative_integer(counts, name, f"$.counts.{name}", errors)


def _validate_usage(payload: dict, errors: list[dict[str, str]]) -> None:
    if "usage" not in payload:
        errors.append(_missing("$.usage", "usage"))
        return
    usage = payload["usage"]
    if usage is None:
        return
    if not isinstance(usage, dict):
        errors.append(_wrong_type("$.usage", "usage must be an object or null"))
        return
    for name in ("input_tokens", "output_tokens"):
        _nullable_nonnegative_integer(usage, name, f"$.usage.{name}", errors)


def _validate_trace(payload: dict, errors: list[dict[str, str]]) -> None:
    if "trace" not in payload:
        errors.append(_missing("$.trace", "trace"))
        return
    trace = payload["trace"]
    if not isinstance(trace, list):
        errors.append(_wrong_type("$.trace", "trace must be an array"))
        return
    for index, item in enumerate(trace):
        if not isinstance(item, dict):
            errors.append(
                _wrong_type(f"$.trace[{index}]", "trace entry must be an object")
            )


def _validate_response_fields(
    payload: dict, request: dict, expected_mode: str, errors: list[dict[str, str]]
) -> None:
    for name in _CONTEXT_FIELDS:
        path = f"$.{name}"
        if name not in payload:
            errors.append(_missing(path, name))
        elif not isinstance(payload[name], str):
            errors.append(_wrong_type(path, f"{name} must be a string"))
        elif payload[name] != request[name]:
            errors.append(_error(path, "mismatch", f"{name} must match the request value"))

    if "mode" not in payload:
        errors.append(_missing("$.mode", "mode"))
    elif not isinstance(payload["mode"], str):
        errors.append(_wrong_type("$.mode", "mode must be a string"))
    elif payload["mode"] not in MODES:
        errors.append(_error("$.mode", "value", "mode must be 'mock' or 'real'"))
    elif payload["mode"] != expected_mode:
        errors.append(_error("$.mode", "mismatch", f"mode must equal expected_mode {expected_mode!r}"))

    status: str | None = None
    if "status" not in payload:
        errors.append(_missing("$.status", "status"))
    elif not isinstance(payload["status"], str):
        errors.append(_wrong_type("$.status", "status must be a string"))
    elif payload["status"] not in TERMINAL_STATUSES:
        errors.append(_error("$.status", "value", "status must be one of the six terminal statuses"))
    else:
        status = payload["status"]

    if "answer" not in payload:
        errors.append(_missing("$.answer", "answer"))
    else:
        answer = payload["answer"]
        if answer is not None and not isinstance(answer, str):
            errors.append(_wrong_type("$.answer", "answer must be a string or null"))
        elif status == "needs_clarification" and not answer:
            errors.append(
                _error(
                    "$.answer",
                    "value",
                    "needs_clarification requires a non-empty answer with the missing information",
                )
            )

    if "table" not in payload:
        errors.append(_missing("$.table", "table"))
    else:
        table = payload["table"]
        if table is not None:
            if not isinstance(table, dict):
                errors.append(_wrong_type("$.table", "table must be an object or null"))
            else:
                _validate_table(table, errors)

    if "sql" not in payload:
        errors.append(_missing("$.sql", "sql"))
    elif payload["sql"] is not None and not isinstance(payload["sql"], str):
        errors.append(_wrong_type("$.sql", "sql must be a string or null"))

    _validate_metrics(payload, errors)
    _validate_error(payload, status, errors)
    _validate_timing(payload, errors)
    _validate_counts(payload, errors)
    _validate_usage(payload, errors)
    _validate_trace(payload, errors)


def validate_response(
    payload: object, *, request: object, expected_mode: str
) -> list[dict[str, str]]:
    """Validate one protocol response against a request and expected mode.

    ``expected_mode`` must be ``"mock"`` or ``"real"``; otherwise only
    ``$expected_mode`` is reported.  An invalid request is reported under the
    ``$request`` root and response fields are not checked in that case.
    """
    if expected_mode not in MODES:
        return [_error("$expected_mode", "value", "expected_mode must be 'mock' or 'real'")]

    request_errors = validate_request(request)
    if request_errors:
        prefixed: list[dict[str, str]] = []
        for item in request_errors:
            path = "$request" if item["path"] == "$" else "$request" + item["path"][1:]
            prefixed.append(_error(path, item["code"], item["message"]))
        return _result(prefixed)

    if not isinstance(payload, dict):
        return [_wrong_type("$", "response must be a JSON object")]

    # validate_request() guarantees the request is a dict with string context
    # fields, so indexing it here cannot fail on user input.
    errors: list[dict[str, str]] = []
    _validate_response_fields(payload, request, expected_mode, errors)
    return _result(errors)
