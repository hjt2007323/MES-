"""C-007 M3 quality CLI: offline dataset judgement and read-only pilot replay."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from evaluation import quality_reporting as reporting
from evaluation import quality_runner as qr

OFFLINE_DIR = qr.OUTPUT_ROOT / "offline"
PILOT_DIR = qr.OUTPUT_ROOT / "pilot"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="C-007 M3 quality evaluation")
    parser.add_argument("--mode", choices=("offline", "replay"), required=True)
    return parser


def _offline() -> int:
    doc = qr.load_dataset()
    budget = qr.load_budget()
    validation = qr.validate_dataset(
        doc,
        source_bytes=qr.DATASET_PATH.read_bytes(),
        budget=budget,
        a_fixture=qr.ROOT / "projects" / "A-mes-assistant" / "db" / "fixture-boundary.json",
    )
    records = qr.judge_local_dataset(doc)
    summary = reporting.write_offline_report(OFFLINE_DIR, doc, validation, records)
    if not validation["ok"]:
        print("ERROR invalid_dataset", file=sys.stderr)
        return 2
    if summary["local_fail"]:
        print("ERROR local_quality_fail", file=sys.stderr)
        return 1
    print(
        "offline local_examples=%d pass=%d pilot_executed=%d total_rate=null"
        % (summary["local_examples"], summary["local_pass"], summary["pilot_executed"])
    )
    return 0


def _replay() -> int:
    doc = qr.load_dataset()
    budget = qr.load_budget()
    try:
        experiment = qr.load_experiment(dataset=doc, budget=budget)
    except qr.ReplayError as exc:
        print("ERROR invalid_experiment", file=sys.stderr)
        return 2
    summary = reporting.write_pilot_report(PILOT_DIR, doc, experiment)
    print(
        "replay state=%s full_executed=%s focused_executed=%s total_rate=null"
        % (
            summary.get("experiment_state", "missing"),
            summary["strategies"]["full"]["executed"],
            summary["strategies"]["focused"]["executed"],
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit:
        return 2
    try:
        if args.mode == "offline":
            return _offline()
        return _replay()
    except (OSError, ValueError, KeyError, TypeError):
        print("ERROR invalid_input", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
