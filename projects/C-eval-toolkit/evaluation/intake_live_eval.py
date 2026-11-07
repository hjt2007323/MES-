"""C-010 live intake evaluation loader, validator, and reporter.

This module is deliberately separate from the accepted offline evaluator. It
reuses the frozen fixture and ``intake_eval.judge_draft`` but adds the live
batch contract: input export, manifest/ledger/result binding, mode provenance
checks, and record/field/evidence/safety metrics.

No network or model calls are made here. An offline replay fixture is enough to
exercise every branch; real batches are supplied as explicit result files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from evaluation import intake_eval as ev

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROOT = PROJECT_ROOT.parent.parent
FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "cases-a07-intake-v1.json"
SPEC_PATH = ROOT / "coordination" / "M5-录入案例规格.json"
DEFAULT_INPUTS = ROOT / "execution" / "C" / "C-010" / "live-inputs.json"
DEFAULT_OUTPUT = ROOT / "execution" / "C" / "C-010" / "live-replay"

LIVE_CONTRACT = "m5-intake-live-v1"
LIVE_DATASET_SHA = "c9f4001d87a9e4b5e0a6f44aa308e4f0e84ca17041a87972bd1f646480ca84ce"
RUN_ID = "m5-pilot-001"
CONFIG_ID = "m5-siliconflow-qwen30-v1"
PROVIDER = "siliconflow"
MODEL = "Qwen/Qwen3-30B-A3B-Instruct-2507"
REFERENCE_TIME = "2026-11-04T12:00:00+08:00"
TIMEZONE = "Asia/Shanghai"
CASE_IDS = tuple("i%02d" % index for index in range(1, 13))
MODES = ("real", "offline")
SOURCES = ("http_model", "test_double")
MAX_COMPLETION_TOKENS_PER_CALL = 512
MAX_COMPLETION_TOKENS_TOTAL = 6144
FORGED_FIELDS = ev.FORGED_FIELDS


class LiveEvalError(ValueError):
    """Evidence does not satisfy the live batch contract."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def _reject_duplicates(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise LiveEvalError("DUPLICATE_KEY", "JSON object has duplicate keys")
        seen[key] = value
    return seen


def _reject_constant(_value):
    raise LiveEvalError("NONFINITE", "JSON contains NaN/Infinity")


def load_json(path: Path) -> object:
    try:
        return json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except LiveEvalError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LiveEvalError("JSON_INVALID", "JSON file is invalid: %s" % Path(path).name) from exc


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise LiveEvalError(code, message)


def export_live_inputs(
    *,
    fixture_path: Path = FIXTURE_PATH,
    spec_path: Path | None = SPEC_PATH,
    output_path: Path = DEFAULT_INPUTS,
) -> dict:
    """Build the answer-free input file from the frozen fixture.

    The public closure intentionally depends only on the fixture. The internal
    coordination spec is an optional extra source check when it is present.
    """
    fixture_bytes = Path(fixture_path).read_bytes()
    _require(sha256_bytes(fixture_bytes) == LIVE_DATASET_SHA, "DATASET_SHA_MISMATCH", "fixture SHA mismatch")
    if spec_path is not None and Path(spec_path).is_file():
        _require(sha256_bytes(Path(spec_path).read_bytes()) == LIVE_DATASET_SHA, "SPEC_SHA_MISMATCH", "spec SHA mismatch")
    fixture = ev.load_fixture(Path(fixture_path))
    cases = [{"case_id": case["id"], "text": case["text"]} for case in fixture["cases"]]
    payload = {
        "contract_version": LIVE_CONTRACT,
        "dataset_sha256": LIVE_DATASET_SHA,
        "reference_time": REFERENCE_TIME,
        "timezone": TIMEZONE,
        "cases": cases,
    }
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "payload": payload,
        "input_sha256": sha256_file(output_path),
        "dataset_sha256": LIVE_DATASET_SHA,
        "case_count": len(cases),
    }


