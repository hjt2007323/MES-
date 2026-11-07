"""A07 real-mode adapter for C-004.

This module is the only C-side bridge to A's public runtime.  It inserts the
fixed sibling A project path, imports only public functions, and converts one
runtime case into an A ``run_query`` call with a memory log sink.  It never
opens SQL, credentials, or an input-selected module.
"""

from __future__ import annotations

import copy
import io
import sys
from pathlib import Path

_A_PROJECT = Path(__file__).resolve().parents[2] / "A-mes-assistant"
if str(_A_PROJECT) not in sys.path:
    sys.path.insert(0, str(_A_PROJECT))

from mes_assistant.mysql_tools import run_named_query as _run_named_query
from mes_assistant.query_runtime import MonotonicClock as _MonotonicClock
from mes_assistant.query_runtime import run_query as _run_query

QUERY_IDS = ("line_snapshot", "work_order_progress", "downtime_details")
_INVALID_TARGET = "invalid real target"


def invoke_named_case(case: dict) -> dict:
    """Invoke A's public named-query runtime for one real-mode case.

    ``case["target"]`` must be exactly ``{"query_id", "params"}`` with one of
    the three frozen query IDs and an object params value.  ``params`` is
    validated by A's public runtime, not reimplemented here.  The A log sink is
    an in-memory stream, so this adapter writes no log files.
    """
    if not isinstance(case, dict):
        raise ValueError(_INVALID_TARGET)
    target = case.get("target")
    if not isinstance(target, dict) or set(target) != {"query_id", "params"}:
        raise ValueError(_INVALID_TARGET)
    query_id = target["query_id"]
    params = target["params"]
    if query_id not in QUERY_IDS or not isinstance(params, dict):
        raise ValueError(_INVALID_TARGET)
    request = case.get("request")
    if not isinstance(request, dict):
        raise ValueError(_INVALID_TARGET)
    budget = request.get("budget")
    if not isinstance(budget, dict):
        raise ValueError(_INVALID_TARGET)
    timeout_ms = budget.get("timeout_ms")
    if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool):
        raise ValueError(_INVALID_TARGET)

    return _run_query(
        request,
        query_id=query_id,
        params=copy.deepcopy(params),
        tool=_run_named_query,
        clock=_MonotonicClock(),
        service_limits=budget,
        remaining_timeout_ms=timeout_ms,
        mode="real",
        log_sink=io.StringIO(),
    )
