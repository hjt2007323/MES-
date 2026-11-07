"""Controlled named-query tools for the A07/MES boundary dataset (B-004 工作包3).

This module is the only data access entry for A: five fixed ``query_id`` values,
no arbitrary SQL, no caller-supplied host/port/database/username.  It talks to
the dedicated MySQL instance created by ``db/mysql_instance.py`` with the
read-only account only.

``run_named_query`` returns an internal tool DTO (not a public QA schema):

    query_id, data_version, status, table, sql, sql_sha256,
    error, counts{queries}, timing{internal_ms}, cleanup[]

Limits: at most two SELECTs are submitted per call (one identity handshake and
one named query).  No business retries.  Timeouts and oversized output go
through the approved D2 cleanup: KILL the handshake connection id on a separate
ro connection, confirm the id disappeared via a separate root diagnostic query,
then terminate the client process.  KILL return or client exit alone is not
treated as proof that the server side stopped.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

__all__ = ["get_query_catalog", "run_named_query", "describe_schema"]

BASE_DIR = Path(__file__).resolve().parents[1]
PROJECTS_DIR = BASE_DIR.parent
RUNTIME_DIR = PROJECTS_DIR.parent / "runtime" / "mysql-a07"
SECRETS_DIR = RUNTIME_DIR / "secrets"
RUN_DIR = RUNTIME_DIR / "run"
TMP_DIR = RUNTIME_DIR / "tmp"
DATA_DIR = RUNTIME_DIR / "data"
DIST_DIR = RUNTIME_DIR / "dist"
INSTANCE_RECORD = RUN_DIR / "instance.json"
RO_CNF = SECRETS_DIR / "ro.cnf"
ROOT_CNF = SECRETS_DIR / "root.cnf"
MYSQL_CLIENT = DIST_DIR / "bin" / "mysql.exe"
MANIFEST_PATH = BASE_DIR / "db" / "fixture-manifest.json"
SCHEMA_PATH = BASE_DIR / "db" / "schema.sql"

DATABASE = "mes_assistant_demo_v1"
PORT = 13317
MAX_EFFECTIVE_MS = 3000
MAX_ROWS_LIMIT = 200
OUTPUT_LIMIT = 1024 * 1024
CLEANUP_GRACE_MS = 1000

STATUS_OK = "ok"
STATUS_REFUSED = "refused"
STATUS_ERROR = "error"
STATUS_TIMEOUT = "timeout"

CODE_REFUSED = "QUERY_REFUSED"
CODE_CLIENT = "DB_CLIENT_ERROR"
CODE_TIMEOUT = "DB_TIMEOUT"
CODE_RESULT_INVALID = "DB_RESULT_INVALID"
CODE_INSTANCE_MISMATCH = "DB_INSTANCE_MISMATCH"
CODE_OUTPUT_LIMIT = "DB_OUTPUT_LIMIT"
CODE_VERSION_MISMATCH = "DB_DATA_VERSION_MISMATCH"
CODE_CLEANUP_UNCONFIRMED = "DB_CLEANUP_UNCONFIRMED"

_TABLE_NAMES = ("line", "product", "machine", "work_order", "downtime")
_TABLE_IN_LIST = ", ".join("'" + name + "'" for name in _TABLE_NAMES)

_DATETIME_PARAM_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00\Z")
_MYSQL_DATETIME_RE = re.compile(
    r"\A(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(\.\d{1,6})?\Z"
)

_QUERY_DEFS = {
    "line_snapshot": {
        "params": {},
        "columns": [
            ("line_no", "string", False),
            ("actual_qty", "decimal", False),
        ],
        "business": True,
    },
    "work_order_progress": {
        "params": {"line_id": {"type": "integer", "required": True}},
        "columns": [
            ("order_no", "string", False),
            ("line_no", "string", False),
            ("plan_qty", "integer", False),
            ("actual_qty", "integer", False),
            ("quantity_ratio", "decimal", True),
            ("status", "string", False),
        ],
        "business": True,
    },
    "downtime_details": {
        "params": {
            "window_start": {"type": "string", "required": True},
            "window_end": {"type": "string", "required": True},
        },
        "columns": [
            ("downtime_id", "integer", False),
            ("machine_no", "string", False),
            ("start_time", "datetime", False),
            ("end_time", "datetime", True),
            ("reason", "string", False),
            ("order_id", "string", True),
        ],
        "business": True,
    },
    "schema_columns": {
        "params": {},
        "columns": [
            ("table_name", "string", False),
            ("column_name", "string", False),
            ("column_type", "string", False),
            ("nullable", "boolean", False),
            ("column_default", "string", True),
            ("ordinal_position", "integer", False),
        ],
        "business": False,
    },
    "schema_relations": {
        "params": {},
        "columns": [
            ("table_name", "string", False),
            ("column_name", "string", False),
            ("referenced_table_schema", "string", False),
            ("referenced_table_name", "string", False),
            ("referenced_column_name", "string", False),
            ("constraint_name", "string", False),
        ],
        "business": False,
    },
}


# --------------------------------------------------------------------------
# catalog
# --------------------------------------------------------------------------
def get_query_catalog() -> list:
    """Return the fixed catalog as fresh objects (five query ids only)."""
    catalog = []
    for query_id, definition in _QUERY_DEFS.items():
        item = {
            "query_id": query_id,
            "params": {
                name: {"type": spec["type"], "required": spec["required"]}
                for name, spec in definition["params"].items()
            },
            "columns": [
                {"name": name, "type": column_type, "nullable": nullable}
                for name, column_type, nullable in definition["columns"]
            ],
        }
        if definition["business"]:
            item["review_status"] = "pending"
        catalog.append(item)
    return catalog


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
class _SessionTimeout(Exception):
    pass


class _SessionClosed(Exception):
    pass


class _SessionOverflow(Exception):
    pass


def _now_ns() -> int:
    """Injectable monotonic clock; tests replace it with a controlled fake."""
    return time.monotonic_ns()


def _now_ms_since(start_ns: int) -> float:
    return round((_now_ns() - start_ns) / 1_000_000, 3)


def _remaining_ns(deadline_ns: int) -> int:
    return deadline_ns - _now_ns()


def _remaining_ms(deadline_ns: int) -> int:
    """Milliseconds left; may be 0 or negative and is never clamped for decisions."""
    return int(_remaining_ns(deadline_ns) / 1_000_000)


def _has_time(deadline_ns: int) -> bool:
    return _now_ns() < deadline_ns


def _hint_ms(deadline_ns: int) -> int:
    """MAX_EXECUTION_TIME needs >=1; only used when a dispatch was decided."""
    return max(1, _remaining_ms(deadline_ns))


def _normalized_path(value) -> str:
    if not isinstance(value, str):
        return ""
    return value.replace("\\", "/").rstrip("/").casefold()


def _load_json_object(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _load_instance_record():
    return _load_json_object(INSTANCE_RECORD)


def _load_manifest():
    return _load_json_object(MANIFEST_PATH)


def _file_sha256(path: Path):
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _version_is_consistent(instance: dict, manifest: dict) -> bool:
    schema_sha = _file_sha256(SCHEMA_PATH)
    return (
        schema_sha is not None
        and instance.get("data_version") == manifest.get("data_version")
        and instance.get("schema_sha256") == manifest.get("schema_sha256") == schema_sha
        and instance.get("fixture_sha256") == manifest.get("fixture_sha256")
    )


def _error(code: str, message: str) -> dict:
    return {"code": code, "stage": "query", "message": message, "retryable": False}


def _make_dto(*, query_id, data_version, status, table=None, sql=None,
              queries=0, error=None, started_ns=None, cleanup=None) -> dict:
    return {
        "query_id": query_id,
        "data_version": data_version,
        "status": status,
        "table": table,
        "sql": sql,
        "sql_sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest() if sql else None,
        "error": error,
        "counts": {"queries": queries},
        "timing": {"internal_ms": _now_ms_since(started_ns) if started_ns else 0.0},
        "cleanup": cleanup or [],
    }


# --------------------------------------------------------------------------
# parameter validation
# --------------------------------------------------------------------------
def _validate_line_id(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("line_id must be an integer")
    if value < 1 or value > 2147483647:
        raise ValueError("line_id is outside 1..2147483647")
    return value


def _validate_window(value) -> str:
    if not isinstance(value, str) or not _DATETIME_PARAM_RE.match(value):
        raise ValueError("window time must be YYYY-MM-DDTHH:MM:SS+08:00")
    parsed = _dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S+08:00")
    return parsed


def _validate_params(query_id: str, params) -> dict:
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    expected = _QUERY_DEFS[query_id]["params"]
    if set(params) != set(expected):
        raise ValueError("params keys do not match the query contract")

    if query_id == "work_order_progress":
        return {"line_id": _validate_line_id(params["line_id"])}
    if query_id == "downtime_details":
        start = _validate_window(params["window_start"])
        end = _validate_window(params["window_end"])
        if start >= end:
            raise ValueError("window_start must be before window_end")
        return {"window_start": start, "window_end": end}
    return {}


# --------------------------------------------------------------------------
# SQL builders (fixed text; only validated values are interpolated)
# --------------------------------------------------------------------------
def _hint(remaining_ms: int) -> str:
    return "/*+ MAX_EXECUTION_TIME(%d) */" % max(1, int(remaining_ms))


def _handshake_sql(remaining_ms: int) -> str:
    return (
        "SELECT %s JSON_OBJECT("
        "'connection_id', CONNECTION_ID(), "
        "'port', @@port, "
        "'datadir', @@datadir, "
        "'server_uuid', @@server_uuid);" % _hint(remaining_ms)
    )


def _line_snapshot_sql(remaining_ms: int, limit: int) -> str:
    return (
        "SELECT %s JSON_ARRAY(\n"
        "  l.line_no,\n"
        "  CAST(COALESCE(SUM(w.actual_qty), 0) AS CHAR)\n"
        ")\n"
        "FROM line AS l\n"
        "LEFT JOIN work_order AS w ON w.line_id = l.id\n"
        "GROUP BY l.id, l.line_no\n"
        "ORDER BY l.id\n"
        "LIMIT %d;" % (_hint(remaining_ms), limit)
    )


def _work_order_progress_sql(remaining_ms: int, limit: int, line_id: int) -> str:
    return (
        "SELECT %s JSON_ARRAY(\n"
        "  w.order_no,\n"
        "  l.line_no,\n"
        "  w.plan_qty,\n"
        "  w.actual_qty,\n"
        "  CAST(w.actual_qty / NULLIF(w.plan_qty, 0) AS CHAR),\n"
        "  w.status\n"
        ")\n"
        "FROM work_order AS w\n"
        "JOIN line AS l ON l.id = w.line_id\n"
        "WHERE w.line_id = %d\n"
        "ORDER BY w.id\n"
        "LIMIT %d;" % (_hint(remaining_ms), line_id, limit)
    )


def _downtime_details_sql(remaining_ms: int, limit: int, start: _dt.datetime, end: _dt.datetime) -> str:
    start_txt = start.strftime("%Y-%m-%d %H:%M:%S")
    end_txt = end.strftime("%Y-%m-%d %H:%M:%S")
    return (
        "SELECT %s JSON_ARRAY(\n"
        "  d.id,\n"
        "  m.machine_no,\n"
        "  d.start_time,\n"
        "  d.end_time,\n"
        "  d.reason,\n"
        "  d.order_id\n"
        ")\n"
        "FROM downtime AS d\n"
        "JOIN machine AS m ON m.id = d.machine_id\n"
        "WHERE d.start_time < '%s' "
        "AND (d.end_time IS NULL OR d.end_time > '%s')\n"
        "ORDER BY d.id\n"
        "LIMIT %d;" % (_hint(remaining_ms), end_txt, start_txt, limit)
    )


def _schema_columns_sql(remaining_ms: int, limit: int) -> str:
    return (
        "SELECT %s JSON_ARRAY(\n"
        "  TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, ORDINAL_POSITION\n"
        ")\n"
        "FROM INFORMATION_SCHEMA.COLUMNS\n"
        "WHERE TABLE_SCHEMA = DATABASE()\n"
        "  AND TABLE_NAME IN (%s)\n"
        "ORDER BY TABLE_NAME, ORDINAL_POSITION\n"
        "LIMIT %d;" % (_hint(remaining_ms), _TABLE_IN_LIST, limit)
    )


def _schema_relations_sql(remaining_ms: int, limit: int) -> str:
    return (
        "SELECT %s JSON_ARRAY(\n"
        "  k.TABLE_NAME, k.COLUMN_NAME, k.REFERENCED_TABLE_SCHEMA,\n"
        "  k.REFERENCED_TABLE_NAME, k.REFERENCED_COLUMN_NAME, k.CONSTRAINT_NAME\n"
        ")\n"
        "FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE AS k\n"
        "WHERE k.TABLE_SCHEMA = DATABASE()\n"
        "  AND k.TABLE_NAME IN (%s)\n"
        "  AND k.REFERENCED_TABLE_SCHEMA = DATABASE()\n"
        "  AND k.REFERENCED_TABLE_NAME IN (%s)\n"
        "ORDER BY k.TABLE_NAME, k.CONSTRAINT_NAME, k.ORDINAL_POSITION\n"
        "LIMIT %d;" % (_hint(remaining_ms), _TABLE_IN_LIST, _TABLE_IN_LIST, limit)
    )


def _build_query_sql(query_id: str, params: dict, remaining_ms: int, limit: int) -> str:
    if query_id == "line_snapshot":
        return _line_snapshot_sql(remaining_ms, limit)
    if query_id == "work_order_progress":
        return _work_order_progress_sql(remaining_ms, limit, params["line_id"])
    if query_id == "downtime_details":
        return _downtime_details_sql(
            remaining_ms, limit, params["window_start"], params["window_end"]
        )
    if query_id == "schema_columns":
        return _schema_columns_sql(remaining_ms, limit)
    return _schema_relations_sql(remaining_ms, limit)


# --------------------------------------------------------------------------
# client session
# --------------------------------------------------------------------------
def _client_argv(config_path: Path) -> list:
    return [
        str(MYSQL_CLIENT),
        "--defaults-file=%s" % config_path,
        "--batch",
        "--raw",
        "--skip-column-names",
        "--unbuffered",
        "--local-infile=0",
        "--skip-reconnect",
        "--connect-timeout=2",
        "--default-character-set=utf8mb4",
    ]


def _client_env() -> dict:
    env = dict(os.environ)
    env.pop("MYSQL_PWD", None)
    env["MYSQL_TEST_LOGIN_FILE"] = str(SECRETS_DIR / "__no_login_file")
    if TMP_DIR.is_dir():
        env["TMP"] = str(TMP_DIR)
        env["TEMP"] = str(TMP_DIR)
    return env


class _Session:
    """One mysql client process with bounded stdout/stderr readers."""

    def __init__(self, config_path: Path):
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(
            _client_argv(config_path),
            cwd=str(RUNTIME_DIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_client_env(),
            shell=False,
            creationflags=creation_flags,
        )
        self._condition = threading.Condition()
        self._out = bytearray()
        self._err = bytearray()
        self._out_eof = False
        self._err_eof = False
        self._overflow = False
        self._stderr_overflow = False
        self._threads = [
            threading.Thread(target=self._pump, args=(self.proc.stdout, self._out, "out"), daemon=True),
            threading.Thread(target=self._pump, args=(self.proc.stderr, self._err, "err"), daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def _pump(self, stream, buffer: bytearray, kind: str):
        limit = OUTPUT_LIMIT
        # ``BufferedReader.read(n)`` waits for n bytes or EOF; read1 returns
        # whatever is available, which is required for the interactive handshake.
        reader = getattr(stream, "read1", stream.read)
        try:
            while True:
                chunk = reader(65536)
                if not chunk:
                    break
                with self._condition:
                    if len(buffer) < limit:
                        buffer.extend(chunk[: limit - len(buffer)])
                    if kind == "out" and len(buffer) >= limit:
                        self._overflow = True
                    if kind == "err" and len(buffer) >= limit:
                        self._stderr_overflow = True
                    self._condition.notify_all()
        except (OSError, ValueError):
            pass
        finally:
            with self._condition:
                if kind == "out":
                    self._out_eof = True
                else:
                    self._err_eof = True
                self._condition.notify_all()

    def send(self, text: str) -> None:
        if self.proc.stdin is None:
            raise _SessionClosed()
        try:
            self.proc.stdin.write(text.encode("utf-8") + b"\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise _SessionClosed() from exc

    def close_stdin(self) -> None:
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def read_line(self, deadline_ns: int) -> str:
        with self._condition:
            while True:
                index = self._out.find(b"\n")
                if index >= 0:
                    line = bytes(self._out[:index])
                    del self._out[: index + 1]
                    return self._decode_line(line)
                if self._overflow:
                    raise _SessionOverflow()
                if self._out_eof and not self._out:
                    raise _SessionClosed()
                if self._out_eof:
                    line = bytes(self._out)
                    self._out.clear()
                    return self._decode_line(line)
                remaining = (deadline_ns - time.monotonic_ns()) / 1_000_000_000
                if remaining <= 0:
                    raise _SessionTimeout()
                self._condition.wait(min(remaining, 0.05))

    @staticmethod
    def _decode_line(line: bytes) -> str:
        text = line.decode("utf-8", errors="replace")
        return text[:-1] if text.endswith("\r") else text

    def wait_exit(self, deadline_ns: int):
        while True:
            code = self.proc.poll()
            if code is not None:
                return code
            if time.monotonic_ns() >= deadline_ns:
                raise _SessionTimeout()
            time.sleep(0.02)

    def is_alive(self) -> bool:
        return self.proc.poll() is None

    def stderr_text(self) -> str:
        with self._condition:
            return bytes(self._err).decode("utf-8", errors="replace")

    def terminate(self, deadline_ns=None) -> bool:
        if self.proc.poll() is not None:
            return True
        try:
            self.proc.terminate()
        except OSError:
            pass
        end = deadline_ns if deadline_ns is not None else time.monotonic_ns() + 2_000_000_000
        while time.monotonic_ns() < end:
            if self.proc.poll() is not None:
                return True
            time.sleep(0.01)
        if self.proc.poll() is None:
            try:
                self.proc.kill()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                return False
        return self.proc.poll() is not None

    def close(self) -> None:
        self.close_stdin()
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass


def _open_session(config_path: Path) -> _Session:
    return _Session(config_path)


#: Tests replace this factory; production always talks to the real client.
_SESSION_FACTORY = _open_session


def _run_simple_sql(config_path: Path, sql: str, deadline_ns: int) -> dict:
    """Run one short fixed statement on a fresh connection (cleanup/diagnostic)."""
    session = None
    started = time.monotonic_ns()
    try:
        session = _SESSION_FACTORY(config_path)
        session.send(sql)
        session.close_stdin()
        lines = []
        try:
            while True:
                lines.append(session.read_line(deadline_ns))
        except _SessionClosed:
            pass
        code = session.wait_exit(deadline_ns)
        return {
            "exit_code": code,
            "stdout": "\n".join(lines),
            "stderr": session.stderr_text(),
            "timed_out": False,
            "duration_ms": round((time.monotonic_ns() - started) / 1_000_000, 3),
        }
    except _SessionTimeout:
        return {"exit_code": None, "stdout": "", "stderr": "", "timed_out": True,
                "duration_ms": round((time.monotonic_ns() - started) / 1_000_000, 3)}
    except (_SessionClosed, OSError, ValueError):
        return {"exit_code": None, "stdout": "", "stderr": "", "timed_out": False,
                "duration_ms": round((time.monotonic_ns() - started) / 1_000_000, 3)}
    finally:
        if session is not None:
            session.terminate(deadline_ns=deadline_ns)
            session.close()


def _new_grace_deadline() -> int:
    """One grace window per interrupted call; phases never restart it."""
    return _now_ns() + CLEANUP_GRACE_MS * 1_000_000


def _confirm_connection_gone(connection_id, *, grace_deadline_ns: int) -> dict:
    """Ask the server (via a separate root diagnostic) whether the id disappeared."""
    if connection_id is None:
        return {"confirmed": False, "attempts": 0, "last_count": None}
    attempts = 0
    last_count = None
    while _now_ns() < grace_deadline_ns:
        attempts += 1
        sql = (
            "SELECT COUNT(*) FROM performance_schema.threads "
            "WHERE PROCESSLIST_ID = %d;" % int(connection_id)
        )
        result = _run_simple_sql(ROOT_CNF, sql, grace_deadline_ns)
        last_count = result["stdout"].strip()
        if result["exit_code"] == 0 and last_count == "0":
            return {"confirmed": True, "attempts": attempts, "last_count": last_count}
        time.sleep(0.05)
    return {"confirmed": False, "attempts": attempts, "last_count": last_count}


def _terminate_client(session, grace_deadline_ns: int) -> dict:
    started = _now_ns()
    confirmed = session.terminate(deadline_ns=grace_deadline_ns) if session is not None else False
    finished = _now_ns()
    return {
        "confirmed": bool(confirmed),
        "within_grace": finished <= grace_deadline_ns,
        "elapsed_ms": round((finished - started) / 1_000_000, 3),
    }


def _process_cleanup_entry(term: dict) -> dict:
    refs = [
        "client process handle polled",
        "reclaim_elapsed_ms=%.3f" % term["elapsed_ms"],
    ]
    if not term["within_grace"]:
        refs.append("local_reclaim_exceeded_cleanup_grace")
    return {
        "resource_type": "adapter_process",
        "cancel_requested": True,
        "observable_boundary": "local Popen handle (mysql client)",
        "termination_confirmed": True if term["confirmed"] else None,
        "status": "confirmed" if term["confirmed"] else "unconfirmed",
        "code": None if term["confirmed"] else "db_cleanup_unconfirmed",
        "evidence_refs": refs,
    }


def _unconfirmed_sql_entry(reason: str) -> dict:
    return {
        "resource_type": "sql",
        "cancel_requested": False,
        "observable_boundary": "no handshake connection id was captured",
        "termination_confirmed": None,
        "status": "unconfirmed",
        "code": "db_cleanup_unconfirmed",
        "evidence_refs": [reason],
    }


def _cleanup_after_interrupt(session, connection_id, grace_deadline_ns: int) -> tuple:
    """D2: one grace deadline shared by KILL, root diagnostic and local reclaim."""
    kill_requested = False
    if connection_id is not None and _now_ns() < grace_deadline_ns:
        _run_simple_sql(
            RO_CNF,
            "KILL CONNECTION %d;" % int(connection_id),
            grace_deadline_ns,
        )
        kill_requested = True

    confirm = _confirm_connection_gone(
        connection_id, grace_deadline_ns=grace_deadline_ns
    )
    confirmed = bool(confirm["confirmed"])
    entries = [
        {
            "resource_type": "sql",
            "cancel_requested": kill_requested,
            "observable_boundary": "ro-account KILL CONNECTION on handshake connection_id",
            "termination_confirmed": True if confirmed else None,
            "status": "confirmed" if confirmed else "unconfirmed",
            "code": None if confirmed else "db_cleanup_unconfirmed",
            "evidence_refs": [
                "handshake.connection_id=%s" % connection_id,
                "performance_schema.threads probes=%d last=%s"
                % (confirm["attempts"], confirm["last_count"]),
            ],
        }
    ]
    term = _terminate_client(session, grace_deadline_ns)
    entries.append(_process_cleanup_entry(term))
    return entries, confirmed, kill_requested


# --------------------------------------------------------------------------
# result conversion
# --------------------------------------------------------------------------
def _convert_cell(value, column_type: str, nullable: bool):
    if value is None:
        if not nullable:
            raise ValueError("null in non-nullable column")
        return None
    if column_type == "string":
        if not isinstance(value, str):
            raise ValueError("string cell is not a string")
        return value
    if column_type == "boolean":
        if value == "YES":
            return True
        if value == "NO":
            return False
        raise ValueError("boolean cell is not YES/NO")
    if column_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("integer cell is not an integer")
        return value
    if column_type == "decimal":
        if not isinstance(value, str):
            raise ValueError("decimal cell is not a string")
        return value
    if column_type == "datetime":
        if not isinstance(value, str):
            raise ValueError("datetime cell is not a string")
        match = _MYSQL_DATETIME_RE.match(value)
        if match is None:
            raise ValueError("datetime cell has an unexpected format")
        base = match.group(1)
        fraction = match.group(2) or ""
        _dt.datetime.strptime(base, "%Y-%m-%d %H:%M:%S")
        if fraction and fraction[1:].strip("0") == "":
            fraction = ""
        return base.replace(" ", "T") + fraction + "+08:00"
    raise ValueError("unsupported column type")


def _build_table(query_id: str, raw_rows: list, max_rows: int) -> dict:
    definitions = _QUERY_DEFS[query_id]["columns"]
    columns = [
        {"name": name, "type": column_type, "nullable": nullable}
        for name, column_type, nullable in definitions
    ]
    truncated = len(raw_rows) > max_rows
    if truncated:
        raw_rows = raw_rows[:max_rows]

    rows = []
    for raw_row in raw_rows:
        if not isinstance(raw_row, list) or len(raw_row) != len(definitions):
            raise ValueError("row does not match the static column metadata")
        converted = []
        for value, (name, column_type, nullable) in zip(raw_row, definitions):
            try:
                converted.append(_convert_cell(value, column_type, nullable))
            except ValueError as exc:
                raise ValueError("%s: %s" % (name, exc)) from None
        rows.append(converted)

    return {
        "columns": columns,
        "rows": rows,
        "truncated": truncated,
        "truncation_reason": "row_limit" if truncated else None,
    }


# --------------------------------------------------------------------------
# public entry point
# --------------------------------------------------------------------------
def _wait_exit_or_none(session, deadline_ns: int):
    try:
        return session.wait_exit(deadline_ns)
    except _SessionTimeout:
        return None


def run_named_query(
    query_id: str,
    params: dict,
    *,
    remaining_timeout_ms: int,
    max_rows: int = 200,
) -> dict:
    """Run one fixed named query and return the internal tool DTO.

    ``counts.queries`` follows actual database submission evidence:

    * ``0``  -- no confirmed submission (including a 2003 connection failure);
    * ``1``  -- the identity handshake was confirmed, no named query dispatched;
    * ``2``  -- the named query has submission evidence (rows returned or the
      server's own 3024 abort);
    * ``None`` -- dispatched but no reliable server evidence; unknown is never
      reported as 0.
    """
    started_ns = _now_ns()
    manifest = _load_manifest()
    instance = _load_instance_record()
    data_version = (manifest or {}).get("data_version")

    def build(status, *, error=None, table=None, sql=None, queries=0, cleanup=None):
        return _make_dto(
            query_id=query_id,
            data_version=data_version,
            status=status,
            table=table,
            sql=sql,
            error=error,
            queries=queries,
            started_ns=started_ns,
            cleanup=cleanup,
        )

    def refused() -> dict:
        return build(STATUS_REFUSED, queries=0)

    if not isinstance(query_id, str) or query_id not in _QUERY_DEFS:
        return refused()

    try:
        validated = _validate_params(query_id, params)
    except (KeyError, ValueError):
        return refused()

    if isinstance(remaining_timeout_ms, bool) or not isinstance(remaining_timeout_ms, int):
        return refused()
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or not 1 <= max_rows <= MAX_ROWS_LIMIT:
        return refused()

    if remaining_timeout_ms <= 0:
        return build(
            STATUS_TIMEOUT,
            error=_error(CODE_TIMEOUT, "no time remains before dispatch"),
            queries=0,
        )

    if instance is None or manifest is None:
        return build(
            STATUS_ERROR,
            error=_error(CODE_CLIENT, "instance record or fixture manifest is missing"),
            queries=0,
        )
    if not _version_is_consistent(instance, manifest):
        return build(
            STATUS_ERROR,
            error=_error(CODE_VERSION_MISMATCH, "loaded data version does not match db/fixture-manifest.json"),
            queries=0,
        )
    if _normalized_path(instance.get("datadir")) != _normalized_path(str(DATA_DIR)):
        return build(
            STATUS_ERROR,
            error=_error(CODE_INSTANCE_MISMATCH, "instance record does not own this datadir"),
            queries=0,
        )

    effective_ms = min(remaining_timeout_ms, MAX_EFFECTIVE_MS)
    deadline_ns = started_ns + effective_ms * 1_000_000
    limit = max_rows + 1

    # F1: an already expired budget must not start a client at all.
    if not _has_time(deadline_ns):
        return build(
            STATUS_TIMEOUT,
            error=_error(CODE_TIMEOUT, "budget was exhausted before the client was started"),
            queries=0,
        )

    session = None
    try:
        session = _SESSION_FACTORY(RO_CNF)
    except (OSError, ValueError):
        return build(
            STATUS_ERROR,
            error=_error(CODE_CLIENT, "could not start the mysql client"),
            queries=0,
        )

    def finish(payload: dict) -> dict:
        """Every return path goes through here: never leave a held client alive."""
        entries = payload.get("cleanup") or []
        existing = next(
            (entry for entry in entries if entry.get("resource_type") == "adapter_process"),
            None,
        )
        needs_reclaim = existing is None or existing.get("termination_confirmed") is not True
        if needs_reclaim and session.is_alive():
            term = _terminate_client(session, _new_grace_deadline())
            if existing is not None:
                entries.remove(existing)
            entries.append(_process_cleanup_entry(term))
            payload["cleanup"] = entries
        session.close()
        return payload

    try:
        # F1: the spawn itself consumes budget; re-check before the handshake.
        if not _has_time(deadline_ns):
            return finish(
                build(
                    STATUS_TIMEOUT,
                    error=_error(CODE_TIMEOUT, "budget expired before the handshake was dispatched"),
                    queries=0,
                )
            )

        try:
            session.send(_handshake_sql(_hint_ms(deadline_ns)))
        except _SessionClosed:
            return finish(
                build(
                    STATUS_ERROR,
                    error=_error(CODE_CLIENT, "mysql client closed before the handshake could be sent"),
                    queries=0,
                )
            )

        try:
            handshake_line = session.read_line(deadline_ns)
        except _SessionTimeout:
            entries, _confirmed, _ = _cleanup_after_interrupt(
                session, None, _new_grace_deadline()
            )
            return finish(
                build(
                    STATUS_ERROR,
                    error=_error(
                        CODE_CLEANUP_UNCONFIRMED,
                        "identity handshake timed out before its connection id was known",
                    ),
                    queries=None,
                    cleanup=entries,
                )
            )
        except _SessionOverflow:
            entries, _confirmed, _ = _cleanup_after_interrupt(
                session, None, _new_grace_deadline()
            )
            return finish(
                build(
                    STATUS_ERROR,
                    error=_error(
                        CODE_CLEANUP_UNCONFIRMED,
                        "client output exceeded the limit before the connection id was known",
                    ),
                    queries=None,
                    cleanup=entries,
                )
            )
        except _SessionClosed:
            exit_code = _wait_exit_or_none(session, deadline_ns)
            stderr_text = session.stderr_text()
            connection_failed = exit_code not in (0, None) and "2003" in stderr_text
            entries = [
                _unconfirmed_sql_entry(
                    "client output closed before the handshake; connection id unknown"
                )
            ]
            return finish(
                build(
                    STATUS_ERROR,
                    error=_error(
                        CODE_CLIENT,
                        "client output closed before the handshake; held client state was re-checked",
                    ),
                    queries=0 if connection_failed else None,
                    cleanup=entries,
                )
            )

        try:
            handshake = json.loads(handshake_line)
        except ValueError:
            handshake = None
        if not isinstance(handshake, dict):
            return finish(
                build(
                    STATUS_ERROR,
                    error=_error(CODE_RESULT_INVALID, "handshake output was not a JSON object"),
                    queries=1,
                )
            )

        connection_id = handshake.get("connection_id")
        mismatch = (
            not isinstance(connection_id, int)
            or isinstance(connection_id, bool)
            or handshake.get("port") != instance.get("port")
            or _normalized_path(handshake.get("datadir")) != _normalized_path(instance.get("datadir"))
            or handshake.get("server_uuid") != instance.get("server_uuid")
            or handshake.get("port") != PORT
        )
        if mismatch:
            return finish(
                build(
                    STATUS_ERROR,
                    error=_error(CODE_INSTANCE_MISMATCH, "handshake did not match this owned instance"),
                    queries=1,
                )
            )

        queries = 1  # identity handshake is a confirmed submitted SELECT

        # F1: handshake confirmed but the budget may already be gone.
        if not _has_time(deadline_ns):
            return finish(
                build(
                    STATUS_TIMEOUT,
                    error=_error(
                        CODE_TIMEOUT,
                        "budget expired after the handshake; the named query was not dispatched",
                    ),
                    queries=queries,
                )
            )

        sql = _build_query_sql(query_id, validated, _hint_ms(deadline_ns), limit)
        try:
            session.send(sql)
        except _SessionClosed:
            return finish(
                build(
                    STATUS_ERROR,
                    error=_error(CODE_CLIENT, "client closed before the named query could be dispatched"),
                    queries=queries,
                )
            )
        session.close_stdin()
        queries = None  # dispatched; no reliable server evidence yet

        try:
            raw_rows = []
            while True:
                raw_rows.append(json.loads(session.read_line(deadline_ns)))
        except _SessionTimeout:
            entries, confirmed, _ = _cleanup_after_interrupt(
                session, connection_id, _new_grace_deadline()
            )
            if not confirmed:
                return finish(
                    build(
                        STATUS_ERROR,
                        error=_error(
                            CODE_CLEANUP_UNCONFIRMED,
                            "deadline hit and cleanup of the connection was not confirmed",
                        ),
                        queries=queries,
                        cleanup=entries,
                    )
                )
            return finish(
                build(
                    STATUS_TIMEOUT,
                    error=_error(CODE_TIMEOUT, "named query exceeded the deadline"),
                    queries=queries,
                    cleanup=entries,
                )
            )
        except _SessionOverflow:
            entries, confirmed, _ = _cleanup_after_interrupt(
                session, connection_id, _new_grace_deadline()
            )
            code = CODE_OUTPUT_LIMIT if confirmed else CODE_CLEANUP_UNCONFIRMED
            return finish(
                build(
                    STATUS_ERROR,
                    error=_error(code, "client output exceeded the limit"),
                    queries=queries,
                    cleanup=entries,
                )
            )
        except _SessionClosed:
            exit_code = _wait_exit_or_none(session, deadline_ns)
            stderr_text = session.stderr_text()
            if exit_code is None:
                entries, _confirmed, _ = _cleanup_after_interrupt(
                    session, connection_id, _new_grace_deadline()
                )
                return finish(
                    build(
                        STATUS_ERROR,
                        error=_error(
                            CODE_CLEANUP_UNCONFIRMED,
                            "client output closed but its exit status stayed unknown within the deadline",
                        ),
                        queries=queries,
                        cleanup=entries,
                    )
                )
            if exit_code != 0:
                if "3024" in stderr_text:
                    entries, confirmed, _ = _cleanup_after_interrupt(
                        session, connection_id, _new_grace_deadline()
                    )
                    if not confirmed:
                        return finish(
                            build(
                                STATUS_ERROR,
                                error=_error(
                                    CODE_CLEANUP_UNCONFIRMED,
                                    "server aborted at MAX_EXECUTION_TIME but cleanup was not confirmed",
                                ),
                                queries=2,
                                cleanup=entries,
                            )
                        )
                    return finish(
                        build(
                            STATUS_TIMEOUT,
                            error=_error(CODE_TIMEOUT, "server aborted the query at MAX_EXECUTION_TIME"),
                            queries=2,
                            cleanup=entries,
                        )
                    )
                return finish(
                    build(
                        STATUS_ERROR,
                        error=_error(CODE_CLIENT, "mysql client exited with a non-zero status"),
                        queries=queries,
                    )
                )
            queries = 2  # rows returned by the server are submission evidence
        except ValueError:
            return finish(
                build(
                    STATUS_ERROR,
                    error=_error(CODE_RESULT_INVALID, "row output was not a JSON array"),
                    queries=2,
                )
            )

        try:
            table = _build_table(query_id, raw_rows, max_rows)
        except ValueError:
            return finish(
                build(
                    STATUS_ERROR,
                    error=_error(CODE_RESULT_INVALID, "result did not match the static column metadata"),
                    queries=2,
                )
            )

        # F1: formatting/validation time is part of the shared deadline.
        if not _has_time(deadline_ns):
            return finish(
                build(
                    STATUS_TIMEOUT,
                    error=_error(CODE_TIMEOUT, "result validation finished after the deadline"),
                    queries=2,
                )
            )

        return finish(build(STATUS_OK, table=table, sql=sql, queries=2))
    finally:
        # Safety net for unexpected exceptions: never leave a held client behind.
        try:
            if session.is_alive():
                session.terminate(deadline_ns=_new_grace_deadline())
            session.close()
        except (OSError, ValueError):
            pass


def describe_schema(*, remaining_timeout_ms: int) -> dict:
    """Run schema_columns then schema_relations under one shared deadline."""
    started_ns = _now_ns()
    if isinstance(remaining_timeout_ms, bool) or not isinstance(remaining_timeout_ms, int):
        return {"status": STATUS_REFUSED, "results": [], "counts": {"queries": 0}}
    if remaining_timeout_ms <= 0:
        return {"status": STATUS_TIMEOUT, "results": [], "counts": {"queries": 0}}

    results = []
    total_queries = 0
    for query_id in ("schema_columns", "schema_relations"):
        elapsed_ms = (_now_ns() - started_ns) / 1_000_000
        remaining = int(remaining_timeout_ms - elapsed_ms)
        if remaining <= 0:
            return {
                "status": STATUS_TIMEOUT,
                "results": results,
                "counts": {"queries": total_queries},
            }
        dto = run_named_query(query_id, {}, remaining_timeout_ms=remaining)
        results.append(dto)
        value = dto["counts"]["queries"]
        total_queries = None if (total_queries is None or value is None) else total_queries + value
        if dto["status"] != STATUS_OK:
            return {
                "status": dto["status"],
                "results": results,
                "counts": {"queries": total_queries},
            }
    return {"status": STATUS_OK, "results": results, "counts": {"queries": total_queries}}
