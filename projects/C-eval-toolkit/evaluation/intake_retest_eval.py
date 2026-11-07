"""C-011 m5-pilot-002 retest adapter.

The original intake_live_eval is frozen for m5-pilot-001. This module validates a
separate, explicitly bound m5-pilot-002 batch and reuses the unchanged judge.

Certification requires all of:
* approved parent S1 budget and its 002 batch entry;
* child budget SHA/content/parent binding and source-map SHA;
* manifest results/ledger/budget SHA values matching the actual files;
* ledger run_id m5-pilot-002 and per-case call/order consistency;
* source-map covering runtime/batch/extractor and matching actual files;
* http_model calls with provider/model for all model-participating cases;
* zero-call rows only as a clearly evidenced pre-model guard.

No model, network, SQLite or MySQL is called.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from evaluation import intake_eval as ev
from evaluation import intake_live_eval as live

PARENT_BUDGET = live.ROOT / "coordination" / "budgets" / "S1-20261107-授权预算.json"
PARENT_SHA = "7a42c64df858d1d737bfc19a584ede8dc3914cd43edd49e8070084cc669c7059"
CHILD_BUDGET = live.ROOT / "execution" / "A" / "A-012" / "budget-002.json"
RUN_ID = "m5-pilot-002"
RESULT_DIR = "execution/A/A-012/experiment/m5-pilot-002"
INPUTS = live.ROOT / "execution" / "C" / "C-010" / "live-inputs.json"
DATASET_SHA = "c9f4001d87a9e4b5e0a6f44aa308e4f0e84ca17041a87972bd1f646480ca84ce"
INPUT_SHA = "369e5dc872532bc3b8b66d6fa675ac9e45c5d59aae7b4bc7e26087edf935890e"
PROVIDER = "siliconflow"
MODEL = "Qwen/Qwen3-30B-A3B-Instruct-2507"
REQUIRED_SOURCE = ("intake_runtime.py", "intake_batch.py", "intake_extraction.py")


class RetestError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _require(condition, code, message):
    if not condition:
        raise RetestError(code, message)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RetestError("JSON_INVALID", "JSON is not readable") from exc


def load_parent(path: Path = PARENT_BUDGET) -> dict:
    _require(_sha(path) == PARENT_SHA, "PARENT_SHA", "parent budget SHA mismatch")
    data = _json(path)
    _require(data.get("schema_version") == "s1-portfolio-live-budget-v1", "PARENT_SCHEMA", "parent schema")
    _require(data.get("status") == "approved", "PARENT_STATUS", "parent not approved")
    _require(data.get("provider") == PROVIDER and data.get("model") == MODEL, "PARENT_MODEL", "parent model")
    batches = data.get("batches")
    _require(isinstance(batches, list), "PARENT_BATCHES", "parent batches")
    entry = next((item for item in batches if isinstance(item, dict) and item.get("run_id") == RUN_ID), None)
    _require(entry is not None, "PARENT_002", "002 batch missing")
    _require(entry.get("max_attempts") == 12 and entry.get("max_attempts_per_case") == 1, "PARENT_002", "002 caps")
    _require(entry.get("input_path") == "execution/C/C-010/live-inputs.json", "PARENT_002", "002 input")
    _require(entry.get("input_sha256") == INPUT_SHA and entry.get("dataset_sha256") == DATASET_SHA, "PARENT_002", "002 input SHA")
    _require(entry.get("result_directory") == RESULT_DIR, "PARENT_002", "002 result dir")
    return {"data": data, "batch": entry, "sha256": _sha(path)}


def load_source_map(path: Path) -> dict:
    data = _json(path)
    _require(isinstance(data, dict) and data, "SOURCE_MAP", "source map")
    for name, digest in data.items():
        _require(isinstance(name, str) and isinstance(digest, str) and len(digest) == 64, "SOURCE_MAP", "source map entry")
    _require(set(REQUIRED_SOURCE).issubset(data), "SOURCE_MAP_COVERAGE", "runtime/batch/extractor required")
    return data


def load_child_budget(path: Path, *, parent_sha: str, source_map_path: Path) -> dict:
    _require(Path(path).exists(), "CHILD_BUDGET", "child budget missing")
    data = _json(path)
    _require(data.get("contract_version") == live.LIVE_CONTRACT, "CHILD_CONTRACT", "child contract")
    _require(data.get("status") == "approved", "CHILD_STATUS", "child not approved")
    _require(data.get("run_id") == RUN_ID, "CHILD_RUN", "child run")
    _require(data.get("config_id") == "m5-siliconflow-qwen30-v1", "CHILD_CONFIG", "child config")
    _require(data.get("provider") == PROVIDER and data.get("model") == MODEL, "CHILD_MODEL", "child model")
    _require(data.get("dataset_sha256") == DATASET_SHA, "CHILD_DATASET", "child dataset")
    _require(data.get("inputs_path") == "execution/C/C-010/live-inputs.json" and data.get("input_sha256") == INPUT_SHA, "CHILD_INPUT", "child input")
    _require(data.get("parent_budget_sha256") == parent_sha, "CHILD_PARENT", "child parent SHA")
    _require(data.get("result_directory") == RESULT_DIR, "CHILD_DIR", "child result dir")
    _require(data.get("max_requests") == 12 and data.get("max_client_calls") == 12 and data.get("max_calls_per_case") == 1, "CHILD_CAPS", "child caps")
    _require(data.get("automatic_retries") == 0 and data.get("write_policy") == "draft_only_no_commit", "CHILD_POLICY", "child policy")
    _require(data.get("source_map_sha256") == _sha(source_map_path), "CHILD_MAP", "child source map SHA")
    return {"data": data, "sha256": _sha(path)}


def validate_manifest(manifest: object, *, input_sha: str, source_map: dict, parent_sha: str, child_sha: str, results_sha: str, ledger_sha: str) -> dict:
    _require(isinstance(manifest, dict), "MANIFEST_SHAPE", "manifest")
    _require(manifest.get("contract_version") == live.LIVE_CONTRACT, "MANIFEST_CONTRACT", "manifest contract")
    _require(manifest.get("run_id") == RUN_ID, "MANIFEST_RUN", "manifest run")
    _require(manifest.get("config_id") == "m5-siliconflow-qwen30-v1", "MANIFEST_CONFIG", "config")
    _require(manifest.get("provider") == PROVIDER and manifest.get("model") == MODEL, "MANIFEST_MODEL", "model")
    _require(manifest.get("parent_budget_sha256") == parent_sha, "MANIFEST_PARENT", "parent budget binding")
    _require(manifest.get("budget_sha256") == child_sha, "MANIFEST_BUDGET", "child budget SHA mismatch")
    _require(manifest.get("dataset_sha256") == DATASET_SHA, "MANIFEST_DATASET", "dataset")
    _require(manifest.get("input_sha256") == input_sha, "MANIFEST_INPUT", "input")
    _require(manifest.get("source_sha256") == source_map, "MANIFEST_SOURCE", "source map")
    _require(manifest.get("results_sha256") == results_sha, "MANIFEST_RESULTS_SHA", "results SHA mismatch")
    _require(manifest.get("ledger_sha256") == ledger_sha, "MANIFEST_LEDGER_SHA", "ledger SHA mismatch")
    state = manifest.get("state")
    _require(state in ("completed", "complete", "incomplete", "failed"), "MANIFEST_STATE", "state")
    planned = manifest.get("planned_count", manifest.get("planned"))
    observed = manifest.get("observed_count", manifest.get("observed"))
    _require(planned == 12, "MANIFEST_COUNT", "planned")
    _require(isinstance(observed, int) and 0 <= observed <= 12, "MANIFEST_COUNT", "observed")
    _require(not manifest.get("real_batch_reused"), "MANIFEST_REUSE", "real batch reuse")
    return {"manifest": manifest, "state": "complete" if state in ("completed", "complete") else state, "observed": observed}


def _validate_rows(rows: list, ledger: dict, cases: dict) -> tuple[list, list]:
    _require(ledger.get("run_id") == RUN_ID, "LEDGER_RUN", "ledger run_id mismatch")
    ledger_entries = live._ledger_entries(ledger)
    observed_ids = [entry["case_id"] for entry in ledger_entries if entry["observed"]]
    _require([row["case_id"] for row in rows] == observed_ids, "LEDGER_RESULTS", "ledger/results IDs/order")
    _require(len(rows) == 12, "RESULT_COUNT", "12 rows required")
    ledger_by_id = {entry["case_id"]: entry for entry in ledger_entries}
    http_rows, guard_rows = [], []
    total_calls = 0
    total_completion = 0
    for row in rows:
        case_id = row["case_id"]
        _require(case_id in cases, "RESULT_CASE", "unknown case")
        entry = ledger_by_id.get(case_id)
        _require(entry is not None, "LEDGER_MISSING", "ledger case missing")
        _require(entry.get("reserved") is True and entry.get("observed") is True, "LEDGER_STATE", "case not reserved/observed")
        run = row.get("run")
        _require(isinstance(run, dict), "RUN_SHAPE", "run shape")
        calls = run.get("client_calls")
        _require(isinstance(calls, int) and not isinstance(calls, bool) and 0 <= calls <= 1, "RUN_CALLS", "client calls")
        _require(entry.get("client_calls") == calls, "LEDGER_CALLS", "ledger/result calls")
        _require(run.get("mode") == "real", "RUN_MODE", "run mode")
        source = run.get("source", run.get("execution_source"))
        _require(source in ("http_model", "test_double"), "RUN_SOURCE", "execution source")
        total_calls += calls
        _require(total_calls <= 12, "RUN_TOTAL_CALLS", "total calls")
        usage = run.get("usage")
        completion = usage.get("completion_tokens") if isinstance(usage, dict) else None
        if completion is not None:
            _require(isinstance(completion, int) and 0 <= completion <= 512, "RUN_COMPLETION", "completion cap")
            total_completion += completion
        if calls > 0:
            _require(source == "http_model", "REAL_SOURCE", "test_double cannot certify real")
            _require(run.get("provider") == PROVIDER and run.get("model") == MODEL, "RUN_MODEL", "provider/model")
            http_rows.append(row)
        else:
            _require(source == "http_model", "REAL_SOURCE", "test_double zero-call cannot certify real")
            draft = row.get("draft") if isinstance(row.get("draft"), dict) else {}
            issues = draft.get("issues") or []
            expected = cases[case_id]["expected"]
            _require(draft.get("state") == "needs_clarification" and "MULTIPLE_EVENTS" in issues and expected.get("issue") == "MULTIPLE_EVENTS" and draft.get("terminal") is True, "GUARD_EVIDENCE", "zero call is not a proven pre-model guard")
            guard_rows.append(row)
    _require(total_completion <= 6144, "RUN_TOTAL_COMPLETION", "completion total cap")
    return http_rows, guard_rows


def validate_batch(*, inputs_path=INPUTS, parent_path=PARENT_BUDGET, child_budget_path=CHILD_BUDGET, source_map_path=None, source_root=None, manifest_path, ledger_path, results_path, output_dir) -> dict:
    live.load_live_inputs(inputs_path)
    parent = load_parent(parent_path)
    _require(_sha(inputs_path) == INPUT_SHA, "INPUT_SHA", "input SHA")
    source_map = load_source_map(source_map_path)
    source_dir = Path(source_root) if source_root else live.ROOT / "projects" / "A-mes-assistant" / "mes_assistant"
    actual = {name: _sha(source_dir / name) for name in source_map}
    _require(actual == source_map, "SOURCE_ROOT", "source files differ from map")
    child = load_child_budget(child_budget_path, parent_sha=parent["sha256"], source_map_path=source_map_path)
    results_sha = _sha(results_path)
    ledger_sha = _sha(ledger_path)
    manifest_info = validate_manifest(_json(manifest_path), input_sha=INPUT_SHA, source_map=source_map, parent_sha=parent["sha256"], child_sha=child["sha256"], results_sha=results_sha, ledger_sha=ledger_sha)
    rows = live.load_results(results_path)
    ledger = _json(ledger_path)
    cases = {case["id"]: case for case in ev.load_fixture()["cases"]}
    http_rows, guard_rows = _validate_rows(rows, ledger, cases)
    judged = []
    for row in rows:
        record = ev.judge_draft(cases[row["case_id"]], row["draft"])
        record["_draft"] = row["draft"]
        record["_run"] = row["run"]
        judged.append(record)
    summary = live.summarize_records(list(cases.values()), judged, mode="real", source="http_model", state=manifest_info["state"], coverage=len(rows))
    summary.update({
        "run_id": RUN_ID,
        "parent_budget_sha256": parent["sha256"],
        "child_budget_sha256": child["sha256"],
        "results_sha256": results_sha,
        "ledger_sha256": ledger_sha,
        "source_map_sha256": _sha(source_map_path),
        "http_model_cases": [row["case_id"] for row in http_rows],
        "program_guard_cases": [row["case_id"] for row in guard_rows],
        "http_model_calls": sum(row["run"]["client_calls"] for row in http_rows),
        "certification": "certified",
        "certification_reason": None,
        "real_quality": bool(manifest_info["state"] == "complete" and len(rows) == 12 and http_rows),
        "test_double": False,
        "quality_scope": "mixed_system_guard_and_http" if guard_rows else "http_model_only",
        "source_verified": True,
        "ledger_calls_unknown": False,
    })
    main_state_pass = 0
    for row in rows:
        expected_state = cases[row["case_id"]]["expected"]["state"]
        actual_state = (row.get("draft") or {}).get("state")
        if actual_state == expected_state:
            main_state_pass += 1
    calls = sum(row["run"]["client_calls"] for row in rows)
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    usage_unknown = False
    for row in rows:
        value = row["run"].get("usage")
        if not isinstance(value, dict):
            usage_unknown = True
            continue
        for key in usage:
            if isinstance(value.get(key), int):
                usage[key] += value[key]
            else:
                usage_unknown = True
    summary["main_state_pass"] = main_state_pass
    summary["client_calls"] = calls
    summary["known_completion_tokens"] = usage["completion_tokens"]
    summary["usage"] = usage
    summary["usage_unknown"] = usage_unknown
    summary["status"] = "changes_requested" if summary["case_pass"] < 12 else "ok"
    if manifest_info["state"] != "complete":
        summary["rates"] = None
        summary["real_quality"] = False
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "retest-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="m5-pilot-002 retest adapter")
    parser.add_argument("--inputs", type=Path, default=INPUTS)
    parser.add_argument("--parent-budget", type=Path, default=PARENT_BUDGET)
    parser.add_argument("--child-budget", type=Path, default=CHILD_BUDGET)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--source-map", type=Path, required=True)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        summary = validate_batch(
            inputs_path=args.inputs,
            parent_path=args.parent_budget,
            child_budget_path=args.child_budget,
            source_map_path=args.source_map,
            source_root=args.source_root,
            manifest_path=args.manifest,
            ledger_path=args.ledger,
            results_path=args.results,
            output_dir=args.output_dir,
        )
    except RetestError as exc:
        print(json.dumps({"error": exc.code}, ensure_ascii=False))
        return 2
    print(json.dumps({"state": summary["state"], "case_pass": summary["case_pass"], "error_ready": summary["error_ready"], "http_cases": len(summary["http_model_cases"]), "guard_cases": len(summary["program_guard_cases"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
