"""C-010 public CLI check.

Runs the public intake CLI in a fresh temporary directory using only the
project fixture and the C evaluation modules. It verifies:

1. export: 12 case_id/text entries, no answers, frozen dataset/ref/time;
2. pending: live state pending_live, null rates, real_quality false;
3. test_double replay: complete 12/6/30/30/0, source test_double,
   real_quality false.

No network, model, or business database calls are made. The only subprocesses
are ``python -B -m evaluation.intake_live_eval`` from the project root (or a
clean candidate copy of it).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evaluation import intake_eval as ev  # noqa: E402
from evaluation import intake_live_eval as live  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "cases-a07-intake-v1.json"


class CheckFailure(Exception):
    pass


def _run(args: list[str], workdir: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [sys.executable, "-B", "-m", "evaluation.intake_live_eval", *args]
    return subprocess.run(
        command,
        cwd=str(workdir),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _dump(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def _case_texts(fixture: dict) -> dict:
    return {case["id"]: case["text"] for case in fixture["cases"]}


def build_replay(tmp: Path, fixture: dict, input_path: Path) -> dict:
    """Construct a clearly labelled test_double contract replay."""
    case = {item["id"]: item for item in fixture["cases"]}
    drafts = ev.scripted_drafts(fixture["cases"])
    rows = []
    for draft in drafts:
        rows.append(
            {
                "case_id": draft["case_id"],
                "draft": draft,
                "run": {
                    "mode": "real",
                    "execution_source": "test_double",
                    "provider": None,
                    "model": None,
                    "client_calls": 1,
                    "elapsed_ms": 1,
                    "usage": {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None},
                    "error_code": None,
                },
            }
        )
    results_path = tmp / "results.jsonl"
    results_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    ledger = {
        "contract_version": live.LIVE_CONTRACT,
        "run_id": live.RUN_ID,
        "cases": [
            {"case_id": case_id, "reserved": True, "observed": True, "client_calls": 1}
            for case_id in live.CASE_IDS
        ],
    }
    ledger_path = tmp / "ledger.json"
    _dump(ledger_path, ledger)
    input_sha = _sha(input_path.read_bytes())
    # No approved budget/source reference is supplied: certification stays
    # unverified, while test_double metrics remain valid as contract evidence.
    manifest = {
        "contract_version": live.LIVE_CONTRACT,
        "run_id": live.RUN_ID,
        "config_id": live.CONFIG_ID,
        "provider": live.PROVIDER,
        "model": live.MODEL,
        "state": "complete",
        "stop_reason": None,
        "planned": 12,
        "observed": 12,
        "source_sha256": "d" * 64,
        "dataset_sha256": live.LIVE_DATASET_SHA,
        "input_sha256": input_sha,
        "results_sha256": _sha(results_path.read_bytes()),
        "ledger_sha256": _sha(ledger_path.read_bytes()),
    }
    manifest_path = tmp / "manifest.json"
    _dump(manifest_path, manifest)
    return {"manifest": manifest_path, "ledger": ledger_path, "results": results_path}


def main() -> int:
    report = {"tool": "check_intake_public_cli", "checks": {}, "state": "running"}
    try:
        fixture = ev.load_fixture(FIXTURE)
        fixture_bytes_sha = _sha(FIXTURE.read_bytes())
        _check(fixture_bytes_sha == live.LIVE_DATASET_SHA, "fixture SHA mismatch")
        _check(len(fixture["cases"]) == 12, "fixture case count")
        _check([case["id"] for case in fixture["cases"]] == list(live.CASE_IDS), "fixture ids")
        texts = _case_texts(fixture)
        report["checks"]["fixture"] = "ok: 12 cases, sha %s" % fixture_bytes_sha
        with tempfile.TemporaryDirectory(prefix="c010-public-cli-") as temporary:
            tmp = Path(temporary)
            input_path = tmp / "live-inputs.json"

            proc = _run(["--export-inputs", "--inputs", str(input_path)], ROOT)
            _check(proc.returncode == 0, "export exit %d: %s" % (proc.returncode, proc.stderr[-300:]))
            _check(input_path.is_file(), "export file missing")
            payload = json.loads(input_path.read_text(encoding="utf-8"))
            _check(set(payload) == {"contract_version", "dataset_sha256", "reference_time", "timezone", "cases"}, "export top-level keys")
            _check(payload["contract_version"] == live.LIVE_CONTRACT, "export contract")
            _check(payload["dataset_sha256"] == live.LIVE_DATASET_SHA, "export dataset SHA")
            _check(payload["reference_time"] == live.REFERENCE_TIME, "export reference_time")
            _check(payload["timezone"] == live.TIMEZONE, "export timezone")
            _check(len(payload["cases"]) == 12, "export case count")
            _check([item["case_id"] for item in payload["cases"]] == list(live.CASE_IDS), "export ids")
            for item in payload["cases"]:
                _check(set(item) == {"case_id", "text"}, "export case fields")
                _check(item["text"] == texts[item["case_id"]], "export text changed")
                _check("expected" not in item and "answer" not in item, "export contains answer")
            report["checks"]["export"] = "ok: 12 cases, no answers, sha %s" % _sha(input_path.read_bytes())

            pending_dir = tmp / "pending"
            proc = _run(["--inputs", str(input_path), "--output-dir", str(pending_dir)], ROOT)
            _check(proc.returncode == 0, "pending exit %d: %s" % (proc.returncode, proc.stderr[-300:]))
            summary = json.loads((pending_dir / "live-summary.json").read_text(encoding="utf-8"))
            _check(summary["live"]["state"] == "pending_live", "pending live state")
            _check(summary["live"]["rates"] is None, "pending rates must be null")
            _check(summary["live"]["real_quality"] is False, "pending real_quality")
            _check(summary["live"]["coverage"]["observed"] == 0, "pending coverage")
            _check(summary["rule"]["case_pass"] == 2 and summary["scripted"]["case_pass"] == 12, "pending rule/script counts")
            report["checks"]["pending"] = "ok: pending_live, rates null, real_quality false"

            replay = build_replay(tmp, fixture, input_path)
            replay_dir = tmp / "replay-out"
            proc = _run(
                [
                    "--inputs", str(input_path),
                    "--manifest", str(replay["manifest"]),
                    "--ledger", str(replay["ledger"]),
                    "--results", str(replay["results"]),
                    "--output-dir", str(replay_dir),
                ],
                ROOT,
            )
            _check(proc.returncode == 0, "replay exit %d: %s" % (proc.returncode, proc.stderr[-300:]))
            summary = json.loads((replay_dir / "live-summary.json").read_text(encoding="utf-8"))
            item = summary["live"]
            _check(item["state"] == "complete", "replay state")
            _check(item["source"] == "test_double", "replay source")
            _check(item["test_double"] is True, "replay test_double flag")
            _check(item["real_quality"] is False, "replay real_quality")
            _check(item["coverage"]["observed"] == 12, "replay coverage")
            _check(item["case_pass"] == 12, "replay case_pass")
            _check(item["record_exact"] == 6, "replay record")
            _check(item["field_exact"] == 30, "replay fields")
            _check(item["evidence_valid"] == 30, "replay evidence")
            _check(item["error_ready"] == 0, "replay error_ready")
            _check(item["client_calls"] == 12, "replay client_calls")
            _check(item["certification"] == "unverified", "replay certification")
            _check(item["rates"] is not None, "replay rates")
            report["checks"]["replay"] = "ok: test_double 12/6/30/30/0, real_quality false, certification unverified"
        report["state"] = "ok"
        report["temp"] = "temporary directory cleaned"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (CheckFailure, OSError, ValueError, KeyError) as exc:
        report["state"] = "failed"
        report["error"] = str(exc)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
