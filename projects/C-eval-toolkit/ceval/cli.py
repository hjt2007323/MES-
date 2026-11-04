"""Offline synthetic C demo CLI.

Run from the candidate C project root:

    python -B -m ceval.cli --output-dir reports/offline

It only reads candidate-local synthetic cases/results; no model, database,
network, API key, execution directory or fixed host path is used.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from .quality import judge_all

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "data" / "cases.json"
DEFAULT_RESULTS = ROOT / "data" / "results.json"
DEFAULT_OUTPUT = Path("reports") / "offline"


class _BindingError(ValueError):
    pass


def _reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("non-finite JSON")


def _load(path: Path):
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("input too large")
    if path.read_bytes().startswith(b"\xef\xbb\xbf"):
        raise ValueError("BOM not allowed")
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicates,
        parse_constant=_reject_constant,
    )
    if _has_nonfinite(value):
        raise ValueError("non-finite JSON")
    return value


def _nonempty_id(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_binding(cases_doc, results_doc) -> tuple[list, list]:
    if not isinstance(cases_doc, dict) or not isinstance(cases_doc.get("cases"), list):
        raise _BindingError("input_binding")
    if not isinstance(results_doc, dict) or not isinstance(results_doc.get("results"), list):
        raise _BindingError("input_binding")
    cases = cases_doc["cases"]
    results = results_doc["results"]
    if not cases or not results:
        raise _BindingError("input_binding")
    case_ids = []
    for case in cases:
        if not isinstance(case, dict) or not _nonempty_id(case.get("id")):
            raise _BindingError("input_binding")
        if not isinstance(case.get("expected"), dict) or not isinstance(case.get("suite"), str):
            raise _BindingError("input_binding")
        case_ids.append(case["id"])
    result_ids = []
    for result in results:
        if not isinstance(result, dict) or not _nonempty_id(result.get("case_id")):
            raise _BindingError("input_binding")
        result_ids.append(result["case_id"])
    if len(set(case_ids)) != len(case_ids) or len(set(result_ids)) != len(result_ids):
        raise _BindingError("input_binding")
    if set(case_ids) != set(result_ids):
        raise _BindingError("input_binding")
    return cases, results



def _has_nonfinite(value) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_has_nonfinite(key) or _has_nonfinite(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_has_nonfinite(item) for item in value)
    return False


def _write_json(path: Path, value) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(
            json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            for record in records
        )
        + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C offline synthetic quality demo")
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--results", default=str(DEFAULT_RESULTS))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2
    try:
        cases_doc = _load(Path(args.cases))
        results_doc = _load(Path(args.results))
        cases, results = _validate_binding(cases_doc, results_doc)
        records = judge_all(cases, results)
    except _BindingError:
        print("ERROR input_binding", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        print("ERROR invalid_input", file=sys.stderr)
        return 2

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    expected = sum(1 for record in records if record.get("expected_matched") is True)
    passed = sum(1 for record in records if record.get("verdict") == "pass")
    summary = {
        "format_version": "c-public-demo-0.1",
        "source": "candidate_synthetic",
        "scope": "synthetic_demo_only",
        "cases": len(records),
        "expected_matched": expected,
        "verdict_pass": passed,
        "verdict_fail": len(records) - passed,
        "all_expected_matched": expected == len(records),
        "records": records,
    }
    _write_jsonl(output / "cases.jsonl", records)
    _write_json(output / "summary.json", summary)
    lines = [
        "# C offline synthetic demo",
        "",
        "- source: candidate-local synthetic cases/results",
        "- scope: synthetic_demo_only",
        "- cases: %s；expected matched: %s；verdict pass/fail: %s/%s"
        % (len(records), expected, passed, len(records) - passed),
        "",
        "| case | suite | verdict | reason | expected | matched |",
        "|---|---|---|---|---|---|",
    ]
    for record in records:
        lines.append(
            "| %s | %s | %s | %s | %s | %s |"
            % (
                record["case_id"],
                record["suite"],
                record["verdict"],
                record["reason_code"],
                record.get("expected_verdict"),
                record.get("expected_matched"),
            )
        )
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        "offline cases=%d expected_matched=%d verdict_pass=%d"
        % (len(records), expected, passed)
    )
    return 0 if expected == len(records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
