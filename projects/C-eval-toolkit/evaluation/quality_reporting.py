"""Deterministic C-007 quality reports for offline examples and A-008 replay."""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path

from evaluation import quality_runner as qr

FORMAT_VERSION = "c-quality-report-0.1"


def _write_bytes(path: Path, text: str) -> None:
    if path.is_symlink():
        raise OSError("report target must not be a symlink")
    path.write_bytes(text.encode("utf-8"))


def write_json(path: Path, value) -> None:
    _write_bytes(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
    )


def write_jsonl(path: Path, records: list[dict]) -> None:
    lines = [
        json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        for record in records
    ]
    text = "\n".join(lines)
    if lines:
        text += "\n"
    _write_bytes(path, text)


def _median(values: list[int]):
    return statistics.median(values) if values else None


def _max(values: list[int]):
    return max(values) if values else None


def _empty_suite():
    return {"planned": 0, "pass": 0, "fail": 0, "not_run": 0, "rate": None}


def _suite_counts(records: list[dict], cases_by_id: dict) -> dict:
    buckets = {suite: _empty_suite() for suite in qr.SUITES}
    for record in records:
        suite = record["suite"]
        bucket = buckets[suite]
        bucket["planned"] += 1
        verdict = record.get("quality_verdict")
        if verdict == "pass":
            bucket["pass"] += 1
        elif verdict == "fail":
            bucket["fail"] += 1
        else:
            bucket["not_run"] += 1
    for bucket in buckets.values():
        if bucket["not_run"] == 0 and bucket["planned"]:
            bucket["rate"] = bucket["pass"] / bucket["planned"]
    return buckets


def _usage_and_timing(records: list[dict]) -> dict:
    input_total = 0
    output_total = 0
    known_input = 0
    known_output = 0
    unknown_usage = 0
    durations = []
    for record in records:
        if record.get("quality_verdict") == "not_run":
            continue
        usage = record.get("usage")
        if isinstance(usage, dict) and isinstance(usage.get("input_tokens"), int) and isinstance(usage.get("output_tokens"), int):
            input_total += usage["input_tokens"]
            output_total += usage["output_tokens"]
            known_input += 1
            known_output += 1
        else:
            unknown_usage += 1
        timing = record.get("timing")
        if isinstance(timing, dict) and isinstance(timing.get("internal_ms"), int):
            durations.append(timing["internal_ms"])
    return {
        "input_tokens_known": input_total if known_input == known_output and input_total + output_total > 0 else None,
        "output_tokens_known": output_total if known_input == known_output and input_total + output_total > 0 else None,
        "known_input_cases": known_input,
        "known_output_cases": known_output,
        "unknown_usage_cases": unknown_usage,
        "executed_cases": len([r for r in records if r.get("quality_verdict") != "not_run"]),
        "median_internal_ms": _median(durations),
        "max_internal_ms": _max(durations),
    }


def _variant_records(dataset: dict, variant: str, experiment: dict) -> list[dict]:
    cases = {case["id"]: case for case in dataset["cases"]}
    rows = experiment.get("records", {}).get(variant, {})
    records = []
    for case_id in qr.PILOT_IDS:
        case = cases[case_id]
        row = rows.get(case_id)
        if row is None:
            records.append(
                {
                    "case_id": case_id,
                    "suite": case["suite"],
                    "context_mode": variant,
                    "source": "replay",
                    "status": "not_run",
                    "engineering_verdict": "not_run",
                    "quality_verdict": "not_run",
                    "business_verdict": "not_run",
                    "reason_code": "not_run",
                    "counts": None,
                    "usage": None,
                    "timing": None,
                }
            )
            continue
        record = qr.judge_case(
            case,
            row["execution_result"],
            request=row["request"],
            context_mode=variant,
            source="replay",
        )
        record["source"] = "replay"
        records.append(record)
    return records


