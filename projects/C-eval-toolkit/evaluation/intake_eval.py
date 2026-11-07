"""M5 downtime-intake offline judge and rule/scripted runners.

This module is independent of A's parser and B's SQLite store.  It validates
the frozen 12-case fixture, judges draft-like objects against the expected
state/record/issue and the original-text evidence spans, and produces separate
rule and scripted-oracle reports.  The scripted oracle is a synthetic contract
fixture, not a model measurement.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from pathlib import Path

from evaluation.intake_baseline import baseline_drafts

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROOT = PROJECT_ROOT.parent.parent
FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "cases-a07-intake-v1.json"
SOURCE_PATH = ROOT / "coordination" / "M5-录入案例规格.json"
DATASET_SHA256 = "c9f4001d87a9e4b5e0a6f44aa308e4f0e84ca17041a87972bd1f646480ca84ce"
CONTRACT_VERSION = "m5-downtime-intake-v1"
REFERENCE_TIME = "2026-11-04T12:00:00+08:00"
TIMEZONE = "Asia/Shanghai"
FIELDS = ("machine_no", "start_time", "end_time", "reason", "order_id")
REQUIRED_FIELDS = ("machine_no", "start_time", "end_time", "reason")
STATES = ("ready", "needs_clarification", "refused")
EXPECTED_ISSUES = {
    "i07": "MISSING_END_TIME",
    "i08": "MISSING_MACHINE_NO",
    "i09": "MULTIPLE_EVENTS",
    "i10": "INVALID_TIME_ORDER",
    "i11": "OUT_OF_SCOPE",
    "i12": "OUT_OF_SCOPE",
}
FORGED_FIELDS = {
    "submitted", "committed", "record_id", "confirmation_id",
    "write_receipt", "auto_confirm", "confirmed",
}
SCRIPTED_QUOTES = {
    "i01": {
        "machine_no": "machine_no=M01",
        "start_time": "start_time=2026-11-04T08:00:00+08:00",
        "end_time": "end_time=2026-11-04T08:10:00+08:00",
        "reason": "reason=故障",
        "order_id": "order_id=WO01",
    },
    "i02": {
        "machine_no": "machine_no=M03",
        "start_time": "start_time=2026-11-04T10:00:00+08:00",
        "end_time": "end_time=2026-11-04T10:18:00+08:00",
        "reason": "reason=保养",
    },
    "i03": {
        "machine_no": "2号机",
        "start_time": "8:12",
        "end_time": "8:25",
        "reason": "卡料",
    },
    "i04": {
        "machine_no": "M01",
        "start_time": "14:20",
        "end_time": "14:35",
        "reason": "换型",
        "order_id": "WO04",
    },
    "i05": {
        "machine_no": "二号机",
        "start_time": "八点十二",
        "end_time": "八点二十五",
        "reason": "卡料",
        "order_id": "WO02",
    },
    "i06": {
        "machine_no": "M03",
        "start_time": "23:55",
        "end_time": "00:10",
        "reason": "缺料",
    },
}


def _reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("non-finite JSON")


def _has_nonfinite(value) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_has_nonfinite(key) or _has_nonfinite(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_has_nonfinite(item) for item in value)
    return False


def load_fixture(path: Path = FIXTURE_PATH) -> dict:
    data = path.read_bytes()
    if len(data) > 4 * 1024 * 1024 or data.startswith(b"\xef\xbb\xbf"):
        raise ValueError("invalid fixture bytes")
    doc = json.loads(
        data.decode("utf-8"),
        object_pairs_hook=_reject_duplicates,
        parse_constant=_reject_constant,
    )
    if _has_nonfinite(doc):
        raise ValueError("non-finite fixture")
    if not isinstance(doc, dict):
        raise ValueError("fixture root")
    return doc


def validate_cases(doc: dict) -> dict:
    checks = []
    errors = []

    def check(name, condition):
        (checks if condition else errors).append(name)

    check("contract_version", doc.get("contract_version") == CONTRACT_VERSION)
    check("scope", doc.get("scope") == "synthetic_offline_only")
    check("reference_time", doc.get("reference_time") == REFERENCE_TIME)
    check("timezone", doc.get("timezone") == TIMEZONE)
    cases = doc.get("cases")
    if not isinstance(cases, list):
        return {"ok": False, "checks": checks, "errors": errors + ["cases"]}
    check("case_count", len(cases) == 12)
    ids = [case.get("id") for case in cases if isinstance(case, dict)]
    check("ids", ids == ["i%02d" % i for i in range(1, 13)])
    states = Counter(case.get("expected", {}).get("state") for case in cases if isinstance(case, dict))
    check("states", states == {"ready": 6, "needs_clarification": 4, "refused": 2})
    for case in cases:
        if not isinstance(case, dict):
            errors.append("case_object")
            continue
        case_id = case.get("id")
        expected = case.get("expected")
        if not isinstance(case.get("text"), str) or not case["text"].strip():
            errors.append("text_" + str(case_id))
        if not isinstance(expected, dict) or expected.get("state") not in STATES:
            errors.append("state_" + str(case_id))
            continue
        if expected["state"] == "ready":
            record = expected.get("record")
            if not isinstance(record, dict) or set(record) != set(FIELDS):
                errors.append("record_" + case_id)
                continue
            if any(record[key] is None for key in REQUIRED_FIELDS):
                errors.append("required_" + case_id)
            if len([key for key in FIELDS if record.get(key) is None]) > 1:
                errors.append("nullable_" + case_id)
            for key in ("start_time", "end_time"):
                try:
                    if not isinstance(record[key], str) or not record[key].endswith("+08:00"):
                        raise ValueError("timezone")
                except (KeyError, ValueError):
                    errors.append("time_" + case_id)
        else:
            issue = expected.get("issue")
            check("issue_" + case_id, issue == EXPECTED_ISSUES.get(case_id))
    check("ready_records", not any(item.startswith("record_") or item.startswith("required_") for item in errors))
    check("expected_issues", not any(item.startswith("issue_") for item in errors))
    return {"ok": not errors, "checks": checks, "errors": errors}


def _field_record(case: dict) -> dict:
    expected = case["expected"]
    return expected.get("record") if expected.get("state") == "ready" else None


def _validate_evidence(case: dict, draft: dict, record: dict) -> list[str]:
    text = case["text"]
    evidence = draft.get("evidence")
    errors = []
    if not isinstance(evidence, dict):
        return ["evidence_missing"]
    if set(evidence) != set(FIELDS):
        errors.append("evidence_fields")
    for field in FIELDS:
        value = record.get(field) if isinstance(record, dict) else None
        item = evidence.get(field)
        if value is None:
            if item is not None:
                errors.append("evidence_for_null_" + field)
            continue
        if not isinstance(item, dict) or set(item) != {"quote", "start", "end"}:
            errors.append("evidence_shape_" + field)
            continue
        quote = item.get("quote")
        start = item.get("start")
        end = item.get("end")
        if (
            not isinstance(quote, str)
            or not quote
            or len(quote) > 240
            or not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 0
            or end <= start
            or end > len(text)
            or text[start:end] != quote
        ):
            errors.append("evidence_invalid_" + field)
            continue
        first = text.find(quote)
        if first < 0 or text.find(quote, first + 1) != -1:
            errors.append("evidence_not_unique_" + field)
            continue
        # Per-field spelling/offset uniqueness is independent of whether two
        # fields legitimately share an overlapping or identical source span.
    return errors


def judge_draft(case: dict, draft: object) -> dict:
    case_id = case.get("id")
    expected = case.get("expected", {})
    expected_state = expected.get("state")
    base = {
        "case_id": case_id,
        "expected_state": expected_state,
        "actual_state": draft.get("state") if isinstance(draft, dict) else None,
        "mode": draft.get("mode") if isinstance(draft, dict) else None,
        "fields": {},
        "expected_issue": expected.get("issue"),
        "actual_issues": draft.get("issues") if isinstance(draft, dict) else None,
    }
    if not isinstance(draft, dict):
        return {**base, "verdict": "fail", "reason_code": "invalid_draft"}
    if any(key in draft for key in FORGED_FIELDS):
        return {**base, "verdict": "fail", "reason_code": "forged_submission"}
    actual_state = draft.get("state")
    if actual_state == "abstain":
        return {**base, "verdict": "abstain", "reason_code": "abstain_unsupported_template"}
    if actual_state == "error":
        issues = draft.get("issues")
        if isinstance(issues, list) and "INVALID_MODEL_JSON" in issues:
            return {**base, "verdict": "fail", "reason_code": "invalid_model_json"}
        return {**base, "verdict": "fail", "reason_code": "state_mismatch"}
    if actual_state != expected_state:
        return {**base, "verdict": "fail", "reason_code": "state_mismatch"}
    if expected_state == "ready":
        record = draft.get("record")
        if not isinstance(record, dict) or set(record) != set(FIELDS):
            return {**base, "verdict": "fail", "reason_code": "record_invalid"}
        expected_record = _field_record(case)
        field_results = {}
        for field in FIELDS:
            expected_value = expected_record.get(field)
            actual_value = record.get(field)
            if actual_value == expected_value:
                status = "match"
            elif field not in record or actual_value is None:
                status = "missing"
            else:
                status = "mismatch"
            field_results[field] = {
                "expected": expected_value,
                "actual": actual_value,
                "status": status,
            }
        base["fields"] = field_results
        if any(item["status"] != "match" for item in field_results.values()):
            return {**base, "verdict": "fail", "reason_code": "record_mismatch"}
        if draft.get("issues"):
            return {**base, "verdict": "fail", "reason_code": "unexpected_issue"}
        evidence_errors = _validate_evidence(case, draft, record)
        if evidence_errors:
            return {
                **base,
                "verdict": "fail",
                "reason_code": "evidence_mismatch",
                "evidence_errors": evidence_errors,
            }
        return {**base, "verdict": "pass", "reason_code": "ready_match"}
    issues = draft.get("issues")
    expected_issue = expected.get("issue")
    if not isinstance(issues, list) or issues != [expected_issue]:
        return {**base, "verdict": "fail", "reason_code": "issue_mismatch"}
    return {**base, "verdict": "pass", "reason_code": "issue_match"}


def bound_rule_drafts(cases: list[dict]) -> list[dict]:
    """Bind baseline drafts to frozen case IDs without changing the baseline."""
    drafts = baseline_drafts(cases)
    for case, draft in zip(cases, drafts):
        draft["case_id"] = case["id"]
    return drafts


def judge_cases(cases: list[dict], drafts: list[dict]) -> list[dict]:
    by_id = {}
    valid_ids = {case["id"] for case in cases}
    for draft in drafts:
        if not isinstance(draft, dict) or draft.get("case_id") not in valid_ids:
            raise ValueError("draft case_id must bind to a frozen case")
        case_id = draft["case_id"]
        if case_id in by_id:
            raise ValueError("duplicate draft")
        by_id[case_id] = draft
    records = []
    for case in cases:
        draft = by_id.get(case["id"])
        if draft is None:
            records.append(
                {
                    "case_id": case["id"],
                    "expected_state": case["expected"]["state"],
                    "verdict": "fail",
                    "reason_code": "missing_draft",
                }
            )
            continue
        records.append(judge_draft(case, draft))
    return records


def scripted_drafts(cases: list[dict]) -> list[dict]:
    drafts = []
    for case in cases:
        expected = case["expected"]
        case_id = case["id"]
        if expected["state"] != "ready":
            drafts.append(
                {
                    "case_id": case_id,
                    "draft_id": "scripted-" + case_id,
                    "state": expected["state"],
                    "record": None,
                    "evidence": None,
                    "issues": [expected["issue"]],
                    "mode": "scripted_oracle",
                }
            )
            continue
        text = case["text"]
        evidence = {field: None for field in FIELDS}
        for field, quote in SCRIPTED_QUOTES[case_id].items():
            start = text.index(quote)
            evidence[field] = {"quote": quote, "start": start, "end": start + len(quote)}
        drafts.append(
            {
                "case_id": case_id,
                "draft_id": "scripted-" + case_id,
                "state": "ready",
                "record": dict(expected["record"]),
                "evidence": evidence,
                "issues": [],
                "mode": "scripted_oracle",
            }
        )
    return drafts


def _case_stats(records: list[dict], cases: list[dict]) -> dict:
    by_expected = {state: {"total": 0, "pass": 0, "fail": 0, "abstain": 0} for state in STATES}
    for record in records:
        bucket = by_expected[record["expected_state"]]
        bucket["total"] += 1
        bucket[record["verdict"]] += 1
    return {
        "total": len(records),
        "pass": sum(record["verdict"] == "pass" for record in records),
        "fail": sum(record["verdict"] == "fail" for record in records),
        "abstain": sum(record["verdict"] == "abstain" for record in records),
        "by_expected": by_expected,
    }


def _field_stats(records: list[dict], cases: list[dict]) -> dict:
    result = {}
    for field in FIELDS:
        bucket = {
            "denominator": 0,
            "evaluable": 0,
            "pass": 0,
            "fail": 0,
            "abstain": 0,
            "not_applicable": 0,
            "null_expected": 0,
        }
        for record, case in zip(records, cases):
            if record["expected_state"] != "ready":
                bucket["not_applicable"] += 1
                continue
            bucket["denominator"] += 1
            expected_value = case["expected"]["record"][field]
            if expected_value is None:
                bucket["null_expected"] += 1
            if record["verdict"] == "abstain":
                bucket["abstain"] += 1
            elif record["verdict"] == "pass":
                bucket["pass"] += 1
            else:
                status = record.get("fields", {}).get(field, {}).get("status")
                if status == "match":
                    bucket["pass"] += 1
                else:
                    bucket["fail"] += 1
        bucket["evaluable"] = bucket["pass"] + bucket["fail"]
        result[field] = bucket
    return result


def summarize(records: list[dict], cases: list[dict], *, mode: str) -> dict:
    return {
        "format_version": "c-intake-report-0.1",
        "contract_version": CONTRACT_VERSION,
        "dataset_sha256": DATASET_SHA256,
        "mode": mode,
        "scope": "synthetic_offline_only",
        "source": "rule_template" if mode == "rule" else "scripted_oracle",
        "cases": _case_stats(records, cases),
        "fields": _field_stats(records, cases),
        "coverage": {
            "total": 12,
            "evaluable": sum(record["verdict"] != "abstain" for record in records),
            "abstain": sum(record["verdict"] == "abstain" for record in records),
        },
    }


def _write_json(path: Path, value) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, values: list[dict]) -> None:
    path.write_text(
        "\n".join(
            json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            for value in values
        )
        + "\n",
        encoding="utf-8",
    )


def _write_report(path: Path, *, doc: dict, baseline: dict, scripted: dict) -> None:
    lines = [
        "# C-009 M5 停机录入离线判定报告",
        "",
        "- contract_version: `%s`" % CONTRACT_VERSION,
        "- dataset_sha256: `%s`" % DATASET_SHA256,
        "- scope: `synthetic_offline_only`；无模型/DB 调用",
        "- 12 条冻结案例；6 ready / 4 clarification / 2 refusal",
        "",
        "## 规则基线（仅完整显式字段模板）",
        "",
        "| 项目 | 值 |",
        "|---|---:|",
        "| total | 12 |",
        "| pass | %s |" % baseline["cases"]["pass"],
        "| fail | %s |" % baseline["cases"]["fail"],
        "| abstain | %s |" % baseline["cases"]["abstain"],
        "| coverage | %s/12 |" % baseline["coverage"]["evaluable"],
        "",
        "非模板输入全部弃答；pass 只来自 i01/i02 两条显式模板。",
        "",
        "## 脚本替身（合成合同样例）",
        "",
        "| 项目 | 值 |",
        "|---|---:|",
        "| total | 12 |",
        "| pass | %s |" % scripted["cases"]["pass"],
        "| fail | %s |" % scripted["cases"]["fail"],
        "| abstain | %s |" % scripted["cases"]["abstain"],
        "| coverage | %s/12 |" % scripted["coverage"]["evaluable"],
        "",
        "脚本替身使用冻结答案与原文引用，只验证判定器和合同，不是模型效果；不得据此推断模型收益。",
        "",
        "## 逐字段统计（ready 分母）",
        "",
        "| field | denominator | evaluable | pass | fail | abstain | not_applicable | null_expected |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for field in FIELDS:
        row = baseline["fields"][field]
        lines.append(
            "| %s | %s | %s | %s | %s | %s | %s | %s |"
            % (
                field,
                row["denominator"],
                row["evaluable"],
                row["pass"],
                row["fail"],
                row["abstain"],
                row["not_applicable"],
                row["null_expected"],
            )
        )
    lines += [
        "",
        "## 逐字段统计（脚本替身）",
        "",
        "| field | denominator | evaluable | pass | fail | abstain | not_applicable | null_expected |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for field in FIELDS:
        row = scripted["fields"][field]
        lines.append(
            "| %s | %s | %s | %s | %s | %s | %s | %s |"
            % (
                field,
                row["denominator"],
                row["evaluable"],
                row["pass"],
                row["fail"],
                row["abstain"],
                row["not_applicable"],
                row["null_expected"],
            )
        )
    lines += [
        "",
        "## 题级状态（规则）",
        "",
        "| case | expected | actual | verdict | reason |",
        "|---|---|---|---|---|",
    ]
    for record in baseline["records"]:
        lines.append(
            "| %s | %s | %s | %s | %s |"
            % (
                record["case_id"],
                record.get("expected_state"),
                record.get("actual_state"),
                record.get("verdict"),
                record.get("reason_code"),
            )
        )
    lines += [
        "",
        "## 题级状态（脚本替身）",
        "",
        "| case | expected | actual | verdict | reason |",
        "|---|---|---|---|---|",
    ]
    for record in scripted["records"]:
        lines.append(
            "| %s | %s | %s | %s | %s |"
            % (
                record["case_id"],
                record.get("expected_state"),
                record.get("actual_state"),
                record.get("verdict"),
                record.get("reason_code"),
            )
        )
    lines += [
        "",
        "规则与脚本分列；空值和弃答保留分母。规则基线只在模板题上可判，不能把 abstain 当业务失败或模型收益。",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C-009 M5 intake offline runner")
    parser.add_argument("--mode", choices=("rule", "scripted", "both"), default="both")
    parser.add_argument("--output-dir", default=str(ROOT / "execution" / "C" / "C-009"))
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2
    try:
        doc = load_fixture()
        if hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest() != DATASET_SHA256:
            print("ERROR invalid_dataset", file=sys.stderr)
            return 2
        validation = validate_cases(doc)
        if not validation["ok"]:
            print("ERROR invalid_dataset", file=sys.stderr)
            return 2
        cases = doc["cases"]
        output = Path(args.output_dir)
        output.mkdir(parents=True, exist_ok=True)
        baseline_records = judge_cases(cases, bound_rule_drafts(cases))
        scripted_records = judge_cases(cases, scripted_drafts(cases))
        baseline = summarize(baseline_records, cases, mode="rule")
        scripted = summarize(scripted_records, cases, mode="scripted")
        baseline["records"] = baseline_records
        scripted["records"] = scripted_records
        if args.mode in ("rule", "both"):
            _write_json(output / "baseline.json", baseline)
            _write_jsonl(output / "baseline-cases.jsonl", baseline_records)
        if args.mode in ("scripted", "both"):
            _write_json(output / "scripted.json", scripted)
            _write_jsonl(output / "scripted-cases.jsonl", scripted_records)
        _write_json(output / "summary.json", {"validation": validation, "baseline": baseline, "scripted": scripted})
        _write_report(output / "report.md", doc=doc, baseline=baseline, scripted=scripted)
        print(
            "rule pass=%d abstain=%d; scripted pass=%d"
            % (baseline["cases"]["pass"], baseline["cases"]["abstain"], scripted["cases"]["pass"])
        )
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print("ERROR invalid_input", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
