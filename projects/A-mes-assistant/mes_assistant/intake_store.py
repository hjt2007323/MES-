"""M5 downtime intake sandbox: validation, idempotent commit and window reads (B-010).

The module is deliberately small and self-contained:

* standard-library SQLite only; no MySQL, network, model or tool access;
* fixed, parameterized SQL; no public API accepts arbitrary SQL;
* ``record`` has exactly the five M5 fields (``order_id`` may be null);
* a submission key makes retries idempotent; a different payload under the
  same key is a conflict;
* reads use the existing ``downtime_details`` column definition and the
  half-open intersection rule ``start_time < window_end AND end_time > window_start``.

Errors never echo local paths, SQL text or field values.
"""

from __future__ import annotations

import copy
import datetime as _dt
import json
import os
import re
import sqlite3
import stat
import tempfile
from pathlib import Path

from .query_catalog import describe_query

__all__ = [
    "DEFAULT_DB_PATH",
    "MACHINE_WHITELIST",
    "ORDER_WHITELIST",
    "PRODUCTION_DB_PATH",
    "SANDBOX_ID",
    "DowntimeStore",
    "IntakeError",
]

SANDBOX_ID = "intake-sandbox-v1"
MACHINE_WHITELIST = ("M01", "M02", "M03")
ORDER_WHITELIST = ("WO01", "WO02", "WO03", "WO04", "WO05", "WO06")
DEFAULT_DB_PATH = (
    Path(__file__).resolve().parents[3] / "runtime" / "a07-intake" / "downtime.sqlite3"
)
PRODUCTION_DB_PATH = DEFAULT_DB_PATH

INVALID_RECORD = "INVALID_RECORD"
INVALID_TIME_ORDER = "INVALID_TIME_ORDER"
SUBMISSION_CONFLICT = "SUBMISSION_CONFLICT"
STORE_UNAVAILABLE = "STORE_UNAVAILABLE"