def build_pilot_report(dataset: dict, experiment: dict) -> tuple[dict, dict[str, list[dict]]]:
    cases = {case["id"]: case for case in dataset["cases"]}
    records_by_variant = {
        variant: _variant_records(dataset, variant, experiment)
        for variant in qr.VARIANTS
    }
    strategies = {}
    for variant in qr.VARIANTS:
        records = records_by_variant[variant]
        executed = [r for r in records if r.get("quality_verdict") != "not_run"]
        passed = [r for r in executed if r.get("quality_verdict") == "pass"]
        failed = [r for r in executed if r.get("quality_verdict") == "fail"]
        engineering_pass = [r for r in executed if r.get("engineering_verdict") == "pass"]
        strategies[variant] = {
            "planned": len(records),
            "executed": len(executed),
            "pass": len(passed),
            "fail": len(failed),
            "not_run": len(records) - len(executed),
            "coverage": len(executed) / len(records) if records else None,
            "quality_rate": len(passed) / len(records) if len(executed) == len(records) and records else None,
            "engineering_rate": len(engineering_pass) / len(records) if len(executed) == len(records) and records else None,
            "by_suite": _suite_counts(records, cases),
            "usage_and_timing": _usage_and_timing(executed),
        }
    paired = []
    for case_id in qr.PILOT_IDS:
        full = next(r for r in records_by_variant["full"] if r["case_id"] == case_id)
        focused = next(r for r in records_by_variant["focused"] if r["case_id"] == case_id)
        full_pair = {
            "status": full.get("status"),
            "quality_verdict": full.get("quality_verdict"),
            "engineering_verdict": full.get("engineering_verdict"),
        }
        focused_pair = {
            "status": focused.get("status"),
            "quality_verdict": focused.get("quality_verdict"),
            "engineering_verdict": focused.get("engineering_verdict"),
        }
        paired.append(
            {
                "case_id": case_id,
                "suite": cases[case_id]["suite"],
                "full": full_pair,
                "focused": focused_pair,
                "changed": full_pair != focused_pair,
            }
        )
    changes = sum(1 for item in paired if item["changed"])
    badcases = []
    for variant in qr.VARIANTS:
        for record in records_by_variant[variant]:
            if record.get("quality_verdict") == "fail":
                badcases.append(
                    {
                        "case_id": record["case_id"],
                        "suite": record["suite"],
                        "context_mode": variant,
                        "status": record.get("status"),
                        "quality_verdict": "fail",
                        "reason_code": record.get("reason_code"),
                        "engineering_verdict": record.get("engineering_verdict"),
                        "refusal_mode": record.get("refusal_mode"),
                    }
                )
    experiment_state = experiment.get("state", "missing")
    manifest = experiment.get("manifest", {})
    hashes = dict(experiment.get("hashes", {}))
    for variant in qr.VARIANTS:
        if variant in experiment.get("result_hashes", {}):
            hashes[variant + "_results_sha256"] = experiment["result_hashes"][variant]
    summary = {
        "format_version": FORMAT_VERSION,
        "contract_version": qr.CONTRACT_VERSION,
        "dataset_id": qr.DATASET_ID,
        "dataset_sha256": qr.DATASET_SHA256,
        "semantic_policy_version": qr.SEMANTIC_POLICY_VERSION,
        "scope": "synthetic_demo_only",
        "source": "replay",
        "upstream_source": "A-008 experiment",
        "upstream_live": experiment_state != "missing",
        "live": False,
        "experiment_state": experiment_state,
        "execution_state": experiment_state,
        "blocked": experiment_state in ("blocked", "missing"),
        "incomplete": experiment_state == "incomplete",
        "stop_reason": manifest.get("stop_reason") if isinstance(manifest, dict) else None,
        "errors": experiment.get("errors", []),
        "experiment_hashes": hashes,
        "run_id": "m3-pilot-001",
        "config_id": "m3-siliconflow-qwen30-v1",
        "provider": "siliconflow",
        "model": "Qwen/Qwen3-30B-A3B-Instruct-2507",
        "total_cases": 30,
        "pilot_cases": len(qr.PILOT_IDS),
        "pilot_planned_variants": len(qr.PILOT_IDS) * len(qr.VARIANTS),
        "not_run_cases": 30 - len(qr.PILOT_IDS),
        "not_run_variants": (30 - len(qr.PILOT_IDS)) * len(qr.VARIANTS),
        "total_rate": None,
        "formal_quality": None,
        "formal_N": 0,
        "strategies": strategies,
        "paired": {
            "case_count": len(paired),
            "changed_count": changes,
            "items": paired,
        },
        "badcases": badcases,
    }
    return summary, records_by_variant


