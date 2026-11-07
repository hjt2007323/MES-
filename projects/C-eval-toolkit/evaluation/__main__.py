"""Command-line entry point for the deterministic C-003 mock runner.

Usage::

    python -B -m evaluation --cases <absolute JSON path> \
        --output-dir <absolute whitelisted output directory>

Only fixture-backed ``mock`` cases are supported.  Input paths must resolve
inside this project's ``tests/fixtures`` directory and output paths must be one
of the six C-003 output directories.  Errors print a fixed category to stderr
and never echo payloads or exception text.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

from .fixture_adapter import fixture_invoke
from .reporting import write_question_markdown, write_report
from .runner import FORMAT_VERSION, run_cases

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_FIXTURES_ROOT = _PROJECT_ROOT / "tests" / "fixtures"
_OUTPUTS_ROOT = (
    _PROJECT_ROOT.parent.parent
    / "execution"
    / "C"
    / "C-003"
    / "revisions"
    / "v1.2"
    / "outputs"
)
_LEGACY_OUTPUTS_ROOT = (
    _PROJECT_ROOT.parent.parent / "execution" / "C" / "C-003" / "outputs"
)
_OUTPUT_NAMES = (
    "engineering",
    "mismatch",
    "pending",
    "test-good",
    "test-mismatch",
    "test-error",
)
_ALLOWED_OUTPUTS = {name: _OUTPUTS_ROOT / name for name in _OUTPUT_NAMES}
_C004_OUTPUTS_ROOT = (
    _PROJECT_ROOT.parent.parent / "execution" / "C" / "C-004" / "outputs"
)
_C004_OUTPUT_NAMES = ("real", "test-good", "test-error")
_C004_OUTPUTS = {
    name: _C004_OUTPUTS_ROOT / name for name in _C004_OUTPUT_NAMES
}
_C005_OUTPUTS_ROOT = (
    _PROJECT_ROOT.parent.parent / "execution" / "C" / "C-005" / "outputs"
)
_C005_OUTPUT_NAMES = ("offline", "replay", "test-good", "test-error")
_C005_OUTPUTS = {
    name: _C005_OUTPUTS_ROOT / name for name in _C005_OUTPUT_NAMES
}
_C006_OUTPUTS_ROOT = (
    _PROJECT_ROOT.parent.parent / "execution" / "C" / "C-006" / "outputs"
)
_C006_OUTPUT_NAMES = ("offline", "replay", "test-good", "test-error")
_C006_OUTPUTS = {
    name: _C006_OUTPUTS_ROOT / name for name in _C006_OUTPUT_NAMES
}
_ALLOWED_OUTPUT_PATHS = (
    tuple(_ALLOWED_OUTPUTS.values())
    + tuple(_LEGACY_OUTPUTS_ROOT / name for name in _OUTPUT_NAMES)
    + tuple(_C004_OUTPUTS.values())
    + tuple(_C005_OUTPUTS.values())
    + tuple(_C006_OUTPUTS.values())
)
# Fixed lexical paths for compatibility checks; never populated from a
# resolved link/junction target.
_ALLOWED_OUTPUT_DIRS = frozenset(_ALLOWED_OUTPUT_PATHS)
_BOM = b"\xef\xbb\xbf"


class _CliError(Exception):
    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _CliError("invalid_arguments")


def _fail(category: str) -> int:
    print(f"ERROR {category}", file=sys.stderr)
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(add_help=False)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--adapter",
        choices=(
            "fixture",
            "a07-named",
            "a07-question-scripted",
            "a07-question-replay",
        ),
        default="fixture",
    )
    return parser


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _is_under(path: Path, root: Path) -> bool:
    candidate = _normalized(path)
    base = _normalized(root)
    try:
        return os.path.commonpath([candidate, base]) == base
    except ValueError:
        return False


def _resolve_path(path: Path, *, strict: bool) -> Path:
    return path.resolve(strict=strict)


def _absolute_path(raw: object) -> Path:
    try:
        path = Path(raw)
    except (TypeError, ValueError):
        raise _CliError("invalid_path")
    if not path.is_absolute():
        raise _CliError("invalid_path")
    return path


def _resolve_cases_path(raw: object) -> Path:
    path = _absolute_path(raw)
    try:
        resolved = _resolve_path(path, strict=True)
    except (OSError, RuntimeError):
        raise _CliError("invalid_path")
    if not resolved.is_file():
        raise _CliError("invalid_path")
    if not _is_under(resolved, _FIXTURES_ROOT):
        raise _CliError("invalid_path")
    return resolved


def _resolve_output_dir(raw: object) -> Path:
    path = _absolute_path(raw)
    lexical = Path(os.path.abspath(os.fspath(path)))
    requested = _normalized(lexical)
    allowed = None
    for candidate in _ALLOWED_OUTPUT_PATHS:
        if requested == _normalized(candidate):
            allowed = candidate
            break
    if allowed is None:
        raise _CliError("invalid_path")
    try:
        resolved = _resolve_path(path, strict=False)
    except (OSError, RuntimeError):
        raise _CliError("invalid_path")
    if _normalized(resolved) != _normalized(allowed):
        raise _CliError("invalid_path")
    if resolved.exists() and not resolved.is_dir():
        raise _CliError("invalid_path")
    return allowed


def _ensure_disjoint(cases_path: Path, output_dir: Path) -> None:
    try:
        nested = cases_path.is_relative_to(output_dir)
    except ValueError:
        nested = False
    if cases_path == output_dir or nested:
        raise _CliError("invalid_path")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise _CliError("invalid_json")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise _CliError("invalid_json")


def _reject_non_finite(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise _CliError("invalid_json")
    if isinstance(value, dict):
        for item in value.values():
            _reject_non_finite(item)
    elif isinstance(value, list):
        for item in value:
            _reject_non_finite(item)


def _read_input_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _load_document(path: Path) -> dict:
    try:
        data = _read_input_bytes(path)
    except OSError:
        raise _CliError("invalid_input")
    try:
        raw = bytes(data)
    except (TypeError, ValueError):
        raise _CliError("invalid_input")
    if raw.startswith(_BOM):
        raise _CliError("invalid_json")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _CliError("invalid_json")
    try:
        document = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except _CliError:
        raise
    except RecursionError:
        raise _CliError("invalid_json")
    except (json.JSONDecodeError, ValueError):
        raise _CliError("invalid_json")
    try:
        _reject_non_finite(document)
    except RecursionError:
        raise _CliError("invalid_json")
    return document


def _document_cases(document: object) -> tuple[str, list]:
    if not isinstance(document, dict):
        raise _CliError("invalid_document")
    if document.get("format_version") != FORMAT_VERSION:
        raise _CliError("invalid_document")
    mode = document.get("mode")
    if mode not in ("mock", "real"):
        raise _CliError("invalid_document")
    if document.get("suite") != "engineering":
        raise _CliError("invalid_document")
    if document.get("review_status") != "pending":
        raise _CliError("invalid_document")
    cases = document.get("cases")
    if not isinstance(cases, list):
        raise _CliError("invalid_document")
    return mode, cases


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return the process exit code.

    ``0`` means all engineering assertions passed and no unexpected
    infrastructure error occurred; ``1`` means at least one fail or
    infrastructure error; ``2`` means argument/input/document/shell or output IO
    failure.
    """
    try:
        args = _build_parser().parse_args(argv)
        cases_path = _resolve_cases_path(args.cases)
        output_dir = _resolve_output_dir(args.output_dir)
        _ensure_disjoint(cases_path, output_dir)
        document = _load_document(cases_path)
        mode, cases = _document_cases(document)
    except _CliError as exc:
        return _fail(exc.category)

    target_kind = "named"
    write_markdown = False
    if args.adapter == "fixture":
        if mode != "mock":
            return _fail("invalid_document")
        invoke = fixture_invoke
    elif args.adapter == "a07-named":
        if mode != "real":
            return _fail("invalid_document")
        try:
            from .a07_adapter import invoke_named_case
        except Exception:
            return _fail("invalid_input")
        invoke = invoke_named_case
    elif args.adapter == "a07-question-scripted":
        if mode != "mock":
            return _fail("invalid_document")
        target_kind = "question"
        write_markdown = True
        try:
            from .a07_question_adapter import invoke_question_case
        except Exception:
            return _fail("invalid_input")
        invoke = invoke_question_case
    else:
        if mode != "real":
            return _fail("invalid_document")
        target_kind = "question"
        write_markdown = True
        try:
            from .a07_question_adapter import invoke_replay_case
        except Exception:
            return _fail("invalid_input")
        invoke = invoke_replay_case

    try:
        records, summary = run_cases(
            cases, invoke, mode=mode, target_kind=target_kind
        )
    except ValueError:
        return _fail("invalid_case")

    try:
        write_report(records, summary, output_dir)
        if write_markdown:
            write_question_markdown(records, summary, output_dir)
    except (OSError, ValueError, TypeError):
        return _fail("output_io")

    if summary["fail"] == 0 and summary["infrastructure_errors"] == 0:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
