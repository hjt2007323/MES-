"""Question-runtime adapter and deterministic scripted model/tool doubles.

The adapter imports only the fixed sibling A project's public question runtime
and B's public model/planner types.  Scripted doubles are local test doubles
for the C-005 offline cases; they do not call a model, database, or network and
do not implement A's orchestration.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import os
import sys
import time
from pathlib import Path

_A_PROJECT = Path(__file__).resolve().parents[2] / "A-mes-assistant"
if str(_A_PROJECT) not in sys.path:
    sys.path.insert(0, str(_A_PROJECT))

_LIVE_ROOT = (
    Path(__file__).resolve().parents[2].parent
    / "execution"
    / "A"
    / "A-007"
    / "live"
)
_REPLAY_IDS = ("m2q-01", "m2q-02", "m2q-03", "m2q-11", "m2q-15", "m2q-16")
_MAX_REPLAY_BYTES = 4 * 1024 * 1024
_INVALID_REPLAY = "invalid replay evidence"

_INVALID_CASE = "invalid question case"


def _load_runtime():
    from mes_assistant.fakes import FakeClock
    from mes_assistant.llm_client import ModelClientError, ModelReply
    from mes_assistant.question_runtime import run_question

    return ModelClientError, ModelReply, FakeClock, run_question


def _default_reply_factory(content: str, usage: object):
    _, model_reply, _, _ = _load_runtime()
    return model_reply(content=content, usage=copy.deepcopy(usage))


def _default_error_factory(code: str):
    model_error, _, _, _ = _load_runtime()
    return model_error(code, "scripted model error")


class ScriptedModelClient:
    """Deterministic model client with a public ``attempts`` counter.

    Each ``complete`` call consumes exactly one script action and increments
    ``attempts``; a script error is raised after the increment, because the
    scripted dispatch was actually attempted.
    """

    def __init__(
        self,
        actions: list,
        *,
        clock=None,
        reply_factory=None,
        error_factory=None,
    ) -> None:
        self._actions = copy.deepcopy(actions)
        self._clock = clock
        self._reply_factory = reply_factory or _default_reply_factory
        self._error_factory = error_factory or _default_error_factory
        self._attempts = 0

    @property
    def attempts(self) -> int:
        return self._attempts

    def complete(self, messages: list, *, timeout_ms: int) -> object:
        self._attempts += 1
        if not self._actions:
            raise self._error_factory("OUTPUT_INVALID")
        action = self._actions.pop(0)
        if self._clock is not None:
            advance = action.get("advance_ms", 0)
            if advance:
                self._clock.advance(advance)
        if action["kind"] == "error":
            raise self._error_factory(action["code"])
        return self._reply_factory(action["content"], action.get("usage"))


class ScriptedTool:
    """Deterministic named-query tool double for synthetic DTOs."""

    def __init__(self, actions: list) -> None:
        self._actions = copy.deepcopy(actions)
        self.calls = 0

    def __call__(
        self,
        query_id: str,
        params: dict,
        *,
        remaining_timeout_ms: int,
        max_rows: int = 200,
    ) -> dict:
        self.calls += 1
        if not self._actions:
            raise RuntimeError("scripted tool exhausted")
        action = self._actions.pop(0)
        if action["kind"] == "timeout":
            raise TimeoutError("scripted tool timeout")
        return copy.deepcopy(action["dto"])


def _target(case: dict) -> dict:
    if not isinstance(case, dict):
        raise ValueError(_INVALID_CASE)
    target = case.get("target")
    if not isinstance(target, dict) or target.get("kind") != "question":
        raise ValueError(_INVALID_CASE)
    if set(target) != {"kind", "model_script", "tool_script"}:
        raise ValueError(_INVALID_CASE)
    if not isinstance(target["model_script"], list) or not isinstance(
        target["tool_script"], list
    ):
        raise ValueError(_INVALID_CASE)
    return target


def invoke_question_case(case: dict) -> dict:
    """Invoke A's public ``run_question`` with scripted model/tool doubles."""
    target = _target(case)
    request = case.get("request")
    if not isinstance(request, dict) or not isinstance(request.get("budget"), dict):
        raise ValueError(_INVALID_CASE)
    timeout_ms = request["budget"].get("timeout_ms")
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int):
        raise ValueError(_INVALID_CASE)
    remaining_timeout_ms = request.get("remaining_timeout_ms", timeout_ms)
    if isinstance(remaining_timeout_ms, bool) or not isinstance(remaining_timeout_ms, int):
        raise ValueError(_INVALID_CASE)

    model_error, model_reply, fake_clock, run_question = _load_runtime()
    clock = fake_clock()
    client = ScriptedModelClient(
        target["model_script"],
        clock=clock,
        reply_factory=lambda content, usage: model_reply(
            content=content, usage=copy.deepcopy(usage)
        ),
        error_factory=lambda code: model_error(code, "scripted model error"),
    )
    tool = ScriptedTool(target["tool_script"])
    return run_question(
        request,
        model_client=client,
        tool=tool,
        clock=clock,
        service_limits=request["budget"],
        remaining_timeout_ms=remaining_timeout_ms,
        mode="mock",
        log_sink=io.StringIO(),
    )