def build_missing_report(dataset: dict, experiment: dict) -> tuple[dict, dict[str, list[dict]]]:
    summary, records = build_pilot_report(dataset, experiment)
    summary["state"] = "missing"
    summary["errors"] = experiment.get("errors", ["experiment_missing"])
    summary["total_rate"] = None
    for variant in qr.VARIANTS:
        summary["strategies"][variant]["quality_rate"] = None
        summary["strategies"][variant]["engineering_rate"] = None
    return summary, records


def build_offline_report(dataset: dict, validation: dict, records: list[dict]) -> dict:
    passed = sum(1 for record in records if record.get("quality_verdict") == "pass")
    failed = len(records) - passed
    by_suite = _suite_counts(records, {case["id"]: case for case in dataset["cases"]})
    return {
        "format_version": FORMAT_VERSION,
        "contract_version": qr.CONTRACT_VERSION,
        "dataset_id": qr.DATASET_ID,
        "dataset_sha256": qr.DATASET_SHA256,
        "semantic_policy_version": qr.SEMANTIC_POLICY_VERSION,
        "scope": "synthetic_demo_only",
        "source": "offline_example",
        "live": False,
        "total_cases": 30,
        "local_examples": 30,
        "local_pass": passed,
        "local_fail": failed,
        "pilot_planned_variants": 24,
        "pilot_executed": 0,
        "pilot_not_run": 24,
        "not_run_cases": 30,
        "total_rate": None,
        "formal_quality": None,
        "formal_N": 0,
        "dataset_validation": validation,
        "local_by_suite": by_suite,
    }


def _write_report_header(lines: list[str], summary: dict) -> None:
    lines.extend(
        [
            "# C-007 M3 质量评测报告",
            "",
            "- contract_version: `%s`" % summary["contract_version"],
            "- dataset: `%s` / `%s`" % (summary["dataset_id"], summary["dataset_sha256"]),
            "- semantic_policy_version: `%s`" % summary["semantic_policy_version"],
            "- scope: `synthetic_demo_only`；source: `%s`；live: `%s`"
            % (summary["source"], summary["live"]),
            "- total_rate: `null`；formal_quality: `null`；formal_N=0",
            "",
        ]
    )


