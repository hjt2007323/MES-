"""Grouped engineering summary and deterministic report output.

This module only reports engineering assertion status.  It never upgrades the
synthetic ``pending`` review state and never computes a business accuracy,
safety, recovery or performance rate.
"""

from __future__ import annotations

import json
from pathlib import Path

FORMAT_VERSION = "c-cases-0.1"
SUITES = ("engineering", "query", "safety", "recovery", "clarification")
CHECK_STATUSES = ("pass", "fail", "unjudged")
MODES = ("mock", "real")


def _empty_suite() -> dict:
    return {
        "total": 0,
        "pass": 0,
        "fail": 0,
        "unjudged": 0,
        "pending": 0,
        "formal_N": 0,
        "formal_rate": None,
    }


def summarize(records: list[dict], *, mode: str = "mock") -> dict:
    """Return the fixed five-suite engineering summary for ``records``.

    ``mode`` is explicit even for an empty record list; it is never inferred
    from records.  Every record must carry the same mode.  ``formal_N`` and
    ``formal_rate`` stay at their zero/null values because every case is
    synthetic and pending.  Unknown or malformed records raise a fixed
    ``ValueError`` instead of being silently dropped.
    """
    if mode not in MODES or not isinstance(records, list):
        raise ValueError("invalid records")
    by_suite: dict[str, dict] = {name: _empty_suite() for name in SUITES}
    total = 0
    passed = 0
    failed = 0
    unjudged = 0
    infrastructure_errors = 0

    for record in records:
        if not isinstance(record, dict):
            raise ValueError("invalid records")
        suite = record.get("suite")
        check_status = record.get("check_status")
        if (
            suite not in by_suite
            or check_status not in CHECK_STATUSES
            or record.get("mode") != mode
        ):
            raise ValueError("invalid records")
        bucket = by_suite[suite]
        bucket["total"] += 1
        bucket[check_status] += 1
        total += 1
        if check_status == "pass":
            passed += 1
        elif check_status == "fail":
            failed += 1
        else:
            unjudged += 1
        if record.get("review_status") == "pending":
            bucket["pending"] += 1
        if record.get("infrastructure_error") is True:
            infrastructure_errors += 1

    summary = {
        "format_version": FORMAT_VERSION,
        "mode": mode,
        "suite": "engineering",
        "review_status": "pending",
        "total": total,
        "pass": passed,
        "fail": failed,
        "unjudged": unjudged,
        "infrastructure_errors": infrastructure_errors,
        "formal_quality": None,
        "by_suite": by_suite,
    }
    question_records = [record for record in records if "verdict" in record]
    if question_records:
        summary["business_unjudged"] = sum(
            1 for record in question_records if record.get("verdict") == "unjudged"
        )
    return summary


def write_report(records: list[dict], summary: dict, output_dir: Path) -> None:
    """Write ``cases.jsonl`` and ``summary.json`` as UTF-8 without BOM.

    The output is deterministic: records keep input order, JSON keys keep
    insertion order, no timestamp or random value is embedded, and
    ``allow_nan=False`` rejects non-finite values instead of emitting them.
    """
    if not isinstance(records, list):
        raise ValueError("invalid records")
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # Refuse pre-existing symlinked targets so a whitelisted output directory
    # cannot be used to write through a link outside the allowlisted area.
    for name in ("cases.jsonl", "summary.json"):
        if (output_path / name).is_symlink():
            raise OSError("report target must not be a symlink")

    case_lines = [
        json.dumps(
            record,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        for record in records
    ]
    cases_text = "\n".join(case_lines)
    if case_lines:
        cases_text += "\n"
    (output_path / "cases.jsonl").write_bytes(cases_text.encode("utf-8"))

    summary_text = json.dumps(
        summary,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
    )
    (output_path / "summary.json").write_bytes(
        (summary_text + "\n").encode("utf-8")
    )


def write_question_markdown(
    records: list[dict], summary: dict, output_dir: Path
) -> None:
    """Write the deterministic C-005 question report as ``report.md``.

    Engineering verdicts and business verdicts are separate columns.  Business
    cases remain ``unjudged`` regardless of a successful scripted engineering
    assertion; no business accuracy or formal denominator is emitted.
    """
    if not isinstance(records, list):
        raise ValueError("invalid records")
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    target = output_path / "report.md"
    if target.is_symlink():
        raise OSError("report target must not be a symlink")

    lines = [
        "# C-006 问答工程回归报告",
        "",
        "- format_version: `%s`" % summary.get("format_version"),
        "- mode: `%s`" % summary.get("mode"),
        "- total: %s；engineering pass/fail: %s/%s；business unjudged: %s"
        % (
            summary.get("total"),
            summary.get("pass"),
            summary.get("fail"),
            summary.get("business_unjudged", summary.get("unjudged")),
        ),
        "- formal_quality: `null`；formal_N=0；business review_status=pending",
        "",
        "| id | suite | mode | engineering | business | status | model_calls | queries | corrections | internal_ms | usage | source | replay_source | reason |",
        "|---|---|---|---|---|---|---:|---:|---:|---:|---|---|---|---|",
    ]
    for record in records:
        result = record.get("execution_result")
        if not isinstance(result, dict):
            result = {}
        counts = result.get("counts")
        if not isinstance(counts, dict):
            counts = {}
        timing = result.get("timing")
        internal_ms = timing.get("internal_ms") if isinstance(timing, dict) else None
        usage = result.get("usage")
        if usage is None:
            usage_text = "null"
        else:
            usage_text = json.dumps(
                usage, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        replay = result.get("replay_source")
        if isinstance(replay, dict):
            replay_text = "req:%s/res:%s/idx:%s/read:%sms" % (
                str(replay.get("requests_sha256", ""))[:12],
                str(replay.get("results_sha256", ""))[:12],
                str(replay.get("index_sha256", ""))[:12],
                replay.get("read_ms"),
            )
        else:
            replay_text = "null"
        lines.append(
            "| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |"
            % (
                record.get("id"),
                record.get("suite"),
                record.get("mode"),
                record.get("engineering_verdict", record.get("check_status")),
                record.get("verdict", "unjudged"),
                result.get("status"),
                counts.get("model_calls"),
                counts.get("queries"),
                counts.get("corrections"),
                internal_ms,
                usage_text,
                record.get("execution_source", "scripted"),
                replay_text,
                record.get("reason_code"),
            )
        )
    text = "\n".join(lines) + "\n"
    target.write_bytes(text.encode("utf-8"))
