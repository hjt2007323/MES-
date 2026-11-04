"""A-006 question CLI: explicit mock/real, one r3 JSON on stdout."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mes_assistant.fakes import FakeClock
from mes_assistant.llm_client import ChatModelClient, ModelClientError
from mes_assistant.model_factory import build_model_config
from mes_assistant.question_fakes import MockTool, ScriptedModelClient
from mes_assistant.question_runtime import run_question
from mes_assistant.query_runtime import MonotonicClock
from mes_assistant.service import ContractInputError

EXIT_OK = 0
EXIT_INPUT = 2
EXIT_EXECUTION = 3
MAX_INPUT_BYTES = 1_048_576


class _InputError(Exception):
    pass


def _reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _InputError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise _InputError("non-finite JSON constant")


def _read_json(path: str):
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise _InputError("cannot read input") from exc
    if len(data) > MAX_INPUT_BYTES:
        raise _InputError("input too large")
    if data.startswith(b"\xef\xbb\xbf"):
        raise _InputError("BOM is not allowed")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _InputError("invalid UTF-8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except (_InputError, ValueError, RecursionError, TypeError) as exc:
        raise _InputError("invalid JSON") from exc
    if not isinstance(value, dict):
        raise _InputError("JSON root must be an object")
    return value


def _parse_args(argv):
    parser = argparse.ArgumentParser(description="A-006 controlled MES question CLI")
    parser.add_argument("--request", required=True)
    parser.add_argument("--mode", choices=("mock", "real"), default="mock")
    parser.add_argument("--script")
    parser.add_argument("--log", required=True)
    return parser.parse_args(argv)


def _script_replies(script_path):
    script = _read_json(script_path)
    if "replies" in script:
        replies = script["replies"]
    else:
        replies = script
    if not isinstance(replies, list) or not replies:
        raise _InputError("script replies must be a non-empty list")
    normalized = []
    for item in replies:
        if isinstance(item, str):
            normalized.append(item)
            continue
        if not isinstance(item, dict):
            raise _InputError("script reply must be a string or object")
        allowed = {"content", "usage", "advance_ms"}
        if set(item) - allowed or "content" not in item:
            raise _InputError("script reply has invalid fields")
        if not isinstance(item["content"], str):
            raise _InputError("script content must be a string")
        if "usage" in item and not (item["usage"] is None or isinstance(item["usage"], dict)):
            raise _InputError("script usage must be an object or null")
        if "advance_ms" in item and (
            isinstance(item["advance_ms"], bool)
            or not isinstance(item["advance_ms"], int)
            or item["advance_ms"] < 0
        ):
            raise _InputError("script advance_ms must be a non-negative integer")
        normalized.append(item)
    return normalized


def _limits_from_request(request):
    budget = request.get("budget")
    if not isinstance(budget, dict):
        raise _InputError("request budget is missing")
    limits = {
        "timeout_ms": budget.get("timeout_ms"),
        "max_model_calls": budget.get("max_model_calls"),
        "max_queries": budget.get("max_queries"),
        "max_corrections": budget.get("max_corrections"),
    }
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in limits.values()):
        raise _InputError("request budget values must be integers")
    return limits, limits["timeout_ms"]


def _load_real_client():
    config = build_model_config()
    return ChatModelClient(config=config)


def _load_real_tool():
    from mes_assistant.mysql_tools import run_named_query

    return run_named_query


def main(argv=None) -> int:
    try:
        args = _parse_args(argv)
        request = _read_json(args.request)
        service_limits, remaining = _limits_from_request(request)
        if args.mode == "real":
            sys.stderr.write("REAL_UNAVAILABLE\n")
            return EXIT_INPUT
        if args.mode == "mock":
            if not args.script:
                raise _InputError("mock mode requires --script")
            replies = _script_replies(args.script)
        else:
            if args.script:
                raise _InputError("real mode does not accept --script")
    except _InputError:
        sys.stderr.write("INPUT_ERROR\n")
        return EXIT_INPUT
    except (ContractInputError, KeyError, ValueError):
        sys.stderr.write("INPUT_ERROR\n")
        return EXIT_INPUT

    try:
        if args.mode == "mock":
            clock = FakeClock()
            model_client = ScriptedModelClient(replies, clock=clock)
            tool = MockTool()
        else:
            clock = MonotonicClock()
            model_client = _load_real_client()
            tool = _load_real_tool()
        log_path = Path(args.log)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_sink:
            result = run_question(
                request,
                model_client=model_client,
                tool=tool,
                clock=clock,
                service_limits=service_limits,
                remaining_timeout_ms=remaining,
                mode=args.mode,
                log_sink=log_sink,
            )
    except ModelClientError:
        sys.stderr.write("EXECUTION_ERROR\n")
        return EXIT_EXECUTION
    except (ContractInputError, KeyError, ValueError):
        sys.stderr.write("INPUT_ERROR\n")
        return EXIT_INPUT
    except Exception:
        sys.stderr.write("EXECUTION_ERROR\n")
        return EXIT_EXECUTION

    if not isinstance(result, dict):
        sys.stderr.write("EXECUTION_ERROR\n")
        return EXIT_EXECUTION
    error = result.get("error")
    if isinstance(error, dict) and error.get("code") == "LOG_WRITE_FAILED":
        sys.stderr.write("EXECUTION_ERROR\n")
        return EXIT_EXECUTION
    try:
        payload = json.dumps(result, ensure_ascii=True, allow_nan=False)
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()
    except (TypeError, ValueError):
        sys.stderr.write("EXECUTION_ERROR\n")
        return EXIT_EXECUTION
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