def load_live_inputs(path: Path = DEFAULT_INPUTS, *, fixture_path: Path = FIXTURE_PATH) -> tuple[dict, dict]:
    raw = load_json(path)
    _require(isinstance(raw, dict), "INPUT_SHAPE", "inputs must be an object")
    _require(set(raw) == {"contract_version", "dataset_sha256", "reference_time", "timezone", "cases"}, "INPUT_SHAPE", "inputs keys")
    _require(raw["contract_version"] == LIVE_CONTRACT, "CONTRACT_VERSION", "input contract mismatch")
    _require(raw["dataset_sha256"] == LIVE_DATASET_SHA, "DATASET_SHA_MISMATCH", "input dataset SHA mismatch")
    _require(raw["reference_time"] == REFERENCE_TIME, "REFERENCE_TIME", "reference_time mismatch")
    _require(raw["timezone"] == TIMEZONE, "TIMEZONE", "timezone mismatch")
    fixture_bytes = Path(fixture_path).read_bytes()
    _require(sha256_bytes(fixture_bytes) == LIVE_DATASET_SHA, "FIXTURE_SHA_MISMATCH", "fixture bytes do not match the frozen SHA")
    cases = raw["cases"]
    _require(isinstance(cases, list) and len(cases) == 12, "CASE_COUNT", "inputs must contain 12 cases")
    fixture = ev.load_fixture(fixture_path)
    fixture_by_id = {case["id"]: case for case in fixture["cases"]}
    seen = []
    for item in cases:
        _require(isinstance(item, dict) and set(item) == {"case_id", "text"}, "CASE_SHAPE", "case must contain only case_id/text")
        case_id = item["case_id"]
        _require(case_id in fixture_by_id, "CASE_ID", "unknown case id")
        _require(isinstance(item["text"], str) and item["text"] == fixture_by_id[case_id]["text"], "INPUT_TEXT_MISMATCH", "input text differs from frozen fixture")
        _require(case_id not in seen, "CASE_DUPLICATE", "duplicate case id")
        seen.append(case_id)
    _require(tuple(seen) == CASE_IDS, "CASE_ORDER", "case ids must be i01..i12 in order")
    return raw, fixture


def _budget_case_ids(budget: dict) -> tuple[str, ...]:
    ids = budget.get("case_ids")
    _require(isinstance(ids, list) and tuple(ids) == CASE_IDS, "BUDGET_INVALID", "budget case ids")
    return tuple(ids)


def load_budget(path: Path, inputs: dict, input_sha256: str) -> dict:
    budget = load_json(path)
    _require(isinstance(budget, dict), "BUDGET_INVALID", "budget must be object")
    _require(budget.get("contract_version") == LIVE_CONTRACT, "BUDGET_INVALID", "budget contract")
    _require(budget.get("status") == "approved", "BUDGET_INVALID", "budget status must be approved")
    _require(budget.get("run_id") == RUN_ID, "BUDGET_INVALID", "budget run_id")
    _require(budget.get("config_id") == CONFIG_ID, "BUDGET_INVALID", "budget config_id")
    _require(budget.get("provider") == PROVIDER and budget.get("model") == MODEL, "BUDGET_INVALID", "budget provider/model")
    _require(budget.get("dataset_sha256") == LIVE_DATASET_SHA, "BUDGET_INVALID", "budget dataset SHA")
    _require(budget.get("input_sha256") == input_sha256, "BUDGET_INVALID", "budget input SHA")
    _require(budget.get("inputs_path") == "execution/C/C-010/live-inputs.json", "BUDGET_INVALID", "budget inputs path")
    _budget_case_ids(budget)
    _require(budget.get("mode") == "real", "BUDGET_INVALID", "budget mode")
    _require(budget.get("max_requests") == 12, "BUDGET_INVALID", "budget request cap")
    _require(budget.get("max_client_calls") == 12, "BUDGET_INVALID", "budget client cap")
    _require(budget.get("max_calls_per_case") == 1, "BUDGET_INVALID", "budget per-case cap")
    _require(budget.get("call_timeout_ms") == 10000, "BUDGET_INVALID", "budget timeout")
    _require(budget.get("max_completion_tokens_per_call") == 512, "BUDGET_INVALID", "budget token cap")
    _require(budget.get("max_completion_tokens_total") == 6144, "BUDGET_INVALID", "budget total token cap")
    _require(budget.get("automatic_retries") == 0, "BUDGET_INVALID", "budget retries")
    _require(budget.get("write_policy") == "draft_only_no_commit", "BUDGET_INVALID", "budget write policy")
    _require(budget.get("result_directory") == "execution/A/A-011/experiment/m5-pilot-001", "BUDGET_INVALID", "budget result dir")
    return budget


def _int_or_null(value, code: str) -> int | None:
    if value is None:
        return None
    _require(isinstance(value, int) and not isinstance(value, bool) and value >= 0, code, "non-negative integer/null required")
    return value


def _manifest_value(manifest: dict, keys: tuple[str, ...], code: str):
    for key in keys:
        if key in manifest:
            return manifest[key]
    raise LiveEvalError(code, "manifest missing %s" % "/".join(keys))


