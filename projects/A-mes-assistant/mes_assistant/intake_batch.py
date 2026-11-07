"""A-011 fixed 12-case batch driver.

Only this module writes the live result directory.  Dry-run is the default and
never creates a result directory or dispatches a request.  Execution requires
an approved budget and a fresh result directory; reserved/observed ledger
entries prevent duplicate dispatch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

from mes_assistant.intake_runtime import (
    CONFIG_MODEL_MISMATCH,
    STOP_CODES,
    check_real_config,
    run_intake_request,
)

WORKSPACE = Path(__file__).resolve().parents[3]
DEFAULT_INPUTS = WORKSPACE / "execution" / "C" / "C-010" / "live-inputs.json"
DEFAULT_BUDGET = WORKSPACE / "coordination" / "M5-实测预算.json"
FIXED_RESULT_REL = "execution/A/A-011/experiment/m5-pilot-001"
DEFAULT_RESULT_DIR = WORKSPACE / FIXED_RESULT_REL
PARENT_BUDGET = WORKSPACE / "coordination" / "budgets" / "S1-20261107-授权预算.json"
CONTRACT = "m5-intake-live-v1"
RUN_ID = "m5-pilot-001"
CONFIG_ID = "m5-siliconflow-qwen30-v1"
EXPECTED_CASES = ["i%02d" % index for index in range(1, 13)]
RUN_SPECS = {
    "001": {
        "run_id": "m5-pilot-001",
        "config_id": "m5-siliconflow-qwen30-v1",
        "result_rel": FIXED_RESULT_REL,
    },
    "002": {
        "run_id": "m5-pilot-002",
        "config_id": "m5-siliconflow-qwen30-v1",
        "result_rel": "execution/A/A-012/experiment/m5-pilot-002",
    },
}


def _spec(value):
    if value not in RUN_SPECS:
        raise BatchError("BUDGET_INVALID", "unknown run spec")
    return RUN_SPECS[value]


class BatchError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


def _sha_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _sha_source():
    return {
        name: _sha_file(Path(__file__).resolve().parent / name)
        for name in ("intake_runtime.py", "intake_batch.py", "intake_extraction.py")
    }


def _load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise BatchError("INPUT_BINDING_FAILED", "JSON input is not readable")


def load_inputs(path):
    raw = Path(path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    data = _load_json(path)
    if not isinstance(data, dict):
        raise BatchError("INPUT_BINDING_FAILED", "input root is not an object")
    if set(data) != {"contract_version", "dataset_sha256", "reference_time", "timezone", "cases"}:
        raise BatchError("INPUT_BINDING_FAILED", "input has unexpected fields")
    if data.get("contract_version") != CONTRACT:
        raise BatchError("INPUT_BINDING_FAILED", "contract_version mismatch")
    if not isinstance(data.get("dataset_sha256"), str) or len(data["dataset_sha256"]) != 64:
        raise BatchError("INPUT_BINDING_FAILED", "dataset_sha256 is invalid")
    if data.get("reference_time") != "2026-11-04T12:00:00+08:00":
        raise BatchError("INPUT_BINDING_FAILED", "reference_time mismatch")
    if data.get("timezone") != "Asia/Shanghai":
        raise BatchError("INPUT_BINDING_FAILED", "timezone mismatch")
    cases = data.get("cases")
    if not isinstance(cases, list) or len(cases) != 12:
        raise BatchError("INPUT_BINDING_FAILED", "cases must contain 12 items")
    for index, item in enumerate(cases):
        if not isinstance(item, dict) or set(item) != {"case_id", "text"}:
            raise BatchError("INPUT_BINDING_FAILED", "case shape is invalid")
        if item["case_id"] != EXPECTED_CASES[index]:
            raise BatchError("INPUT_BINDING_FAILED", "case ids must be i01-i12")
        if not isinstance(item["text"], str) or not item["text"].strip() or len(item["text"]) > 2000:
            raise BatchError("INPUT_BINDING_FAILED", "case text is invalid")
    return data, digest


def _budget(data, inputs, input_sha, *, require_approved, run_spec="001"):
    spec = _spec(run_spec)
    if not isinstance(data, dict):
        raise BatchError("BUDGET_INVALID", "budget root is not an object")
    required = {
        "contract_version",
        "status",
        "run_id",
        "config_id",
        "provider",
        "model",
        "dataset_sha256",
        "inputs_path",
        "input_sha256",
        "case_ids",
        "mode",
        "max_requests",
        "max_client_calls",
        "max_calls_per_case",
        "call_timeout_ms",
        "max_completion_tokens_per_call",
        "max_completion_tokens_total",
        "automatic_retries",
        "write_policy",
        "result_directory",
        "stop_on",
    }
    if not required.issubset(data):
        raise BatchError("BUDGET_INVALID", "budget is missing required fields")
    if data.get("contract_version") != CONTRACT:
        raise BatchError("BUDGET_INVALID", "budget contract mismatch")
    if require_approved and data.get("status") != "approved":
        raise BatchError("BUDGET_INVALID", "budget is not approved")
    if data.get("run_id") != spec["run_id"] or data.get("config_id") != spec["config_id"]:
        raise BatchError("BUDGET_INVALID", "run/config id mismatch")
    if data.get("provider") != "siliconflow" or not data.get("model"):
        raise BatchError("BUDGET_INVALID", "provider/model mismatch")
    if data.get("mode") != "real":
        raise BatchError("BUDGET_INVALID", "mode mismatch")
    if data.get("dataset_sha256") != inputs.get("dataset_sha256"):
        raise BatchError("INPUT_BINDING_FAILED", "dataset sha mismatch")
    if data.get("case_ids") != EXPECTED_CASES:
        raise BatchError("BUDGET_INVALID", "case ids mismatch")
    if data.get("max_requests") != 12 or data.get("max_client_calls") != 12:
        raise BatchError("BUDGET_INVALID", "request cap mismatch")
    if data.get("max_calls_per_case") != 1 or data.get("automatic_retries") != 0:
        raise BatchError("BUDGET_INVALID", "retry cap mismatch")
    if data.get("call_timeout_ms") != 10000:
        raise BatchError("BUDGET_INVALID", "timeout mismatch")
    if data.get("max_completion_tokens_per_call") != 512:
        raise BatchError("BUDGET_INVALID", "completion cap mismatch")
    if data.get("max_completion_tokens_total") != 6144:
        raise BatchError("BUDGET_INVALID", "total completion cap mismatch")
    if data.get("write_policy") != "draft_only_no_commit":
        raise BatchError("BUDGET_INVALID", "write policy mismatch")
    if data.get("result_directory") != spec["result_rel"]:
        raise BatchError("BUDGET_INVALID", "result_directory must be the fixed relative path")
    if run_spec == "002" and not data.get("parent_budget_sha256"):
        raise BatchError("BUDGET_INVALID", "002 requires parent_budget_sha256")
    if run_spec == "002" and data.get("parent_budget_sha256") != _sha_file(PARENT_BUDGET):
        raise BatchError("BUDGET_INVALID", "parent budget sha mismatch")
    if run_spec == "002":
        parent_path = Path(str(data.get("parent_budget_path") or ""))
        if not parent_path.is_absolute():
            parent_path = WORKSPACE / parent_path
        if parent_path.resolve() != PARENT_BUDGET.resolve():
            raise BatchError("BUDGET_INVALID", "parent budget path mismatch")
        source_map_path = Path(str(data.get("source_map_path") or ""))
        if not source_map_path.is_absolute():
            source_map_path = WORKSPACE / source_map_path
        if not source_map_path.is_file():
            raise BatchError("BUDGET_INVALID", "source map is missing")
        if _sha_file(source_map_path) != data.get("source_map_sha256"):
            raise BatchError("BUDGET_INVALID", "source map sha mismatch")
        source_map = _load_json(source_map_path)
        if not isinstance(source_map, dict) or source_map != _sha_source():
            raise BatchError("BUDGET_INVALID", "source map does not match actual source")
    if require_approved and data.get("input_sha256") != input_sha:
        raise BatchError("INPUT_BINDING_FAILED", "input sha mismatch")
    return data


def _preflight_real_config(budget, env):
    checked = check_real_config(
        env,
        expected_provider=budget["provider"],
        expected_model=budget["model"],
    )
    if not checked["ok"]:
        raise BatchError(checked["error_code"], "real configuration does not match the budget")
    return checked


def dry_run(inputs_path, budget_path, *, run_spec="001"):
    inputs, input_sha = load_inputs(inputs_path)
    budget = _load_json(budget_path)
    # Dry run may inspect a pending budget; it still reports binding status.
    budget_ok = False
    budget_code = None
    try:
        _budget(budget, inputs, input_sha, require_approved=False, run_spec=run_spec)
        budget_ok = True
    except BatchError as exc:
        budget_code = exc.code
    return {
        "mode": "dry-run",
        "contract_version": CONTRACT,
        "input_path": str(Path(inputs_path)),
        "input_sha256": input_sha,
        "dataset_sha256": inputs["dataset_sha256"],
        "budget_path": str(Path(budget_path)),
        "budget_status": budget.get("status") if isinstance(budget, dict) else None,
        "budget_binding_ok": budget_ok,
        "budget_error": budget_code,
        "planned_cases": [item["case_id"] for item in inputs["cases"]],
        "planned_count": 12,
        "network_dispatches": 0,
        "commits": 0,
        "result_directory_created": False,
    }


def _atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp-%s" % uuid.uuid4().hex)
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temp, path)


def _ledger_payload(run_id, cases, updated):
    return {
        "contract_version": CONTRACT,
        "run_id": run_id,
        "cases": cases,
        "updated_at": updated,
    }


def _lock(path):
    token = uuid.uuid4().hex
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, token.encode("ascii"))
        os.close(fd)
    except FileExistsError:
        raise BatchError("BATCH_LOCKED", "another batch owns the result directory")
    return token


def _unlock(path, token):
    try:
        if Path(path).read_text(encoding="ascii") == token:
            Path(path).unlink()
    except OSError:
        pass


def _run_one(case, *, model_factory, env, expected_provider=None, expected_model=None):
    model = model_factory(case["case_id"]) if model_factory is not None else None
    return run_intake_request(
        case["text"],
        reference_time="2026-11-04T12:00:00+08:00",
        mode="real",
        model_client=model,
        env=env,
        expected_provider=expected_provider,
        expected_model=expected_model,
    )


def _finalize_incomplete(manifest, manifest_path, results_path, ledger_path, observed, reason):
    try:
        if manifest is None:
            return False
        manifest.update(
            {
                "observed_count": observed,
                "state": "incomplete",
                "stop_reason": reason,
                "finished_at": time.time(),
                "results_sha256": _sha_file(results_path) if Path(results_path).exists() else None,
                "ledger_sha256": _sha_file(ledger_path) if Path(ledger_path).exists() else None,
            }
        )
        _atomic_json(manifest_path, manifest)
        return True
    except Exception:
        return False


def run_batch(
    inputs_path=DEFAULT_INPUTS,
    budget_path=DEFAULT_BUDGET,
    *,
    execute=False,
    model_factory=None,
    env=None,
    result_dir=None,
    run_spec="001",
):
    inputs, input_sha = load_inputs(inputs_path)
    budget = _load_json(budget_path)
    if not execute:
        return dry_run(inputs_path, budget_path, run_spec=run_spec)

    spec = _spec(run_spec)
    _budget(budget, inputs, input_sha, require_approved=True, run_spec=run_spec)
    preflight = None
    if model_factory is None:
        preflight = _preflight_real_config(budget, env)
    # The CLI ignores budget-controlled locations; only the explicit spec path is used.
    target = Path(result_dir) if result_dir is not None else WORKSPACE / spec["result_rel"]
    if not target.is_absolute():
        target = WORKSPACE / target
    target.mkdir(parents=True, exist_ok=True)
    lock_path = target / ".lock"
    ledger_path = target / "session-ledger.json"
    results_path = target / "results.jsonl"
    manifest_path = target / "manifest.json"
    lock_token = _lock(lock_path)
    manifest = None
    observed = 0
    current_case_id = None
    current_persisted = False
    try:
        if ledger_path.exists() or results_path.exists() or manifest_path.exists():
            raise BatchError("BATCH_STATE_EXISTS", "reserved/observed state already exists")
        ledger = _ledger_payload(spec["run_id"], {}, time.time())
        _atomic_json(ledger_path, ledger)
        source_sha = _sha_source()
        manifest = {
            "contract_version": CONTRACT,
            "run_id": spec["run_id"],
            "config_id": budget["config_id"],
            "provider": preflight["provider"] if preflight else budget["provider"],
            "model": preflight["model"] if preflight else budget["model"],
            "dataset_sha256": inputs["dataset_sha256"],
            "input_sha256": input_sha,
            "budget_sha256": _sha_file(budget_path),
            "parent_budget_sha256": budget.get("parent_budget_sha256"),
            "source_sha256": source_sha,
            "planned_count": 12,
            "observed_count": 0,
            "max_requests": budget["max_requests"],
            "max_client_calls": budget["max_client_calls"],
            "state": "running",
            "stop_reason": None,
            "results_sha256": None,
            "ledger_sha256": None,
        }
        _atomic_json(manifest_path, manifest)
        results_path.write_text("", encoding="utf-8")
        stop_reason = None
        started = time.time()
        for case in inputs["cases"]:
            case_id = case["case_id"]
            current_case_id = case_id
            ledger["cases"][case_id] = {
                "state": "reserved",
                "reserved_at": time.time(),
                "observed_at": None,
                "client_calls": None,
            }
            _atomic_json(ledger_path, ledger)
            current_persisted = False
            result = _run_one(
                case,
                model_factory=model_factory,
                env=env,
                expected_provider=budget["provider"],
                expected_model=budget["model"],
            )
            row = {
                "case_id": case_id,
                "draft": result["draft"],
                "run": result["run"],
            }
            with results_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            ledger["cases"][case_id].update(
                {
                    "state": "observed",
                    "observed_at": time.time(),
                    "client_calls": result["run"].get("client_calls"),
                }
            )
            _atomic_json(ledger_path, ledger)
            observed += 1
            current_persisted = True
            if result["run"].get("error_code") in STOP_CODES:
                stop_reason = result["run"]["error_code"]
                break
        manifest.update(
            {
                "observed_count": observed,
                "state": "completed" if observed == 12 else "incomplete",
                "stop_reason": stop_reason,
                "started_at": started,
                "finished_at": time.time(),
                "results_sha256": _sha_file(results_path),
                "ledger_sha256": _sha_file(ledger_path),
            }
        )
        _atomic_json(manifest_path, manifest)
        return manifest
    except BatchError:
        if manifest is not None:
            _finalize_incomplete(
                manifest,
                manifest_path,
                results_path,
                ledger_path,
                observed,
                "BATCH_INTERRUPTED",
            )
        raise
    except BaseException:
        try:
            if current_case_id is not None and not current_persisted:
                ledger["cases"][current_case_id]["state"] = "reserved"
                ledger["cases"][current_case_id]["observed_at"] = None
                ledger["cases"][current_case_id]["client_calls"] = None
                _atomic_json(ledger_path, ledger)
        except Exception:
            pass
        if manifest is not None:
            _finalize_incomplete(
                manifest,
                manifest_path,
                results_path,
                ledger_path,
                observed,
                "BATCH_INTERRUPTED",
            )
        raise BatchError("BATCH_INTERRUPTED", "batch was interrupted")
    finally:
        _unlock(lock_path, lock_token)


def main(argv=None):
    parser = argparse.ArgumentParser(description="M5 fixed 12-case batch")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--execute", action="store_true")
    parser.add_argument("--inputs", default=str(DEFAULT_INPUTS))
    parser.add_argument("--budget", default=str(DEFAULT_BUDGET))
    parser.add_argument("--run-spec", choices=tuple(RUN_SPECS), default="001")
    args = parser.parse_args(argv)
    try:
        if args.execute:
            manifest = run_batch(
                args.inputs,
                args.budget,
                execute=True,
                run_spec=args.run_spec,
            )
            print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
            return 0
        plan = dry_run(args.inputs, args.budget, run_spec=args.run_spec)
        print(json.dumps(plan, ensure_ascii=False, sort_keys=True))
        return 0
    except BatchError as exc:
        print(json.dumps({"error": exc.code}, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    sys.exit(main())
