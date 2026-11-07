"""Offline tests for the M5 downtime intake sandbox (B-010)."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mes_assistant import intake_store  # noqa: E402
from mes_assistant.intake_store import DowntimeStore, IntakeError  # noqa: E402
from mes_assistant.query_catalog import describe_query  # noqa: E402

START = "2026-11-04T08:00:00+08:00"
END = "2026-11-04T09:00:00+08:00"


def make_record(**overrides):
    record = {
        "machine_no": "M01",
        "start_time": START,
        "end_time": END,
        "reason": "换型",
        "order_id": "WO01",
    }
    record.update(overrides)
    return record


def make_directory_link(link, target):
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError, AttributeError):
        if os.name == "nt":
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                text=True,
            )
            return result.returncode == 0 and link.exists()
        return False


def remove_directory_link(link):
    try:
        if link.is_symlink():
            os.unlink(link)
        elif link.exists():
            os.rmdir(link)
    except OSError:
        pass


def make_hardlink(link, target):
    try:
        os.link(target, link)
        return True
    except (OSError, NotImplementedError, AttributeError):
        return False


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree_files(root):
    return sorted(
        path.relative_to(root).as_posix()
        for path in Path(root).rglob("*")
        if path.is_file()
    )


class StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.path = self.tmp / "downtime.sqlite3"
        self.store = DowntimeStore(self.path, test_root=self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def count_rows(self, store=None):
        target = store or self.store
        result = target.list_window("2020-01-01T00:00:00+08:00", "2030-01-01T00:00:00+08:00")
        return len(result["rows"])

    def assert_code(self, code, func, *args, **kwargs):
        with self.assertRaises(IntakeError) as raised:
            func(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def fake_workspace(self):
        workspace = self.tmp / "fake-workspace"
        (workspace / "runtime" / "a07-intake").mkdir(parents=True)
        (workspace / "execution").mkdir()
        return workspace


class TestValidateRecord(StoreCase):
    def test_normalizes_exact_five_fields(self):
        normalized = self.store.validate_record(
            {
                "machine_no": " 1号机 ",
                "start_time": START,
                "end_time": END,
                "reason": "  换型  ",
                "order_id": " WO01 ",
            }
        )
        self.assertEqual(
            normalized,
            {
                "machine_no": "M01",
                "start_time": START,
                "end_time": END,
                "reason": "换型",
                "order_id": "WO01",
            },
        )
        self.assertEqual(
            set(normalized),
            {"machine_no", "start_time", "end_time", "reason", "order_id"},
        )

    def test_missing_order_defaults_to_null(self):
        record = make_record()
        record.pop("order_id")
        self.assertIsNone(self.store.validate_record(record)["order_id"])

    def test_aliases_and_null_order(self):
        normalized = self.store.validate_record(
            make_record(machine_no="三号机", order_id=None)
        )
        self.assertEqual(normalized["machine_no"], "M03")
        self.assertIsNone(normalized["order_id"])

    def test_invalid_shapes_and_fields(self):
        cases = [
            None,
            [],
            "record",
            {"machine_no": "M01", "start_time": START, "reason": "x"},
            make_record(extra="value"),
            make_record(sql="SELECT 1"),
        ]
        for record in cases:
            with self.subTest(record=repr(record)):
                self.assert_code("INVALID_RECORD", self.store.validate_record, record)

    def test_unknown_machine_and_order(self):
        for record in (
            make_record(machine_no="M99"),
            make_record(machine_no="4号机"),
            make_record(order_id="WO99"),
            make_record(order_id=""),
            make_record(order_id=1),
            make_record(machine_no=1),
        ):
            with self.subTest(record=record):
                self.assert_code("INVALID_RECORD", self.store.validate_record, record)

    def test_reason_length_and_type(self):
        for reason in ("", "   ", "x" * 33, 123):
            with self.subTest(reason=reason):
                self.assert_code(
                    "INVALID_RECORD", self.store.validate_record, make_record(reason=reason)
                )
        self.assertEqual(len(self.store.validate_record(make_record(reason="x" * 32))["reason"]), 32)

    def test_time_format_is_exact(self):
        for value in (
            "2026-11-04T08:00:00Z",
            "2026-11-04T08:00:00.000+08:00",
            "2026-11-04 08:00:00+08:00",
            "2026-11-04T08:00+08:00",
            "2026-13-01T08:00:00+08:00",
            "2026-02-30T08:00:00+08:00",
            "2026-11-04T08:00:00+07:00",
        ):
            with self.subTest(value=value):
                self.assert_code(
                    "INVALID_RECORD", self.store.validate_record, make_record(start_time=value)
                )

    def test_reverse_or_equal_time_order(self):
        for end in (START, "2026-11-04T07:00:00+08:00"):
            with self.subTest(end=end):
                self.assert_code(
                    "INVALID_TIME_ORDER",
                    self.store.validate_record,
                    make_record(end_time=end),
                )


class TestCommit(StoreCase):
    def test_first_commit_then_replay_and_persistence(self):
        first = self.store.commit(make_record(), submission_key="draft-1")
        self.assertFalse(first["replayed"])
        self.assertIsInstance(first["record_id"], int)
        self.assertEqual(first["record"], make_record())

        replay = DowntimeStore(self.path, test_root=self.tmp).commit(make_record(), submission_key="draft-1")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["record_id"], first["record_id"])
        self.assertEqual(replay["record"], first["record"])

        reopened = DowntimeStore(self.path, test_root=self.tmp)
        found = reopened.list_window(START, END)
        self.assertEqual(len(found["rows"]), 1)
        self.assertEqual(found["rows"][0][0], first["record_id"])
        self.assertEqual(found["rows"][0][1], "M01")

    def test_same_key_normalized_equivalence_replays(self):
        first = self.store.commit(make_record(), submission_key="draft-2")
        replay = self.store.commit(
            make_record(reason=" 换型 ", machine_no="一号机"),
            submission_key="  draft-2  ",
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["record_id"], first["record_id"])
        self.assertEqual(self.count_rows(), 1)

    def test_same_key_different_payload_conflicts(self):
        first = self.store.commit(make_record(), submission_key="draft-3")
        self.assert_code(
            "SUBMISSION_CONFLICT",
            self.store.commit,
            make_record(reason="故障"),
            submission_key="draft-3",
        )
        self.assertEqual(self.count_rows(), 1)
        self.assertEqual(
            self.store.list_window(START, END)["rows"][0][0], first["record_id"]
        )

    def test_different_keys_insert_separate_rows(self):
        first = self.store.commit(make_record(), submission_key="draft-4a")
        second = self.store.commit(make_record(reason="保养"), submission_key="draft-4b")
        self.assertNotEqual(first["record_id"], second["record_id"])
        self.assertEqual(self.count_rows(), 2)

    def test_invalid_records_leave_no_rows(self):
        bad_records = [
            make_record(machine_no="M99"),
            make_record(order_id="WO99"),
            make_record(end_time=START),
            make_record(start_time="bad"),
            make_record(extra="x"),
        ]
        for index, record in enumerate(bad_records):
            with self.subTest(index=index):
                with self.assertRaises(IntakeError):
                    self.store.commit(record, submission_key="bad-%d" % index)
        self.assertEqual(self.count_rows(), 0)

    def test_invalid_submission_key_is_rejected(self):
        for key in (None, "", "   ", 1, "x" * 129):
            with self.subTest(key=repr(key)):
                self.assert_code(
                    "INVALID_RECORD", self.store.commit, make_record(), submission_key=key
                )
        self.assertEqual(self.count_rows(), 0)

    def test_unavailable_store_is_fixed_error(self):
        marker = self.tmp / "not-a-directory"
        marker.write_text("x", encoding="utf-8")
        store = DowntimeStore(marker / "downtime.sqlite3", test_root=self.tmp)
        self.assert_code("STORE_UNAVAILABLE", store.commit, make_record(), submission_key="k")

    def test_constructor_rejects_directory_and_mysql_runtime(self):
        self.assert_code(
            "STORE_UNAVAILABLE", DowntimeStore, self.tmp, test_root=self.tmp
        )
        self.assert_code(
            "STORE_UNAVAILABLE",
            DowntimeStore,
            self.tmp / "mysql-a07" / "downtime.sqlite3",
            test_root=self.tmp,
        )

    def test_sqlite_failure_maps_to_store_unavailable(self):
        from unittest import mock

        with mock.patch(
            "mes_assistant.intake_store.sqlite3.connect",
            side_effect=sqlite3.Error("boom"),
        ):
            self.assert_code(
                "STORE_UNAVAILABLE",
                self.store.commit,
                make_record(),
                submission_key="draft-5",
            )

    def test_commit_does_not_echo_input_in_error(self):
        error = self.assert_code(
            "INVALID_RECORD",
            self.store.commit,
            make_record(machine_no="SECRET-MACHINE"),
            submission_key="draft-6",
        )
        self.assertNotIn("SECRET-MACHINE", str(error))


class TestPathPolicy(StoreCase):
    def test_default_and_explicit_production_path(self):
        self.assertEqual(DowntimeStore().path, intake_store.DEFAULT_DB_PATH)
        self.assertEqual(
            DowntimeStore(intake_store.DEFAULT_DB_PATH).path,
            intake_store.DEFAULT_DB_PATH,
        )
        self.assert_code(
            "STORE_UNAVAILABLE",
            DowntimeStore,
            intake_store.DEFAULT_DB_PATH,
            test_root=self.tmp,
        )

    def test_non_production_path_requires_explicit_test_root(self):
        path = self.tmp / "db.sqlite3"
        self.assert_code("STORE_UNAVAILABLE", DowntimeStore, path)
        self.assertFalse(path.exists())

    def test_legal_test_injection_keeps_path_inside_root(self):
        path = self.tmp / "sub" / "db.sqlite3"
        store = DowntimeStore(path, test_root=self.tmp)
        store.commit(make_record(), submission_key="policy-1")
        self.assertTrue(path.exists())
        self.assertEqual(store.path, path.resolve())

    def test_test_root_must_exist_and_be_directory(self):
        self.assert_code(
            "STORE_UNAVAILABLE",
            DowntimeStore,
            self.tmp / "db.sqlite3",
            test_root=self.tmp / "missing",
        )
        marker = self.tmp / "marker-file"
        marker.write_text("x", encoding="utf-8")
        self.assert_code(
            "STORE_UNAVAILABLE",
            DowntimeStore,
            self.tmp / "db.sqlite3",
            test_root=marker,
        )

    def test_test_root_outside_trusted_locations_rejected(self):
        self.assert_code(
            "STORE_UNAVAILABLE",
            DowntimeStore,
            ROOT / "db" / "policy-test.sqlite3",
            test_root=ROOT,
        )
        self.assertFalse((ROOT / "db" / "policy-test.sqlite3").exists())

    def test_sibling_prefix_directory_rejected(self):
        sibling = self.tmp.parent / (self.tmp.name + "-sibling")
        sibling.mkdir()
        try:
            self.assert_code(
                "STORE_UNAVAILABLE",
                DowntimeStore,
                sibling / "db.sqlite3",
                test_root=self.tmp,
            )
            self.assertFalse((sibling / "db.sqlite3").exists())
        finally:
            sibling.rmdir()

    def test_dotdot_escape_rejected_without_creating_target(self):
        outside = self.tmp.parent / (self.tmp.name + "-outside")
        target = self.tmp / ".." / outside.name / "db.sqlite3"
        self.assert_code(
            "STORE_UNAVAILABLE", DowntimeStore, target, test_root=self.tmp
        )
        self.assertFalse(outside.exists())

    def test_symlink_escape_rejected_if_supported(self):
        outside = tempfile.TemporaryDirectory()
        outside_path = Path(outside.name)
        link = self.tmp / "escape-link"
        created = False
        try:
            os.symlink(outside_path, link, target_is_directory=True)
            created = True
        except (OSError, NotImplementedError, AttributeError):
            if os.name == "nt":
                result = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(outside_path)],
                    capture_output=True,
                    text=True,
                )
                created = result.returncode == 0 and link.exists()
        if not created:
            outside.cleanup()
            self.skipTest("symlinks and junctions are not available")
        try:
            self.assert_code(
                "STORE_UNAVAILABLE",
                DowntimeStore,
                link / "db.sqlite3",
                test_root=self.tmp,
            )
            self.assertFalse((outside_path / "db.sqlite3").exists())
        finally:
            try:
                if link.is_symlink():
                    os.unlink(link)
                else:
                    os.rmdir(link)
            except OSError:
                pass
            outside.cleanup()

    def test_path_equal_root_and_denied_sources_rejected(self):
        self.assert_code(
            "STORE_UNAVAILABLE", DowntimeStore, self.tmp, test_root=self.tmp
        )
        for denied in ("mysql-a07", "dsh"):
            self.assert_code(
                "STORE_UNAVAILABLE",
                DowntimeStore,
                self.tmp / denied / "db.sqlite3",
                test_root=self.tmp,
            )


class TestConcurrency(StoreCase):
    def _run_pair(self, key, first_record, second_record):
        barrier = threading.Barrier(2)

        def worker(record):
            store = DowntimeStore(self.path, test_root=self.tmp)
            barrier.wait()
            try:
                return ("ok", store.commit(record, submission_key=key))
            except IntakeError as error:
                return ("error", error.code)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker, first_record), pool.submit(worker, second_record)]
            return [future.result() for future in futures]

    def test_two_connections_same_payload_insert_one_row(self):
        results = self._run_pair("race-1", make_record(), make_record())
        oks = [value for status, value in results if status == "ok"]
        self.assertEqual(len(oks), 2)
        self.assertEqual({item["record_id"] for item in oks}, {oks[0]["record_id"]})
        self.assertEqual(sorted(item["replayed"] for item in oks), [False, True])
        self.assertEqual(self.count_rows(), 1)

    def test_two_connections_different_payload_conflicts(self):
        results = self._run_pair(
            "race-2", make_record(), make_record(reason="故障")
        )
        statuses = sorted(status for status, _ in results)
        self.assertEqual(statuses, ["error", "ok"])
        self.assertIn("SUBMISSION_CONFLICT", [value for status, value in results if status == "error"])
        self.assertEqual(self.count_rows(), 1)


class TestBoundaryRevalidation(StoreCase):
    def fake_workspace(self):
        workspace = self.tmp / "fake-workspace"
        (workspace / "runtime" / "a07-intake").mkdir(parents=True)
        (workspace / "execution").mkdir()
        return workspace

    def test_default_production_path_is_checked_without_writing(self):
        store = DowntimeStore()
        self.assertEqual(store.path, intake_store.DEFAULT_DB_PATH)
        self.assertTrue(store.path.is_absolute())

    def test_system_temp_root_itself_rejected(self):
        temp_root = Path(tempfile.gettempdir())
        candidate = temp_root / "b010-temp-root.sqlite3"
        self.assert_code(
            "STORE_UNAVAILABLE", DowntimeStore, candidate, test_root=temp_root
        )
        self.assertFalse(candidate.exists())

    def test_production_junction_boundary_rejected(self):
        outside = self.tmp / "outside-production"
        outside.mkdir()
        workspace = self.fake_workspace()
        target = workspace / "runtime" / "a07-intake" / "downtime.sqlite3"
        with mock.patch.object(intake_store, "DEFAULT_DB_PATH", target):
            self.assertEqual(DowntimeStore().path, target)
            shutil.rmtree(workspace / "runtime" / "a07-intake")
            link = workspace / "runtime" / "a07-intake"
            if not make_directory_link(link, outside):
                self.skipTest("links are not available")
            try:
                self.assert_code("STORE_UNAVAILABLE", DowntimeStore)
                self.assertFalse((outside / "downtime.sqlite3").exists())
            finally:
                remove_directory_link(link)

    def test_production_junction_after_construction_rejected(self):
        outside = self.tmp / "outside-after"
        outside.mkdir()
        workspace = self.fake_workspace()
        target = workspace / "runtime" / "a07-intake" / "downtime.sqlite3"
        with mock.patch.object(intake_store, "DEFAULT_DB_PATH", target):
            store = DowntimeStore()
            shutil.rmtree(workspace / "runtime" / "a07-intake")
            link = workspace / "runtime" / "a07-intake"
            if not make_directory_link(link, outside):
                self.skipTest("links are not available")
            try:
                self.assert_code(
                    "STORE_UNAVAILABLE",
                    store.commit,
                    make_record(),
                    submission_key="junction-after",
                )
                self.assert_code(
                    "STORE_UNAVAILABLE",
                    store.list_window,
                    START,
                    END,
                )
                self.assertFalse((outside / "downtime.sqlite3").exists())
            finally:
                remove_directory_link(link)

    def test_test_root_junction_after_construction_rejected(self):
        outside = self.tmp / "outside-test-root"
        outside.mkdir()
        root = self.tmp / "injected-root"
        root.mkdir()
        path = root / "downtime.sqlite3"
        store = DowntimeStore(path, test_root=root)
        shutil.rmtree(root)
        if not make_directory_link(root, outside):
            self.skipTest("links are not available")
        try:
            self.assert_code(
                "STORE_UNAVAILABLE",
                store.commit,
                make_record(),
                submission_key="root-junction-after",
            )
            self.assertFalse((outside / "downtime.sqlite3").exists())
        finally:
            remove_directory_link(root)

    def test_production_fake_workspace_lexical_boundary_allows_normal_path(self):
        workspace = self.fake_workspace()
        target = workspace / "runtime" / "a07-intake" / "downtime.sqlite3"
        outside = self.tmp / "outside-normal"
        outside.mkdir()
        with mock.patch.object(intake_store, "DEFAULT_DB_PATH", target):
            store = DowntimeStore()
            receipt = store.commit(make_record(), submission_key="fake-production")
            self.assertFalse(receipt["replayed"])
            self.assertTrue(target.exists())
            self.assertFalse((outside / "downtime.sqlite3").exists())


class TestHardlinkAlias(StoreCase):
    def make_outside_store(self):
        root = self.tmp / "outside-root"
        root.mkdir()
        db = root / "downtime.sqlite3"
        store = DowntimeStore(db, test_root=root)
        store.commit(make_record(), submission_key="outside-seed")
        return root, db, store

    def outside_state(self, root, db):
        connection = sqlite3.connect(
            "file:%s?mode=ro" % Path(db).as_posix(), uri=True
        )
        try:
            rows = connection.execute(
                "SELECT COUNT(*) FROM downtime_events"
            ).fetchone()[0]
        finally:
            connection.close()
        return rows, sha256_file(db)

    def test_existing_hardlink_alias_rejected(self):
        outside_root, outside_db, _ = self.make_outside_store()
        inner = self.tmp / "inside-root"
        inner.mkdir()
        alias = inner / "alias.sqlite3"
        if not make_hardlink(alias, outside_db):
            self.skipTest("hard links are not available")
        before_rows, before_hash = self.outside_state(outside_root, outside_db)
        before_tree = tree_files(inner)
        self.assert_code("STORE_UNAVAILABLE", DowntimeStore, alias, test_root=inner)
        after_rows, after_hash = self.outside_state(outside_root, outside_db)
        self.assertEqual((after_rows, after_hash), (before_rows, before_hash))
        self.assertEqual(tree_files(inner), before_tree)
        self.assertTrue(alias.exists())

    def test_hardlink_alias_inserted_after_construction_rejected(self):
        outside_root, outside_db, _ = self.make_outside_store()
        inner = self.tmp / "inside-post"
        inner.mkdir()
        path = inner / "downtime.sqlite3"
        store = DowntimeStore(path, test_root=inner)
        if not make_hardlink(path, outside_db):
            self.skipTest("hard links are not available")
        before_rows, before_hash = self.outside_state(outside_root, outside_db)
        self.assert_code(
            "STORE_UNAVAILABLE",
            store.commit,
            make_record(),
            submission_key="hardlink-after",
        )
        self.assert_code("STORE_UNAVAILABLE", store.list_window, START, END)
        after_rows, after_hash = self.outside_state(outside_root, outside_db)
        self.assertEqual((after_rows, after_hash), (before_rows, before_hash))
        self.assertEqual(tree_files(inner), ["downtime.sqlite3"])

    def test_production_hardlink_alias_rejected(self):
        outside_root, outside_db, _ = self.make_outside_store()
        workspace = self.fake_workspace()
        target = workspace / "runtime" / "a07-intake" / "downtime.sqlite3"
        if not make_hardlink(target, outside_db):
            self.skipTest("hard links are not available")
        before_rows, before_hash = self.outside_state(outside_root, outside_db)
        before_tree = tree_files(workspace / "runtime" / "a07-intake")
        with mock.patch.object(intake_store, "DEFAULT_DB_PATH", target):
            self.assert_code("STORE_UNAVAILABLE", DowntimeStore)
        after_rows, after_hash = self.outside_state(outside_root, outside_db)
        self.assertEqual((after_rows, after_hash), (before_rows, before_hash))
        self.assertEqual(
            tree_files(workspace / "runtime" / "a07-intake"), before_tree
        )

    def test_normal_single_link_database_still_works(self):
        receipt = self.store.commit(make_record(), submission_key="single-link")
        self.assertFalse(receipt["replayed"])
        self.assertEqual(self.path.stat().st_nlink, 1)
        replay = self.store.commit(make_record(), submission_key="single-link")
        self.assertEqual(replay["record_id"], receipt["record_id"])


class TestListWindow(StoreCase):
    def setUp(self):
        super().setUp()
        self.inside = self.store.commit(
            make_record(), submission_key="window-inside"
        )["record_id"]
        self.before = self.store.commit(
            make_record(
                start_time="2026-11-04T07:00:00+08:00",
                end_time="2026-11-04T08:00:00+08:00",
            ),
            submission_key="window-before",
        )["record_id"]
        self.after = self.store.commit(
            make_record(
                start_time="2026-11-04T09:00:00+08:00",
                end_time="2026-11-04T10:00:00+08:00",
            ),
            submission_key="window-after",
        )["record_id"]
        self.spanning = self.store.commit(
            make_record(
                start_time="2026-11-04T07:30:00+08:00",
                end_time="2026-11-04T09:30:00+08:00",
                reason="跨窗",
            ),
            submission_key="window-spanning",
        )["record_id"]

    def test_columns_reuse_downtime_definition(self):
        result = self.store.list_window(START, END)
        self.assertEqual(
            result["columns"], describe_query("downtime_details")["columns"]
        )
        self.assertEqual(len(result["rows"][0]), len(result["columns"]))

    def test_half_open_intersection_and_stable_id_order(self):
        result = self.store.list_window(
            "2026-11-04T08:00:00+08:00", "2026-11-04T09:00:00+08:00"
        )
        ids = [row[0] for row in result["rows"]]
        self.assertEqual(ids, [self.inside, self.spanning])
        self.assertEqual(ids, sorted(ids))
        self.assertNotIn(self.before, ids)
        self.assertNotIn(self.after, ids)

    def test_empty_result(self):
        result = self.store.list_window(
            "2026-11-04T12:00:00+08:00", "2026-11-04T13:00:00+08:00"
        )
        self.assertEqual(result["rows"], [])
        self.assertFalse(result["truncated"])
        self.assertIsNone(result["truncation_reason"])

    def test_limit_and_truncation(self):
        result = self.store.list_window(
            "2026-11-04T00:00:00+08:00",
            "2026-11-04T23:59:59+08:00",
            limit=1,
        )
        self.assertEqual(len(result["rows"]), 1)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["truncation_reason"], "row_limit")
        wide = self.store.list_window(
            "2026-11-04T00:00:00+08:00", "2026-11-04T23:59:59+08:00", limit=500
        )
        self.assertEqual(len(wide["rows"]), 4)

    def test_invalid_windows(self):
        self.assert_code(
            "INVALID_RECORD", self.store.list_window, "bad", END
        )
        self.assert_code(
            "INVALID_TIME_ORDER", self.store.list_window, END, START
        )
        self.assert_code(
            "INVALID_RECORD", self.store.list_window, START, END, limit=0
        )

    def test_row_values_and_null_order(self):
        record_id = self.store.commit(
            make_record(order_id=None, reason="无工单"),
            submission_key="window-null-order",
        )["record_id"]
        rows = self.store.list_window(START, END)["rows"]
        row = [item for item in rows if item[0] == record_id][0]
        self.assertEqual(row[1], "M01")
        self.assertEqual(row[2], START)
        self.assertEqual(row[3], END)
        self.assertEqual(row[4], "无工单")
        self.assertIsNone(row[5])
        self.assertIsInstance(row[0], int)

    def test_default_path_is_isolated_from_mysql(self):
        self.assertEqual(intake_store.SANDBOX_ID, "intake-sandbox-v1")
        parts = intake_store.DEFAULT_DB_PATH.parts
        self.assertIn("a07-intake", parts)
        self.assertNotIn("mysql-a07", parts)


class TestSearchWindow(StoreCase):
    def _commit_many(self):
        ids = {}
        ids["m01"] = self.store.commit(
            make_record(), submission_key="search-m01"
        )["record_id"]
        ids["m02"] = self.store.commit(
            make_record(machine_no="2号机", reason="M02事件"),
            submission_key="search-m02",
        )["record_id"]
        ids["m03"] = self.store.commit(
            make_record(
                machine_no="M03",
                start_time="2026-11-04T09:00:00+08:00",
                end_time="2026-11-04T10:00:00+08:00",
                reason="M03事件",
            ),
            submission_key="search-m03",
        )["record_id"]
        return ids

    def test_record_id_lookup_without_window(self):
        ids = self._commit_many()
        result = self.store.search_window(record_id=ids["m02"])
        self.assertEqual(result["rows"], [result["rows"][0]])
        self.assertEqual(result["rows"][0][0], ids["m02"])
        self.assertEqual(result["rows"][0][1], "M02")
        self.assertEqual(result["source"], "intake_sqlite")
        self.assertEqual(result["schema_version"], "m5-intake-search-v1")
        self.assertIsInstance(result["snapshot_at"], str)
        self.assertEqual(
            result["filters"],
            {
                "window_start": None,
                "window_end": None,
                "machine_no": None,
                "record_id": ids["m02"],
                "limit": 200,
            },
        )
        self.assertFalse(result["truncated"])
        self.assertIsNone(result["truncation_reason"])
        self.assertEqual(result["columns"], describe_query("downtime_details")["columns"])

    def test_machine_filter_applies_before_limit(self):
        ids = self._commit_many()
        result = self.store.search_window(
            START, "2026-11-04T09:30:00+08:00", machine_no="M02", limit=1
        )
        self.assertEqual([row[0] for row in result["rows"]], [ids["m02"]])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["filters"]["machine_no"], "M02")
        self.assertEqual(result["filters"]["window_start"], START)
        self.assertEqual(result["filters"]["window_end"], "2026-11-04T09:30:00+08:00")

    def test_machine_alias_and_record_id_are_bound(self):
        ids = self._commit_many()
        by_id = self.store.search_window(record_id=ids["m01"], machine_no="一号机")
        self.assertEqual([row[0] for row in by_id["rows"]], [ids["m01"]])
        self.assertEqual(by_id["filters"]["machine_no"], "M01")
        other = self.store.search_window(record_id=ids["m01"], machine_no="M02")
        self.assertEqual(other["rows"], [])

    def test_half_open_equality_is_excluded_and_empty_is_clear(self):
        self.store.commit(
            make_record(
                start_time="2026-11-04T07:00:00+08:00",
                end_time=START,
            ),
            submission_key="search-touching-end",
        )
        self.store.commit(
            make_record(
                start_time=END,
                end_time="2026-11-04T10:00:00+08:00",
            ),
            submission_key="search-touching-start",
        )
        result = self.store.search_window(START, END)
        self.assertEqual(result["rows"], [])
        self.assertFalse(result["truncated"])
        self.assertIsNone(result["truncation_reason"])

    def test_limit_truncation_and_cap(self):
        for index in range(3):
            self.store.commit(
                make_record(
                    start_time="2026-11-04T0%d:00:00+08:00" % index,
                    end_time="2026-11-04T0%d:30:00+08:00" % index,
                ),
                submission_key="search-limit-%d" % index,
            )
        result = self.store.search_window("2026-11-04T00:00:00+08:00", "2026-11-04T03:00:00+08:00", limit=1)
        self.assertEqual(len(result["rows"]), 1)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["truncation_reason"], "row_limit")
        capped = self.store.search_window("2026-11-04T00:00:00+08:00", "2026-11-04T03:00:00+08:00", limit=500)
        self.assertLessEqual(capped["filters"]["limit"], 200)
        self.assertFalse(capped["truncated"])

    def test_parameter_validation(self):
        self.assert_code("INVALID_RECORD", self.store.search_window)
        self.assert_code(
            "INVALID_RECORD", self.store.search_window, START, None
        )
        self.assert_code(
            "INVALID_RECORD", self.store.search_window, None, END
        )
        self.assert_code(
            "INVALID_RECORD", self.store.search_window, START, END, record_id=True
        )
        self.assert_code(
            "INVALID_RECORD", self.store.search_window, START, END, record_id=0
        )
        self.assert_code(
            "INVALID_RECORD", self.store.search_window, START, END, limit=True
        )
        self.assert_code(
            "INVALID_RECORD", self.store.search_window, START, END, limit=0
        )
        self.assert_code(
            "INVALID_RECORD", self.store.search_window, START, END, machine_no="M99"
        )
        self.assert_code(
            "INVALID_TIME_ORDER", self.store.search_window, END, START
        )

    def test_missing_database_creates_no_file(self):
        missing = self.tmp / "missing.sqlite3"
        store = DowntimeStore(missing, test_root=self.tmp)
        self.assertFalse(missing.exists())
        self.assert_code("STORE_UNAVAILABLE", store.search_window, START, END)
        self.assertFalse(missing.exists())
        self.assertFalse(any(p.name.startswith("missing") for p in self.tmp.iterdir()))

    def test_read_only_does_not_change_database(self):
        ids = self._commit_many()
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        result = self.store.search_window(record_id=ids["m01"])
        after = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        self.assertEqual(result["rows"][0][0], ids["m01"])
        self.assertFalse((self.tmp / "downtime.sqlite3-journal").exists())
        self.assertFalse((self.tmp / "downtime.sqlite3-wal").exists())


if __name__ == "__main__":
    unittest.main()