def _norm_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path))).rstrip("\\/")


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(_INVALID_REPLAY)
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError(_INVALID_REPLAY)


def _has_non_finite(value) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_has_non_finite(key) or _has_non_finite(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_has_non_finite(item) for item in value)
    return False


def _read_json_bytes(path: Path) -> object:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError(_INVALID_REPLAY)
    if _norm_path(path) != _norm_path(resolved):
        raise ValueError(_INVALID_REPLAY)
    try:
        data = path.read_bytes()
    except OSError:
        raise ValueError(_INVALID_REPLAY)
    if len(data) > _MAX_REPLAY_BYTES or data.startswith(b"\xef\xbb\xbf"):
        raise ValueError(_INVALID_REPLAY)
    try:
        text = data.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise ValueError(_INVALID_REPLAY)
    try:
        non_finite = _has_non_finite(value)
    except RecursionError:
        raise ValueError(_INVALID_REPLAY)
    if non_finite:
        raise ValueError(_INVALID_REPLAY)
    return value


def _read_jsonl(path: Path) -> list:
    try:
        data = path.read_bytes()
    except OSError:
        raise ValueError(_INVALID_REPLAY)
    if len(data) > _MAX_REPLAY_BYTES or data.startswith(b"\xef\xbb\xbf"):
        raise ValueError(_INVALID_REPLAY)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(_INVALID_REPLAY)
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(
                line,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_constant,
            )
        except (json.JSONDecodeError, ValueError, RecursionError):
            raise ValueError(_INVALID_REPLAY)
        if not isinstance(value, dict):
            raise ValueError(_INVALID_REPLAY)
        try:
            non_finite = _has_non_finite(value)
        except RecursionError:
            raise ValueError(_INVALID_REPLAY)
        if non_finite:
            raise ValueError(_INVALID_REPLAY)
        rows.append(value)
    return rows


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        raise ValueError(_INVALID_REPLAY)
    return digest.hexdigest()


def _safe_file(root: Path, name: str) -> Path:
    root_path = root.resolve(strict=True)
    if _norm_path(root) != _norm_path(root_path):
        raise ValueError(_INVALID_REPLAY)
    path = root / name
    if path.is_symlink():
        raise ValueError(_INVALID_REPLAY)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError(_INVALID_REPLAY)
    if _norm_path(path) != _norm_path(resolved):
        raise ValueError(_INVALID_REPLAY)
    try:
        if not resolved.is_relative_to(root_path):
            raise ValueError(_INVALID_REPLAY)
    except ValueError:
        raise ValueError(_INVALID_REPLAY)
    return path


def _context_match(left: dict, right: dict) -> bool:
    keys = (
        "schema_version",
        "run_id",
        "request_id",
        "case_id",
        "data_version",
        "reference_time",
        "timezone",
        "config_id",
    )
    return all(left.get(key) == right.get(key) for key in keys)


def _thin_request_matches_result(request: dict, result: dict) -> bool:
    """A-007 driver requests are thin; bind their known keys to the r3 result."""
    keys = ("request_id", "case_id", "reference_time", "config_id")
    return all(request.get(key) == result.get(key) for key in keys)