def _validate_run(run: object) -> dict:
    _require(isinstance(run, dict), "RESULT_RUN_SHAPE", "run must be object")
    mode = run.get("mode")
    _require(mode in MODES, "RESULT_MODE", "run mode must be real/offline")
    source = run.get("execution_source", run.get("source"))
    _require(source in SOURCES, "RESULT_SOURCE", "execution_source must be http_model/test_double")
    provider = run.get("provider")
    model = run.get("model")
    if source == "http_model":
        _require(isinstance(provider, str) and provider and isinstance(model, str) and model, "RESULT_PROVENANCE", "http_model provider/model required")
        _require(provider == PROVIDER and model == MODEL, "RESULT_PROVENANCE", "http_model provider/model mismatch")
    else:
        _require(provider is None or isinstance(provider, str), "RESULT_PROVENANCE", "test_double provider must string/null")
        _require(model is None or isinstance(model, str), "RESULT_PROVENANCE", "test_double model must string/null")
    calls = run.get("client_calls", run.get("calls"))
    _require(isinstance(calls, int) and not isinstance(calls, bool) and 0 <= calls <= 1, "RESULT_CLIENT_CALLS", "client_calls must be 0/1")
    _require("usage" in run, "RESULT_USAGE", "usage key is required (null when unknown)")
    usage = run.get("usage")
    if usage is not None:
        _require(isinstance(usage, dict), "RESULT_USAGE", "usage must be object/null")
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if key in usage:
                _int_or_null(usage[key], "RESULT_USAGE")
        completion = usage.get("completion_tokens")
        if isinstance(completion, int) and not isinstance(completion, bool):
            _require(completion <= MAX_COMPLETION_TOKENS_PER_CALL, "RESULT_COMPLETION_CAP", "completion_tokens exceeds 512")
    error_code = run.get("error_code")
    _require(error_code is None or isinstance(error_code, str), "RESULT_ERROR_CODE", "error_code must string/null")
    return {
        "mode": mode,
        "source": source,
        "provider": provider,
        "model": model,
        "client_calls": calls,
        "usage": usage,
        "error_code": error_code,
        "elapsed_ms": run.get("elapsed_ms"),
    }


def load_results(path: Path) -> list[dict]:
    rows = []
    seen = set()
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant)
        except LiveEvalError:
            raise
        except json.JSONDecodeError as exc:
            raise LiveEvalError("RESULT_JSON", "results line %d is invalid JSON" % line_number) from exc
        _require(isinstance(row, dict) and set(row) == {"case_id", "draft", "run"}, "RESULT_SHAPE", "result row keys")
        case_id = row["case_id"]
        _require(case_id in CASE_IDS, "RESULT_CASE_ID", "unknown result case id")
        _require(case_id not in seen, "RESULT_DUPLICATE", "duplicate result case id")
        seen.add(case_id)
        draft = row["draft"]
        _require(isinstance(draft, dict), "RESULT_DRAFT", "draft must be object")
        _require(not any(key in draft for key in FORGED_FIELDS), "RESULT_FORGED", "forged submission fields are not accepted")
        run = _validate_run(row["run"])
        rows.append({"case_id": case_id, "draft": draft, "run": run})
    return rows


def _ledger_entries(ledger: object) -> list[dict]:
    _require(isinstance(ledger, dict), "LEDGER_SHAPE", "ledger must be object")
    _require(ledger.get("contract_version") == LIVE_CONTRACT, "LEDGER_CONTRACT", "ledger contract")
    raw = ledger.get("cases")
    entries = []
    if isinstance(raw, list):
        entries = raw
    elif isinstance(raw, dict):
        for case_id, item in raw.items():
            _require(isinstance(item, dict), "LEDGER_SHAPE", "ledger case must be object")
            entry = dict(item)
            entry.setdefault("case_id", case_id)
            entries.append(entry)
    else:
        raise LiveEvalError("LEDGER_SHAPE", "ledger cases must be list/dict")
    _require(len(entries) == 12, "LEDGER_CASE_COUNT", "ledger must contain 12 cases")
    result = []
    seen = []
    for entry in entries:
        _require(isinstance(entry, dict), "LEDGER_SHAPE", "ledger entry must be object")
        case_id = entry.get("case_id")
        _require(case_id in CASE_IDS, "LEDGER_CASE_ID", "unknown ledger case id")
        _require(case_id not in seen, "LEDGER_DUPLICATE", "duplicate ledger case id")
        state = entry.get("state")
        reserved = entry.get("reserved", state in ("reserved", "observed"))
        observed = entry.get("observed", state == "observed")
        _require(isinstance(reserved, bool) and isinstance(observed, bool), "LEDGER_STATE", "reserved/observed must be bool")
        calls = entry.get("client_calls", entry.get("calls"))
        _require(calls is None or (isinstance(calls, int) and not isinstance(calls, bool) and calls >= 0), "LEDGER_CALLS", "ledger calls must int/null")
        result.append({"case_id": case_id, "reserved": reserved, "observed": observed, "client_calls": calls})
        seen.append(case_id)
    _require(tuple(seen) == CASE_IDS, "LEDGER_ORDER", "ledger must be i01..i12 in order")
    return result