_FIELDS = ("machine_no", "start_time", "end_time", "reason", "order_id")
_REQUIRED = ("machine_no", "start_time", "end_time", "reason")
_MACHINE_ALIASES = {
    "1号机": "M01",
    "一号机": "M01",
    "2号机": "M02",
    "二号机": "M02",
    "3号机": "M03",
    "三号机": "M03",
}
_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$")
_MAX_REASON_CHARS = 32
_MAX_KEY_CHARS = 128
_MAX_ROWS = 200
_TRUNCATION_REASON = "row_limit"
_DENY_PARTS = frozenset(
    {"mysql-a07", "mysql-8.0.28-winx64", "dsh", ".dsh"}
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS downtime_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_no      TEXT    NOT NULL,
    start_time      TEXT    NOT NULL,
    end_time        TEXT    NOT NULL,
    reason          TEXT    NOT NULL,
    order_id        TEXT    NULL,
    submission_key  TEXT    NOT NULL UNIQUE,
    payload         TEXT    NOT NULL,
    created_at      TEXT    NOT NULL
)
"""

_INSERT_SQL = """
INSERT INTO downtime_events
    (machine_no, start_time, end_time, reason, order_id, submission_key, payload, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""

_SELECT_KEY_SQL = "SELECT id, payload FROM downtime_events WHERE submission_key = ?"
_SELECT_WINDOW_SQL = (
    "SELECT id, machine_no, start_time, end_time, reason, order_id "
    "FROM downtime_events "
    "WHERE start_time < ? AND end_time > ? "
    "ORDER BY id ASC LIMIT ?"
)
_SEARCH_COLUMNS = "id, machine_no, start_time, end_time, reason, order_id"
_SEARCH_SOURCE = "intake_sqlite"
_SEARCH_SCHEMA_VERSION = "m5-intake-search-v1"


class IntakeError(ValueError):
    """Fixed-code validation/intake failure; ``str`` carries no input values."""

    CODES = frozenset(
        {INVALID_RECORD, INVALID_TIME_ORDER, SUBMISSION_CONFLICT, STORE_UNAVAILABLE}
    )

    def __init__(self, code: str, message: str):
        if code not in self.CODES:
            raise ValueError("unknown IntakeError code")
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return "%s: %s" % (self.code, self.message)


def _fail(code: str, message: str):
    raise IntakeError(code, message)


def _parse_time(value, field: str) -> _dt.datetime:
    if not isinstance(value, str) or not _TIME_RE.match(value):
        _fail(INVALID_RECORD, "%s must be an ISO time with +08:00" % field)
    try:
        parsed = _dt.datetime.fromisoformat(value)
    except ValueError:
        _fail(INVALID_RECORD, "%s is not a valid calendar time" % field)
    if parsed.utcoffset() != _dt.timedelta(hours=8):
        _fail(INVALID_RECORD, "%s must use +08:00" % field)
    return parsed


def _canonical_time(parsed: _dt.datetime) -> str:
    return parsed.isoformat(timespec="seconds")


def _normalize_machine(value) -> str:
    if not isinstance(value, str):
        _fail(INVALID_RECORD, "machine_no must be a string")
    text = value.strip()
    if text in MACHINE_WHITELIST:
        return text
    alias = _MACHINE_ALIASES.get(text)
    if alias is not None:
        return alias
    _fail(INVALID_RECORD, "machine_no is not a known machine")


def _normalize_order(value):
    if value is None:
        return None
    if not isinstance(value, str):
        _fail(INVALID_RECORD, "order_id must be a string or null")
    text = value.strip()
    if text not in ORDER_WHITELIST:
        _fail(INVALID_RECORD, "order_id is not a known work order")
    return text


def _normalize_reason(value) -> str:
    if not isinstance(value, str):
        _fail(INVALID_RECORD, "reason must be a string")
    reason = value.strip()
    if not 1 <= len(reason) <= _MAX_REASON_CHARS:
        _fail(INVALID_RECORD, "reason length is outside 1-32 characters")
    return reason


def _validate_submission_key(value) -> str:
    if not isinstance(value, str):
        _fail(INVALID_RECORD, "submission_key must be a string")
    key = value.strip()
    if not 1 <= len(key) <= _MAX_KEY_CHARS:
        _fail(INVALID_RECORD, "submission_key length is invalid")
    return key


def _payload(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _absolute(value) -> Path:
    if not isinstance(value, (str, Path)):
        _fail(STORE_UNAVAILABLE, "downtime store path must be a file path")
    try:
        return Path(os.path.abspath(os.fspath(value)))
    except (OSError, RuntimeError, ValueError, TypeError):
        _fail(STORE_UNAVAILABLE, "downtime store path is not usable")


def _workspace_root() -> Path:
    return _absolute(DEFAULT_DB_PATH).parents[2]


def _temp_root() -> Path:
    try:
        return _absolute(tempfile.gettempdir())
    except (OSError, RuntimeError, ValueError):
        _fail(STORE_UNAVAILABLE, "system temporary directory is not usable")


def _has_denied_part(path: Path) -> bool:
    return any(part.lower() in _DENY_PARTS for part in path.parts)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_link_like(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        if hasattr(path, "is_junction") and path.is_junction():
            return True
        status = path.stat(follow_symlinks=False)
    except OSError:
        return False
    attributes = getattr(status, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attributes & reparse)


def _assert_no_links(path: Path, boundary: Path) -> None:
    if _is_link_like(boundary):
        _fail(STORE_UNAVAILABLE, "downtime store path uses a linked boundary")
    if path == boundary:
        return
    current = boundary
    for part in path.relative_to(boundary).parts:
        current = current / part
        if _is_link_like(current):
            _fail(STORE_UNAVAILABLE, "downtime store path uses a linked component")


def _is_hardlink_alias(path: Path) -> bool:
    try:
        status = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return getattr(status, "st_nlink", 1) > 1


def _assert_no_hardlink_alias(path: Path) -> None:
    if path.exists() and path.is_file() and _is_hardlink_alias(path):
        _fail(STORE_UNAVAILABLE, "downtime store path is a hard-link alias")


def _validate_production_path() -> Path:
    path = _absolute(DEFAULT_DB_PATH)
    boundary = path.parents[1]
    if _has_denied_part(path):
        _fail(STORE_UNAVAILABLE, "downtime store may not use an excluded source")
    if not _is_within(path, boundary):
        _fail(STORE_UNAVAILABLE, "downtime store path is outside the runtime root")
    _assert_no_links(path, boundary)
    _assert_no_hardlink_alias(path)
    return path


def _validate_test_root(value) -> Path:
    root = _absolute(value)
    if not root.is_dir():
        _fail(STORE_UNAVAILABLE, "test_root must be an existing directory")
    if _is_link_like(root):
        _fail(STORE_UNAVAILABLE, "test_root must not be a link or reparse point")
    if _has_denied_part(root):
        _fail(STORE_UNAVAILABLE, "test_root may not use an excluded source")
    matched = None
    for base in (_workspace_root() / "execution", _temp_root()):
        if _is_within(root, base):
            matched = base
            break
    if matched is None:
        _fail(STORE_UNAVAILABLE, "test_root is not a trusted test location")
    if root == matched:
        _fail(STORE_UNAVAILABLE, "test_root must be a child of the trusted location")
    _assert_no_links(root, matched)
    return root


def _validate_db_path(path, test_root):
    candidate = _absolute(path)
    if _has_denied_part(candidate):
        _fail(STORE_UNAVAILABLE, "downtime store may not use an excluded source")
    production = _absolute(DEFAULT_DB_PATH)
    if candidate == production:
        if test_root is not None:
            _fail(STORE_UNAVAILABLE, "test_root may not be combined with the production path")
        return _validate_production_path(), None, True
    if test_root is None:
        _fail(STORE_UNAVAILABLE, "a non-production path requires an explicit test_root")
    root = _validate_test_root(test_root)
    if candidate == root or not _is_within(candidate, root):
        _fail(STORE_UNAVAILABLE, "downtime store path is outside the test root")
    _assert_no_links(candidate, root)
    _assert_no_hardlink_alias(candidate)
    if candidate.exists() and candidate.is_dir():
        _fail(STORE_UNAVAILABLE, "downtime store path is a directory")
    return candidate, root, False


def _columns() -> list:
    return copy.deepcopy(describe_query("downtime_details")["columns"])


class DowntimeStore:
    """SQLite-backed M5 downtime intake sandbox.

    ``DowntimeStore()`` uses the fixed production path
    ``runtime/a07-intake/downtime.sqlite3``.  Tests may inject another path
    only as ``DowntimeStore(path, test_root=<trusted-root>)`` where the
    resolved path stays inside an existing root under the workspace
    ``execution/`` tree or the system temporary directory.  The test root is
    caller-supplied on purpose; it is never read from the UI, a model or an
    environment string.
    """

    def __init__(self, path=None, *, test_root=None):
        if path is None:
            if test_root is not None:
                _fail(STORE_UNAVAILABLE, "test_root requires an explicit path")
            self._path = _validate_production_path()
            self._root = None
            self._production = True
        else:
            self._path, self._root, self._production = _validate_db_path(path, test_root)

    @property
    def path(self) -> Path:
        """Normalized database path (read-only diagnostic; links are rejected)."""
        return self._path

    def _revalidate(self) -> None:
        if self._production:
            if self._path != _validate_production_path():
                _fail(STORE_UNAVAILABLE, "downtime store path changed")
            return
        root = _validate_test_root(self._root)
        if root != self._root:
            _fail(STORE_UNAVAILABLE, "test_root changed")
        if self._path == root or not _is_within(self._path, root):
            _fail(STORE_UNAVAILABLE, "downtime store path is outside the test root")
        _assert_no_links(self._path, root)
        _assert_no_hardlink_alias(self._path)
        if self._path.exists() and self._path.is_dir():
            _fail(STORE_UNAVAILABLE, "downtime store path is a directory")

    # ---------------------------------------------------------------- validation
    def validate_record(self, record) -> dict:
        """Return the canonical five-field record or raise ``IntakeError``."""
        if not isinstance(record, dict):
            _fail(INVALID_RECORD, "record must be an object")
        unknown = set(record) - set(_FIELDS)
        if unknown:
            _fail(INVALID_RECORD, "record contains unknown fields")
        for field in _REQUIRED:
            if field not in record:
                _fail(INVALID_RECORD, "record is missing a required field")

        machine_no = _normalize_machine(record["machine_no"])
        start = _parse_time(record["start_time"], "start_time")
        end = _parse_time(record["end_time"], "end_time")
        if end <= start:
            _fail(INVALID_TIME_ORDER, "end_time must be later than start_time")
        reason = _normalize_reason(record["reason"])
        order_id = _normalize_order(record.get("order_id"))
        return {
            "machine_no": machine_no,
            "start_time": _canonical_time(start),
            "end_time": _canonical_time(end),
            "reason": reason,
            "order_id": order_id,
        }

    # ------------------------------------------------------------------ database
    def _connect(self) -> sqlite3.Connection:
        self._revalidate()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                str(self._path), timeout=5.0, isolation_level=None
            )
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute(_SCHEMA_SQL)
            return connection
        except (OSError, sqlite3.Error):
            _fail(STORE_UNAVAILABLE, "downtime store is unavailable")

    @staticmethod
    def _close(connection) -> None:
        try:
            connection.close()
        except sqlite3.Error:
            pass

    def _lookup(self, submission_key: str):
        try:
            connection = self._connect()
            try:
                return connection.execute(_SELECT_KEY_SQL, (submission_key,)).fetchone()
            finally:
                self._close(connection)
        except sqlite3.Error:
            _fail(STORE_UNAVAILABLE, "downtime store is unavailable")

    # -------------------------------------------------------------------- commit
    def commit(self, record, *, submission_key) -> dict:
        """Insert once; same key + same payload replays, changed payload conflicts."""
        normalized = self.validate_record(record)
        key = _validate_submission_key(submission_key)
        payload = _payload(normalized)

        try:
            connection = self._connect()
        except sqlite3.Error:
            _fail(STORE_UNAVAILABLE, "downtime store is unavailable")
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(_SELECT_KEY_SQL, (key,)).fetchone()
            if existing is not None:
                if existing[1] != payload:
                    connection.execute("ROLLBACK")
                    _fail(SUBMISSION_CONFLICT, "submission key already has another payload")
                connection.execute("COMMIT")
                return {"record_id": int(existing[0]), "replayed": True, "record": normalized}

            cursor = connection.execute(
                _INSERT_SQL,
                (
                    normalized["machine_no"],
                    normalized["start_time"],
                    normalized["end_time"],
                    normalized["reason"],
                    normalized["order_id"],
                    key,
                    payload,
                    _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            record_id = int(cursor.lastrowid)
            connection.execute("COMMIT")
            return {"record_id": record_id, "replayed": False, "record": normalized}
        except IntakeError:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        except sqlite3.IntegrityError:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            existing = self._lookup(key)
            if existing is not None and existing[1] == payload:
                return {"record_id": int(existing[0]), "replayed": True, "record": normalized}
            _fail(SUBMISSION_CONFLICT, "submission key already has another payload")
        except sqlite3.Error:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            _fail(STORE_UNAVAILABLE, "downtime store is unavailable")
        finally:
            self._close(connection)

    # ------------------------------------------------------------------- read back
    def list_window(self, window_start, window_end, *, limit: int = 200) -> dict:
        """Return completed events intersecting ``[window_start, window_end)``."""
        start = _parse_time(window_start, "window_start")
        end = _parse_time(window_end, "window_end")
        if end <= start:
            _fail(INVALID_TIME_ORDER, "window_end must be later than window_start")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            _fail(INVALID_RECORD, "limit must be a positive integer")
        effective_limit = min(limit, _MAX_ROWS)

        try:
            connection = self._connect()
            try:
                fetched = connection.execute(
                    _SELECT_WINDOW_SQL,
                    (_canonical_time(end), _canonical_time(start), effective_limit + 1),
                ).fetchall()
            finally:
                self._close(connection)
        except sqlite3.Error:
            _fail(STORE_UNAVAILABLE, "downtime store is unavailable")

        truncated = len(fetched) > effective_limit
        rows = [list(row) for row in fetched[:effective_limit]]
        return {
            "columns": _columns(),
            "rows": rows,
            "truncated": truncated,
            "truncation_reason": _TRUNCATION_REASON if truncated else None,
        }

    def _connect_readonly(self) -> sqlite3.Connection:
        """Open an existing store read-only; never create a file or schema."""
        self._revalidate()
        if not self._path.is_file():
            _fail(STORE_UNAVAILABLE, "downtime store is unavailable")
        try:
            connection = sqlite3.connect(
                self._path.as_uri() + "?mode=ro",
                uri=True,
                timeout=5.0,
                isolation_level=None,
            )
            connection.execute("PRAGMA busy_timeout = 5000")
            return connection
        except (OSError, sqlite3.Error):
            _fail(STORE_UNAVAILABLE, "downtime store is unavailable")

    def search_window(
        self,
        window_start=None,
        window_end=None,
        *,
        machine_no=None,
        record_id=None,
        limit: int = 200,
    ) -> dict:
        """Read-only query by record id or half-open window, with optional machine.

        A window requires both bounds; ``record_id`` may be used without one.
        All filters bind in SQL before ordering/limit, so a filtered result is
        never truncated by unrelated rows.  The database must already exist;
        this method never creates it or writes to it.
        """
        if record_id is not None:
            if isinstance(record_id, bool) or not isinstance(record_id, int):
                _fail(INVALID_RECORD, "record_id must be a positive integer")
            if record_id < 1:
                _fail(INVALID_RECORD, "record_id must be a positive integer")
        if (window_start is None) != (window_end is None):
            _fail(INVALID_RECORD, "window_start and window_end must be provided together")
        if record_id is None and window_start is None:
            _fail(INVALID_RECORD, "a complete window or record_id is required")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            _fail(INVALID_RECORD, "limit must be a positive integer")
        effective_limit = min(limit, _MAX_ROWS)

        start = end = None
        if window_start is not None:
            start = _parse_time(window_start, "window_start")
            end = _parse_time(window_end, "window_end")
            if end <= start:
                _fail(INVALID_TIME_ORDER, "window_end must be later than window_start")
        machine = _normalize_machine(machine_no) if machine_no is not None else None

        conditions = []
        params = []
        if record_id is not None:
            conditions.append("id = ?")
            params.append(record_id)
        if start is not None:
            conditions.append("start_time < ?")
            conditions.append("end_time > ?")
            params.extend((_canonical_time(end), _canonical_time(start)))
        if machine is not None:
            conditions.append("machine_no = ?")
            params.append(machine)
        sql = (
            "SELECT %s FROM downtime_events WHERE %s ORDER BY id ASC LIMIT ?"
            % (_SEARCH_COLUMNS, " AND ".join(conditions))
        )
        params.append(effective_limit + 1)

        try:
            connection = self._connect_readonly()
            try:
                fetched = connection.execute(sql, tuple(params)).fetchall()
            finally:
                self._close(connection)
        except sqlite3.Error:
            _fail(STORE_UNAVAILABLE, "downtime store is unavailable")

        truncated = len(fetched) > effective_limit
        rows = [list(row) for row in fetched[:effective_limit]]
        filters = {
            "window_start": _canonical_time(start) if start is not None else None,
            "window_end": _canonical_time(end) if end is not None else None,
            "machine_no": machine,
            "record_id": record_id,
            "limit": effective_limit,
        }
        return {
            "columns": _columns(),
            "rows": rows,
            "truncated": truncated,
            "truncation_reason": _TRUNCATION_REASON if truncated else None,
            "source": _SEARCH_SOURCE,
            "schema_version": _SEARCH_SCHEMA_VERSION,
            "snapshot_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "filters": filters,
        }