def _strategy_table(lines: list[str], summary: dict) -> None:
    lines.extend(
        [
            "| strategy | planned | executed | pass | fail | not_run | coverage | quality_rate | engineering_rate | median_ms | max_ms |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for variant in qr.VARIANTS:
        value = summary["strategies"][variant]
        timing = value["usage_and_timing"]
        lines.append(
            "| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |"
            % (
                variant,
                value["planned"],
                value["executed"],
                value["pass"],
                value["fail"],
                value["not_run"],
                value["coverage"],
                value["quality_rate"],
                value["engineering_rate"],
                timing["median_internal_ms"],
                timing["max_internal_ms"],
            )
        )


def write_offline_report(output_dir: Path, dataset: dict, validation: dict, records: list[dict]) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = build_offline_report(dataset, validation, records)
    write_json(output_dir / "dataset-validation.json", validation)
    write_jsonl(output_dir / "local-examples.jsonl", records)
    write_json(output_dir / "summary.json", summary)
    lines = []
    _write_report_header(lines, summary)
    lines.append("- local examples: %s pass / %s fail" % (summary["local_pass"], summary["local_fail"]))
    lines.append("- pilot: 不在本 offline 报告中计分；A-008 replay 状态见 `outputs/pilot/summary.json`")
    lines.append("")
    lines.extend(
        [
            "| id | suite | kind | status | engineering | quality | reason |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for record in records:
        lines.append(
            "| %s | %s | %s | %s | %s | %s | %s |"
            % (
                record["case_id"],
                record["suite"],
                record.get("actual_kind"),
                record.get("status"),
                record.get("engineering_verdict"),
                record.get("quality_verdict"),
                record.get("reason_code"),
            )
        )
    lines.extend(
        [
            "",
            "local examples 只验证判定器与冻结参考表的形状；不写入 30 题实跑结果。",
            "A-008 experiment 不存在时，30 题保持未跑、总率 null。",
        ]
    )
    _write_bytes(output_dir / "report.md", "\n".join(lines) + "\n")
    return summary


def write_pilot_report(
    output_dir: Path,
    dataset: dict,
    experiment: dict,
) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if experiment.get("state") == "missing":
        summary, records = build_missing_report(dataset, experiment)
    else:
        summary, records = build_pilot_report(dataset, experiment)
        summary["state"] = experiment.get("state", "unknown")
    flat = []
    for variant in qr.VARIANTS:
        flat.extend(records[variant])
    write_jsonl(output_dir / "cases.jsonl", flat)
    write_json(output_dir / "summary.json", summary)
    write_json(
        output_dir / "binding.json",
        {
            "experiment_state": summary.get("experiment_state", "missing"),
            "experiment_hashes": summary.get("experiment_hashes", {}),
            "result_hashes": experiment.get("result_hashes", {}),
            "dataset_sha256": qr.DATASET_SHA256,
            "budget_sha256": qr.BUDGET_SHA256,
        },
    )
    lines = []
    _write_report_header(lines, summary)
    lines.append("- experiment_state: `%s`" % summary.get("experiment_state", "missing"))
    lines.append(
        "- upstream_source: `%s`；upstream_live: `%s`；C replay only（无模型/DB 调用）"
        % (summary.get("upstream_source"), summary.get("upstream_live"))
    )
    lines.append(
        "- blocked: `%s`；incomplete: `%s`；stop_reason: `%s`"
        % (summary.get("blocked"), summary.get("incomplete"), summary.get("stop_reason"))
    )
    hashes = summary.get("experiment_hashes", {})
    lines.append(
        "- source hashes: manifest=`%s` ledger=`%s` full=`%s` focused=`%s`"
        % (
            hashes.get("manifest_sha256"),
            hashes.get("ledger_sha256"),
            hashes.get("full_results_sha256"),
            hashes.get("focused_results_sha256"),
        )
    )
    lines.append("- pilot cases: 12；planned variants: 24；其余 18 题未跑")
    lines.append("")
    _strategy_table(lines, summary)
    lines.extend(
        [
            "",
            "## Suite scores",
            "",
            "| strategy | suite | planned | pass | fail | not_run | rate |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for variant in qr.VARIANTS:
        for suite in qr.SUITES:
            bucket = summary["strategies"][variant]["by_suite"][suite]
            lines.append(
                "| %s | %s | %s | %s | %s | %s | %s |"
                % (
                    variant,
                    suite,
                    bucket["planned"],
                    bucket["pass"],
                    bucket["fail"],
                    bucket["not_run"],
                    bucket["rate"],
                )
            )
    lines.append("")
    lines.extend(
        [
            "## Paired full/focused",
            "",
            "- paired cases: %s；changed: %s"
            % (summary["paired"]["case_count"], summary["paired"]["changed_count"]),
            "",
            "| case | suite | full_quality | focused_quality | full_status | focused_status | changed |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for item in summary["paired"]["items"]:
        lines.append(
            "| %s | %s | %s | %s | %s | %s | %s |"
            % (
                item["case_id"],
                item["suite"],
                item["full"]["quality_verdict"],
                item["focused"]["quality_verdict"],
                item["full"]["status"],
                item["focused"]["status"],
                item["changed"],
            )
        )
    lines.extend(["", "## Badcases", ""])
    if summary["badcases"]:
        lines.extend(
            [
                "| case | strategy | status | reason | refusal_mode |",
                "|---|---|---|---|---|",
            ]
        )
        for item in summary["badcases"]:
            lines.append(
                "| %s | %s | %s | %s | %s |"
                % (
                    item["case_id"],
                    item["context_mode"],
                    item["status"],
                    item["reason_code"],
                    item.get("refusal_mode"),
                )
            )
    else:
        lines.append("- 无质量 badcase。")
    lines.extend(["", "## Usage/timing", ""])
    for variant in qr.VARIANTS:
        timing = summary["strategies"][variant]["usage_and_timing"]
        lines.append(
            "- %s：input_tokens_known=%s，output_tokens_known=%s，unknown_usage_cases=%s，median_internal_ms=%s，max_internal_ms=%s"
            % (
                variant,
                timing["input_tokens_known"],
                timing["output_tokens_known"],
                timing["unknown_usage_cases"],
                timing["median_internal_ms"],
                timing["max_internal_ms"],
            )
        )
    lines.extend(
        [
            "",
            "工程 verdict 与 demo reference quality 分列；business summary 仍是 synthetic_demo_only，不等同生产准确率。",
            "回放只读 experiment，不调用模型或数据库。",
        ]
    )
    _write_bytes(output_dir / "report.md", "\n".join(lines) + "\n")
    return summary