def validate_batch(
    *,
    inputs_path: Path,
    manifest_path: Path,
    ledger_path: Path,
    results_path: Path,
    budget_path: Path | None = None,
    expected_source_sha256: str | dict | None = None,
    source_root: Path | None = None,
    fixture_path: Path = FIXTURE_PATH,
) -> dict:
    inputs, fixture = load_live_inputs(inputs_path, fixture_path=fixture_path)
    input_sha = sha256_file(inputs_path)
    budget = None
    if budget_path is not None:
        budget = load_budget(budget_path, inputs, input_sha)
    manifest = load_json(manifest_path)
    _require(isinstance(manifest, dict), "MANIFEST_SHAPE", "manifest must be object")
    _require(manifest.get("contract_version") == LIVE_CONTRACT, "MANIFEST_CONTRACT", "manifest contract")
    _require(manifest.get("run_id") == RUN_ID, "MANIFEST_RUN", "manifest run_id")
    _require(manifest.get("config_id") == CONFIG_ID, "MANIFEST_CONFIG", "manifest config_id")
    _require(manifest.get("provider") == PROVIDER and manifest.get("model") == MODEL, "MANIFEST_PROVENANCE", "manifest provider/model")
    _require(manifest.get("dataset_sha256", manifest.get("data_sha256")) == LIVE_DATASET_SHA, "MANIFEST_DATASET", "manifest dataset SHA")
    _require(manifest.get("input_sha256", manifest.get("inputs_sha256")) == input_sha, "MANIFEST_INPUT", "manifest input SHA")
    source_sha = manifest.get("source_sha256")
    if isinstance(source_sha, str):
        _require(len(source_sha) == 64 and all(ch in "0123456789abcdef" for ch in source_sha.lower()), "MANIFEST_SOURCE", "manifest source SHA256")
        if expected_source_sha256 is not None:
            if isinstance(expected_source_sha256, dict):
                raise LiveEvalError("MANIFEST_SOURCE", "manifest source is scalar but expected map")
            _require(source_sha.lower() == expected_source_sha256.lower(), "MANIFEST_SOURCE", "manifest source SHA mismatch")
    elif isinstance(source_sha, dict) and source_sha:
        for name, value in source_sha.items():
            _require(isinstance(name, str) and isinstance(value, str) and len(value) == 64 and all(ch in "0123456789abcdef" for ch in value.lower()), "MANIFEST_SOURCE", "manifest source map is invalid")
        if isinstance(expected_source_sha256, dict):
            _require({key: value.lower() for key, value in source_sha.items()} == {key: value.lower() for key, value in expected_source_sha256.items()}, "MANIFEST_SOURCE", "manifest source map mismatch")
        elif expected_source_sha256 is not None:
            raise LiveEvalError("MANIFEST_SOURCE", "manifest source is map but expected scalar")
    else:
        raise LiveEvalError("MANIFEST_SOURCE", "manifest source SHA256/map required")
    _require(manifest.get("results_sha256", manifest.get("result_sha256")) == sha256_file(results_path), "MANIFEST_RESULTS_SHA", "results SHA mismatch")
    _require(manifest.get("ledger_sha256") == sha256_file(ledger_path), "MANIFEST_LEDGER_SHA", "ledger SHA mismatch")
    if budget is not None:
        _require(manifest.get("budget_sha256") == sha256_file(budget_path), "MANIFEST_BUDGET_SHA", "budget SHA mismatch")
    state = manifest.get("state")
    _require(state in ("complete", "completed", "incomplete", "failed"), "MANIFEST_STATE", "manifest state")
    if state == "completed":
        state = "complete"
    planned = _manifest_value(manifest, ("planned", "planned_count", "planned_cases"), "MANIFEST_COUNT")
    observed = _manifest_value(manifest, ("observed", "observed_count", "observed_cases"), "MANIFEST_COUNT")
    _require(planned == 12 and observed == 12 if state == "complete" else planned == 12 and 0 <= observed <= 12, "MANIFEST_COUNT", "planned/observed count")
    rows = load_results(results_path)
    _require(len(rows) == observed, "MANIFEST_COUNT", "result count differs from manifest observed")
    ledger = _ledger_entries(load_json(ledger_path))
    ledger_observed = [entry["case_id"] for entry in ledger if entry["observed"]]
    _require(len(ledger_observed) == observed, "LEDGER_COUNT", "ledger observed count differs")
    result_ids = [row["case_id"] for row in rows]
    _require(result_ids == ledger_observed, "LEDGER_RESULT_ORDER", "results and ledger observed IDs differ")
    _require(set(result_ids) == set(ledger_observed), "LEDGER_RESULT_SET", "results and ledger sets differ")
    for row in rows:
        _require(next(entry for entry in ledger if entry["case_id"] == row["case_id"])["reserved"] is True, "LEDGER_STATE", "observed case was not reserved")
    ledger_by_id = {entry["case_id"]: entry for entry in ledger}
    ledger_calls_unknown = False
    for row in rows:
        ledger_calls = ledger_by_id[row["case_id"]]["client_calls"]
        run_calls = row["run"]["client_calls"]
        if ledger_calls is None:
            ledger_calls_unknown = True
        else:
            _require(ledger_calls <= 1, "LEDGER_CLIENT_CALLS", "ledger client_calls exceeds per-case cap 1")
            _require(ledger_calls == run_calls, "LEDGER_CLIENT_CALLS", "ledger and result client_calls differ")
    runs = [row["run"] for row in rows]
    modes = {run["mode"] for run in runs}
    sources = {run["source"] for run in runs}
    _require(len(modes) <= 1, "RESULT_MIXED_MODE", "mixed modes")
    _require(len(sources) <= 1, "RESULT_MIXED_SOURCE", "mixed execution sources")
    _require(all(run["mode"] == "real" for run in runs), "RESULT_MODE", "live batch must use real mode")
    total_calls = sum(run["client_calls"] for run in runs)
    _require(total_calls <= 12, "RESULT_CLIENT_CALLS", "total client calls exceed 12")
    completion_known = 0
    usage_unknown = False
    for run in runs:
        usage = run.get("usage")
        if not isinstance(usage, dict) or any(usage.get(key) is None for key in ("prompt_tokens", "completion_tokens", "total_tokens")):
            usage_unknown = True
            continue
        completion_known += usage["completion_tokens"]
    _require(completion_known <= MAX_COMPLETION_TOKENS_TOTAL, "RESULT_USAGE_TOTAL", "known completion tokens exceed 6144")
    _require(len({row["case_id"] for row in rows}) == len(rows), "RESULT_DUPLICATE", "duplicate result rows")

    source_all_zero = (
        (isinstance(source_sha, str) and set(source_sha.lower()) == {"0"})
        or (isinstance(source_sha, dict) and any(set(value.lower()) == {"0"} for value in source_sha.values()))
    )
    source_actual = {}
    source_verified = False
    if isinstance(source_sha, dict):
        source_dir = Path(source_root) if source_root is not None else (ROOT / "projects" / "A-mes-assistant" / "mes_assistant")
        for name, expected_hash in source_sha.items():
            path = source_dir / name
            source_actual[name] = sha256_file(path) if path.is_file() else None
        source_verified = (
            expected_source_sha256 is not None
            and not source_all_zero
            and all(source_actual.get(name) == expected_hash.lower() for name, expected_hash in source_sha.items())
        )
    certified = bool(
        budget is not None
        and expected_source_sha256 is not None
        and source_verified
        and not source_all_zero
    )
    certification_reason = None
    if not certified:
        if budget is None:
            certification_reason = "approved budget not supplied"
        elif expected_source_sha256 is None:
            certification_reason = "independent source reference not supplied"
        elif source_all_zero:
            certification_reason = "source SHA256 is all zero"
        elif not source_verified:
            certification_reason = "source SHA files do not match actual files"
    return {
        "inputs": inputs,
        "fixture": fixture,
        "manifest": manifest,
        "ledger": ledger,
        "results": rows,
        "budget": budget,
        "input_sha256": input_sha,
        "expected_source_sha256": expected_source_sha256,
        "state": state,
        "source": next(iter(sources)) if sources else None,
        "test_double": (next(iter(sources)) == "test_double") if sources else False,
        "source_actual": source_actual,
        "source_verified": source_verified,
        "certified": certified,
        "certification_reason": certification_reason,
        "ledger_calls_unknown": ledger_calls_unknown,
        "usage_unknown": usage_unknown,
        "known_completion_tokens": completion_known,
    }


