import tempfile
import unittest
from pathlib import Path

from mes_assistant.intake_store import DowntimeStore
from mes_assistant.workspace_assistant import WorkspaceService, WorkspaceError

TMP = Path(tempfile.gettempdir()) / "a07-workspace-tests" / "service"
REFERENCE = "2026-11-04T12:00:00+08:00"
TEXT = (
    "machine_no=M02；start_time=2026-11-04T09:00:00+08:00；"
    "end_time=2026-11-04T09:12:00+08:00；reason=换型；order_id=WO03"
)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        TMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=str(TMP))
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "workspace.sqlite3"
        self.store = DowntimeStore(self.db, test_root=TMP)
        self.store._connect().close()
        self.service = WorkspaceService(self.store)

    def _request(self, question, *, last=None):
        return {
            "request_id": "req",
            "session_id": "session",
            "question": question,
            "reference_time": REFERENCE,
            "timezone": "Asia/Shanghai",
            "mode": "offline",
            "last_confirmed_record_id": last,
        }

    def _prepare_confirm(self):
        result = self.service.run(self._request("录入 " + TEXT))
        self.assertEqual(result["action"], "prepare_intake")
        self.assertEqual(result["status"], "success")
        receipt = self.service.confirm(result["draft"])
        return result, receipt

    def test_prepare_is_draft_only_then_confirm_and_query_by_id(self):
        result, receipt = self._prepare_confirm()
        self.assertEqual(receipt["replayed"], False)
        self.assertEqual(len(self.store.search_window("2026-11-04T00:00:00+08:00", "2026-11-05T00:00:00+08:00")["rows"]), 1)
        query = self.service.run(self._request("刚录入的停机记录", last=receipt["record_id"]))
        self.assertEqual(query["status"], "success")
        self.assertEqual(query["action"], "intake_query")
        self.assertEqual(query["source"], "intake_sqlite")
        self.assertEqual(len(query["table"]["rows"]), 1)
        self.assertEqual(query["record_id"], 1)

    def test_confirmation_is_the_only_write_and_replay_same_id(self):
        result, first = self._prepare_confirm()
        before = self.store.search_window("2026-11-04T00:00:00+08:00", "2026-11-05T00:00:00+08:00")
        second = self.service.confirm(result["draft"])
        after = self.store.search_window("2026-11-04T00:00:00+08:00", "2026-11-05T00:00:00+08:00")
        self.assertEqual(first["record_id"], second["record_id"])
        self.assertTrue(second["replayed"])
        self.assertEqual(len(before["rows"]), len(after["rows"]))

    def test_today_window_and_empty_result(self):
        # B search_window is read-only; initialize an empty table for this test.
        self.store._connect().close()
        result = self.service.run(self._request("查询今天的停机记录"))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["source"], "intake_sqlite")
        self.assertEqual(result["table"]["rows"], [])
        self._prepare_confirm()
        result = self.service.run(self._request("查询今天的停机记录"))
        self.assertEqual(len(result["table"]["rows"]), 1)

    def test_missing_window_and_missing_last_record_clarify(self):
        no_window = self.service.run(self._request("查询停机记录"))
        self.assertEqual(no_window["status"], "needs_clarification")
        self.assertIn("MISSING_WINDOW", no_window["issues"])
        no_last = self.service.run(self._request("刚录入的停机记录"))
        self.assertEqual(no_last["status"], "needs_clarification")
        self.assertIn("NO_CONFIRMED_RECORD", no_last["issues"])

    def test_device_ambiguity_clarifies(self):
        result = self.service.run(self._request("查询今天M01和M02的停机记录"))
        self.assertEqual(result["status"], "needs_clarification")
        self.assertIn("MULTIPLE_MACHINES", result["issues"])

    def test_unknown_machine_is_not_dropped(self):
        result = self.service.run(self._request("查询今天M99的停机记录"))
        self.assertEqual(result["status"], "needs_clarification")
        self.assertIn("UNKNOWN_MACHINE", result["issues"])
        self.assertNotEqual(result.get("source"), "intake_sqlite")

    def test_changed_source_rejects_old_ready_draft(self):
        result = self.service.run(self._request("录入 " + TEXT))
        self.assertEqual(result["draft"]["state"], "ready")
        with self.assertRaises(WorkspaceError) as caught:
            self.service.confirm(
                result["draft"],
                session_id="session",
                source_text="machine_no=M03；start_time=2026-11-04T10:00:00+08:00；"
                "end_time=2026-11-04T10:10:00+08:00；reason=换型",
            )
        self.assertEqual(caught.exception.code, "STALE_DRAFT")
        self.assertEqual(self.store.search_window("2026-11-04T00:00:00+08:00", "2026-11-05T00:00:00+08:00")["rows"], [])

    def test_returned_draft_mutation_is_rejected(self):
        result = self.service.run(self._request("录入 " + TEXT))
        self.assertEqual(result["draft"]["record"]["machine_no"], "M02")
        result["draft"]["record"]["machine_no"] = "M01"
        with self.assertRaises(WorkspaceError) as caught:
            self.service.confirm(
                result["draft"],
                session_id="session",
                source_text="录入 " + TEXT,
            )
        self.assertEqual(caught.exception.code, "DRAFT_VERSION_MISMATCH")
        self.assertEqual(
            self.store.search_window(
                "2026-11-04T00:00:00+08:00", "2026-11-05T00:00:00+08:00"
            )["rows"],
            [],
        )

    def test_confirm_rejects_unregistered_ready_dict(self):
        fake = {
            "draft_id": "forged",
            "state": "ready",
            "record": {
                "machine_no": "M01",
                "start_time": "2026-11-04T08:00:00+08:00",
                "end_time": "2026-11-04T08:10:00+08:00",
                "reason": "故障",
                "order_id": None,
            },
        }
        with self.assertRaises(WorkspaceError) as caught:
            self.service.confirm(fake)
        self.assertEqual(caught.exception.code, "DRAFT_NOT_REGISTERED")

    def test_real_history_never_falls_back_to_mock(self):
        class ForbiddenModel:
            def complete(self, messages, *, timeout_ms):
                raise RuntimeError("must not dispatch")

        service = WorkspaceService(self.store, model_client=ForbiddenModel())
        request = self._request("查询3号产线的工单完成情况")
        request["mode"] = "real"
        result = service.run(request)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["source"], "history_mysql")
        self.assertIsNone(result["execution_source"])
        self.assertNotEqual(result["status"], "success")

    def test_real_history_uses_model_plan_not_mock_defaults(self):
        from types import SimpleNamespace
        from mes_assistant.question_fakes import MockTool

        class PlanModel:
            def complete(self, messages, *, timeout_ms):
                return SimpleNamespace(
                    content='{"action":"query","query_id":"work_order_progress","params":{"line_id":3}}',
                    usage=None,
                )

        service = WorkspaceService(
            self.store, model_client=PlanModel(), history_tool=MockTool()
        )
        request = self._request("查询3号产线的工单完成情况")
        request["mode"] = "real"
        result = service.run(request)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["filters"], {"line_id": 3})
        self.assertEqual(result["execution_source"], "http_model")
        self.assertEqual(result["model_calls"], 1)

    def test_offline_history_uses_explicit_line_three(self):
        result = self.service.run(self._request("查询3号产线的工单完成情况"))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["filters"], {"line_id": 3})
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(result["execution_source"], "test_double")

    def test_history_query_delegates_controlled_runtime(self):
        result = self.service.run(self._request("列出各产线当前累计实际产量快照（不按日期）"))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["action"], "history_query")
        self.assertEqual(result["source"], "history_mysql")
        self.assertIsNotNone(result["table"])
        self.assertEqual(result["query_calls"], 1)
        self.assertEqual(result["model_calls"], 0)

    def test_queue_keeps_independent_drafts(self):
        text = "\n".join([TEXT, "这一行无法识别", TEXT])
        drafts = self.service.prepare_queue(text, request=self._request("queue"))
        self.assertEqual(len(drafts), 3)
        self.assertEqual(drafts[0]["draft"]["state"], "ready")
        self.assertEqual(drafts[1]["status"], "needs_clarification")
        self.assertEqual(drafts[2]["draft"]["state"], "ready")
        for item in drafts:
            self.assertIsNotNone(item["draft"]["draft_id"])

    def test_queue_limit_is_fixed(self):
        text = "\n".join(["M01 8:00到8:10故障停机"] * 11)
        with self.assertRaises(WorkspaceError) as caught:
            self.service.prepare_queue(text, request=self._request("queue"))
        self.assertEqual(caught.exception.code, "QUEUE_LIMIT")


if __name__ == "__main__":
    unittest.main()
