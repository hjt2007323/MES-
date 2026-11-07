"""A2/A3 workspace CLI: route one synthetic request without touching MySQL."""

from __future__ import annotations

import argparse
import json
import sys

from mes_assistant.workspace_assistant import WorkspaceError, WorkspaceService


def main(argv=None):
    parser = argparse.ArgumentParser(description="A07 v2 workspace offline route")
    parser.add_argument("--question", required=True)
    parser.add_argument("--session-id", default="cli-session")
    parser.add_argument("--request-id", default="cli-request")
    parser.add_argument("--reference-time", default="2026-11-04T12:00:00+08:00")
    parser.add_argument("--mode", choices=("offline", "real"), default="offline")
    parser.add_argument("--last-record", type=int, default=None)
    args = parser.parse_args(argv)
    request = {
        "request_id": args.request_id,
        "session_id": args.session_id,
        "question": args.question,
        "reference_time": args.reference_time,
        "timezone": "Asia/Shanghai",
        "mode": args.mode,
        "last_confirmed_record_id": args.last_record,
    }
    try:
        result = WorkspaceService().run(request)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("status") == "success" else 2
    except WorkspaceError as exc:
        print(json.dumps({"error": exc.code}, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    sys.exit(main())