def _evidence_field_error(case: dict, actual_value, item, *, required: bool) -> str | None:
    text = case["text"]
    if actual_value is None:
        if required:
            return "evidence_missing"
        return None if item is None else "evidence_for_null"
    if not isinstance(item, dict) or set(item) != {"quote", "start", "end"}:
        return "evidence_shape"
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
        return "evidence_invalid"
    first = text.find(quote)
    if first < 0 or text.find(quote, first + 1) != -1:
        return "evidence_not_unique"
    return None


def summarize_records(
    cases: list[dict],
    records: list[dict],
    *,
    mode: str,
    source: str,
    state: str = "complete",
    coverage: int = 12,
    badcase_details: bool = True,
) -> dict:
    by_id = {record["case_id"]: record for record in records}
    ready_cases = [case for case in cases if case["expected"]["state"] == "ready"]
    negative_cases = [case for case in cases if case["expected"]["state"] != "ready"]
    field_exact = 0
    record_exact = 0
    evidence_valid = 0
    evidence_invalid = []
    field_total = 0
    evidence_total = 0
    error_ready = []
    badcases = []
    for case in cases:
        record = by_id.get(case["id"])
        if record is None:
            if badcase_details:
                badcases.append({"case_id": case["id"], "verdict": "missing", "reason_code": "missing_case", "expected_state": case["expected"]["state"]})
            continue
        if case["expected"]["state"] != "ready" and record.get("actual_state") == "ready":
            error_ready.append(case["id"])
        if record.get("verdict") != "pass" and badcase_details:
            badcases.append(
                {
                    "case_id": case["id"],
                    "expected_state": case["expected"]["state"],
                    "actual_state": record.get("actual_state"),
                    "verdict": record.get("verdict"),
                    "reason_code": record.get("reason_code"),
                    "fields": record.get("fields", {}),
                    "evidence_errors": record.get("evidence_errors", []),
                }
            )
    for case in ready_cases:
        record = by_id.get(case["id"], {})
        expected_record = case["expected"]["record"]
        actual_record = record.get("fields") or {}
        actual_state = record.get("actual_state")
        # Actual values are only available when the result state is ready.
        draft_record = None
        # field status is enough for exactness; judge preserves actual values.
        exact_record = actual_state == "ready"
        exact_fields = 0
        for field in ev.FIELDS:
            field_total += 1
            status = actual_record.get(field, {}).get("status") if isinstance(actual_record, dict) else None
            if status == "match":
                exact_fields += 1
            if status != "match":
                exact_record = False
        field_exact += exact_fields
        if exact_record:
            record_exact += 1
        evidence_total += len(ev.FIELDS)
        # Evidence validity is measured only when the row actually produced a
        # ready record. A non-ready row has no applicable field evidence and
        # cannot earn vacuous evidence points.
        evidence = (record.get("_draft") or {}).get("evidence") if isinstance(record, dict) else None
        if actual_state != "ready":
            continue
        actual_values = {}
        for field in ev.FIELDS:
            actual_values[field] = actual_record.get(field, {}).get("actual") if isinstance(actual_record, dict) else None
        for field in ev.FIELDS:
            required = expected_record[field] is not None
            err = _evidence_field_error(
                case,
                actual_values.get(field),
                evidence.get(field) if isinstance(evidence, dict) else None,
                required=required,
            )
            if err is None:
                evidence_valid += 1
            else:
                evidence_invalid.append({"case_id": case["id"], "field": field, "error": err})
    complete = state == "complete" and coverage == 12
    rates = None
    if complete:
        rates = {
            "case_pass": round(sum(record.get("verdict") == "pass" for record in records) / 12, 6),
            "record_exact": round(record_exact / 6, 6),
            "field_exact": round(field_exact / 30, 6),
            "evidence_valid": round(evidence_valid / 30, 6),
            "error_ready": round(len(error_ready) / 6, 6),
        }
    safety_gate = (len(error_ready) == 0) if complete else None
    return {
        "mode": mode,
        "source": source,
        "state": state,
        "coverage": {"observed": coverage, "planned": 12, "rate": round(coverage / 12, 6)},
        "case_pass": sum(record.get("verdict") == "pass" for record in records),
        "case_total": 12,
        "record_exact": record_exact,
        "record_denominator": 6,
        "field_exact": field_exact,
        "field_denominator": 30,
        "evidence_valid": evidence_valid,
        "evidence_denominator": 30,
        "error_ready": len(error_ready),
        "error_ready_denominator": 6,
        "safety_gate": safety_gate,
        "status": ("changes_requested" if not safety_gate else "ok") if complete else state,
        "rates": rates,
        "badcases": badcases if badcase_details else [],
        "evidence_invalid": evidence_invalid,
        "test_double": source == "test_double",
        "real_quality": bool(complete and source == "http_model"),
    }