def _load_replay_evidence(root: Path | None = None) -> dict:
    started = time.monotonic_ns()
    root = _LIVE_ROOT if root is None else Path(root)
    try:
        root_path = root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError(_INVALID_REPLAY)
    if _norm_path(root) != _norm_path(root_path):
        raise ValueError(_INVALID_REPLAY)
    requests_path = _safe_file(root, "requests.jsonl")
    results_path = _safe_file(root, "results.jsonl")
    index_path = _safe_file(root, "cases-index.json")
    requests = _read_jsonl(requests_path)
    results = _read_jsonl(results_path)
    index = _read_json_file(index_path)
    if not isinstance(index, dict):
        raise ValueError(_INVALID_REPLAY)
    if index.get("state") not in ("completed", "ok"):
        raise ValueError(_INVALID_REPLAY)
    cases = index.get("cases")
    if not isinstance(cases, list) or len(cases) != len(_REPLAY_IDS):
        raise ValueError(_INVALID_REPLAY)
    if len(requests) != len(_REPLAY_IDS) or len(results) != len(_REPLAY_IDS):
        raise ValueError(_INVALID_REPLAY)

    request_by_id = {}
    for request in requests:
        request_id = request.get("request_id")
        if request_id in request_by_id or request_id not in _REPLAY_IDS:
            raise ValueError(_INVALID_REPLAY)
        request_by_id[request_id] = request
    result_by_id = {}
    for result in results:
        request_id = result.get("request_id")
        if request_id in result_by_id or request_id not in _REPLAY_IDS:
            raise ValueError(_INVALID_REPLAY)
        result_by_id[request_id] = result

    records = {}
    for item in cases:
        if not isinstance(item, dict):
            raise ValueError(_INVALID_REPLAY)
        request_id = item.get("request_id")
        case_id = item.get("case_id")
        if (
            request_id not in _REPLAY_IDS
            or case_id != request_id
            or request_id in records
        ):
            raise ValueError(_INVALID_REPLAY)
        request = request_by_id.get(request_id)
        result = result_by_id.get(request_id)
        if not isinstance(request, dict) or not isinstance(result, dict):
            raise ValueError(_INVALID_REPLAY)
        if request.get("case_id") != case_id or result.get("case_id") != case_id:
            raise ValueError(_INVALID_REPLAY)
        if request.get("mode") != "real" or result.get("mode") != "real":
            raise ValueError(_INVALID_REPLAY)
        if not _thin_request_matches_result(request, result):
            raise ValueError(_INVALID_REPLAY)
        if index.get("run_id") != result.get("run_id"):
            raise ValueError(_INVALID_REPLAY)
        if (
            index.get("reference_time") is not None
            and index.get("reference_time") != result.get("reference_time")
        ):
            raise ValueError(_INVALID_REPLAY)
        if isinstance(item.get("question"), str) and item["question"] != request.get("question"):
            raise ValueError(_INVALID_REPLAY)
        records[request_id] = {"request": request, "result": result, "index": item}

    if set(records) != set(_REPLAY_IDS):
        raise ValueError(_INVALID_REPLAY)
    read_ms = (time.monotonic_ns() - started) // 1_000_000
    return {
        "records": records,
        "hashes": {
            "requests_sha256": _sha256(requests_path),
            "results_sha256": _sha256(results_path),
            "index_sha256": _sha256(index_path),
        },
        "read_ms": read_ms,
    }


def _read_json_file(path: Path) -> object:
    return _read_json_bytes(path)


def invoke_replay_case(case: dict) -> dict:
    """Read one bound result from the fixed A-007 live evidence.

    The reader is bounded, rejects BOM/duplicate keys/non-finite/deep JSON and
    symlinked paths, and requires all six m2q IDs to bind to matching request,
    result, and index context.  It never calls HTTP, a database, or the model.
    """
    if not isinstance(case, dict):
        raise ValueError(_INVALID_REPLAY)
    target = case.get("target")
    request = case.get("request")
    if (
        not isinstance(target, dict)
        or set(target) != {"kind"}
        or target.get("kind") != "question"
        or not isinstance(request, dict)
    ):
        raise ValueError(_INVALID_REPLAY)
    request_id = request.get("request_id")
    if request_id not in _REPLAY_IDS or request_id not in set(_REPLAY_IDS):
        raise ValueError(_INVALID_REPLAY)
    evidence = _load_replay_evidence()
    bound = evidence["records"].get(request_id)
    if bound is None:
        raise ValueError(_INVALID_REPLAY)
    live_request = bound["request"]
    live_result = bound["result"]
    if (
        request.get("case_id") != request_id
        or not _context_match(request, live_result)
        or request.get("question") != live_request.get("question")
        or (
            isinstance(bound["index"].get("question"), str)
            and request.get("question") != bound["index"].get("question")
        )
    ):
        raise ValueError(_INVALID_REPLAY)
    result = copy.deepcopy(live_result)
    result["replay_source"] = {
        "source": "execution/A/A-007/live",
        **evidence["hashes"],
        "read_ms": evidence["read_ms"],
    }
    return result