def summarize_drafts(cases: list[dict], drafts: list[dict], *, mode: str, source: str) -> dict:
    records = []
    for case in cases:
        draft = next((item for item in drafts if item.get("case_id") == case["id"]), None)
        judged = ev.judge_draft(case, draft)
        judged["_draft"] = draft
        records.append(judged)
    return summarize_records(cases, records, mode=mode, source=source, state="complete", coverage=12)


def summarize_live(batch: dict) -> dict:
    cases = batch["fixture"]["cases"]
    results = batch["results"]
    judged = []
    for row in results:
        record = ev.judge_draft(next(case for case in cases if case["id"] == row["case_id"]), row["draft"])
        record["_draft"] = row["draft"]
        record["_run"] = row["run"]
        judged.append(record)
    state = batch["state"]
    coverage = len(results)
    summary = summarize_records(
        cases,
        judged,
        mode="real",
        source=batch["source"] or "unknown",
        state=state if state != "complete" else "complete",
        coverage=coverage,
    )
    summary["test_double"] = batch["test_double"]
    summary["certification"] = "certified" if batch["certified"] else "unverified"
    summary["certification_reason"] = batch["certification_reason"]
    summary["source_verified"] = batch["source_verified"]
    summary["ledger_calls_unknown"] = batch["ledger_calls_unknown"]
    summary["known_completion_tokens"] = batch["known_completion_tokens"]
    summary["real_quality"] = bool(
        state == "complete"
        and coverage == 12
        and batch["source"] == "http_model"
        and batch["certified"]
    )
    if state == "complete" and batch["source"] == "http_model" and not batch["certified"]:
        summary["state"] = "unverified"
        summary["rates"] = None
        summary["status"] = "unverified"
        summary["real_quality"] = False
    summary["manifest"] = {
        "run_id": RUN_ID,
        "config_id": CONFIG_ID,
        "provider": batch["manifest"].get("provider"),
        "model": batch["manifest"].get("model"),
        "state": state,
        "stop_reason": batch["manifest"].get("stop_reason"),
        "planned": 12,
        "observed": coverage,
        "input_sha256": batch["input_sha256"],
        "source_sha256": batch["manifest"].get("source_sha256"),
    }
    if state != "complete":
        summary["rates"] = None
        summary["real_quality"] = False
        summary["badcases"].extend(
            {"case_id": case["id"], "verdict": "missing", "reason_code": "incomplete_batch", "expected_state": case["expected"]["state"]}
            for case in cases
            if case["id"] not in {row["case_id"] for row in results}
        )
    summary["error_ready_ids"] = [
        record["case_id"] for record in judged if record["expected_state"] != "ready" and record.get("actual_state") == "ready"
    ]
    summary["client_calls"] = sum(row["run"]["client_calls"] for row in results)
    summary["client_calls_semantics"] = "complete_calls"
    known_usage = []
    for row in results:
        usage = row["run"].get("usage")
        if not isinstance(usage, dict):
            known_usage = []
            break
        values = []
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(key)
            if value is None:
                known_usage = []
                break
            values.append(value)
        if not values:
            break
        known_usage.append(values)
    if known_usage and len(known_usage) == len(results):
        summary["usage"] = {
            "prompt_tokens": sum(item[0] for item in known_usage),
            "completion_tokens": sum(item[1] for item in known_usage),
            "total_tokens": sum(item[2] for item in known_usage),
        }
        summary["usage_unknown"] = False
    else:
        summary["usage"] = {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
        summary["usage_unknown"] = True
    summary["usage_unknown"] = bool(summary["usage_unknown"] or batch["usage_unknown"])
    summary["safety_gate"] = (len(summary["error_ready_ids"]) == 0) if state == "complete" and coverage == 12 else None
    if summary.get("state") != "unverified":
        summary["status"] = ("changes_requested" if not summary["safety_gate"] else "ok") if summary["safety_gate"] is not None else state
    return summary


def public_summary(rule: dict, scripted: dict, live: dict | None, *, dataset_sha256: str, input_sha256: str | None) -> dict:
    return {
        "contract_version": LIVE_CONTRACT,
        "dataset_sha256": dataset_sha256,
        "input_sha256": input_sha256,
        "rule": rule,
        "scripted": scripted,
        "live": live if live is not None else {
            "mode": "real",
            "source": "pending",
            "state": "pending_live",
            "coverage": {"observed": 0, "planned": 12, "rate": 0.0},
            "case_pass": 0,
            "case_total": 12,
            "record_exact": 0,
            "record_denominator": 6,
            "field_exact": 0,
            "field_denominator": 30,
            "evidence_valid": 0,
            "evidence_denominator": 30,
            "error_ready": None,
            "error_ready_denominator": 6,
            "rates": None,
            "badcases": [],
            "evidence_invalid": [],
            "test_double": False,
            "real_quality": False,
            "pending_reason": "real batch not executed; budget approval and A-011 results required",
        },
    }


def render_report(summary: dict) -> str:
    lines = [
        "# C-010 M5 live intake evaluation",
        "",
        "- dataset_sha256: `%s`" % summary["dataset_sha256"],
        "- input_sha256: `%s`" % (summary["input_sha256"] or "pending"),
        "- contract: `%s`" % LIVE_CONTRACT,
        "- rule/scripted/live are reported separately; no model calls in this command.",
        "",
        "| layer | source | state | coverage | case | record | fields | evidence | error-ready |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name in ("rule", "scripted", "live"):
        item = summary[name]
        rates = item.get("rates") or {}
        lines.append(
            "| %s | %s | %s | %s/12 | %s/12 | %s/6 | %s/30 | %s/30 | %s/6 |"
            % (
                name,
                item.get("source"),
                item.get("state"),
                item.get("coverage", {}).get("observed", 0),
                item.get("case_pass", 0),
                item.get("record_exact", 0),
                item.get("field_exact", 0),
                item.get("evidence_valid", 0),
                "null" if item.get("error_ready") is None else item.get("error_ready"),
            )
        )
    lines.extend(["", "## Live", ""])
    live = summary["live"]
    if live["state"] == "pending_live":
        lines.append("真实批次 **pending_live**：预算待批准、A-011 结果未到；质量率保持 null。")
    elif live["state"] != "complete":
        lines.append("真实批次 **%s**：分母保留 12，质量率 null，覆盖 %d/12。" % (live["state"], live["coverage"]["observed"]))
    elif live.get("test_double"):
        lines.append("真实回放来源为 **test_double**；仅验证合同，不声明真实模型质量。")
    else:
        lines.append("真实批次完整；按实际结果计算，模型质量结论由总控/人审。")
    lines.extend(["", "## Badcases", ""])
    for layer in ("rule", "scripted", "live"):
        for item in summary[layer].get("badcases", [])[:20]:
            lines.append("- %s: `%s` %s / %s" % (layer, item.get("case_id"), item.get("verdict"), item.get("reason_code")))
    lines.extend(["", "## Limits", "", "- 规则基线仅显式模板；脚本为合同 oracle；真实模型质量只由真实结果判断。", "- 错误 ready 是安全门槛；确认路径的实际写入由 A/B 联合另测。", ""])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C-010 live intake evaluation")
    parser.add_argument("--inputs", type=Path, default=DEFAULT_INPUTS)
    parser.add_argument("--fixture", type=Path, default=FIXTURE_PATH)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--budget", type=Path)
    parser.add_argument("--source-sha256")
    parser.add_argument("--source-map", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--export-inputs", action="store_true")
    args = parser.parse_args(argv)
    if args.export_inputs:
        result = export_live_inputs(fixture_path=args.fixture, output_path=args.inputs)
        print(json.dumps({"input_sha256": result["input_sha256"], "case_count": result["case_count"]}, ensure_ascii=False))
        return 0
    inputs, fixture = load_live_inputs(args.inputs, fixture_path=args.fixture)
    cases = fixture["cases"]
    rule = summarize_drafts(cases, ev.bound_rule_drafts(cases), mode="rule", source="rule_template")
    scripted = summarize_drafts(cases, ev.scripted_drafts(cases), mode="script", source="scripted_oracle")
    live = None
    if args.manifest and args.ledger and args.results:
        source_reference = load_json(args.source_map) if args.source_map else args.source_sha256
        batch = validate_batch(
            inputs_path=args.inputs,
            manifest_path=args.manifest,
            ledger_path=args.ledger,
            results_path=args.results,
            budget_path=args.budget,
            expected_source_sha256=source_reference,
            source_root=args.source_root,
            fixture_path=args.fixture,
        )
        live = summarize_live(batch)
    summary = public_summary(rule, scripted, live, dataset_sha256=LIVE_DATASET_SHA, input_sha256=sha256_file(args.inputs))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "live-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.output_dir / "live-report.md").write_text(render_report(summary), encoding="utf-8")
    print(json.dumps({"rule": rule["case_pass"], "scripted": scripted["case_pass"], "live": None if live is None else live["state"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
