import tempfile
import unittest
from pathlib import Path
from unittest import mock

from streamlit.testing.v1 import AppTest

from mes_assistant import workspace_ui
from mes_assistant.intake_store import DowntimeStore

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
TMP = Path(tempfile.gettempdir()) / "a07-workspace-tests" / "app"


class WorkspaceAppTests(unittest.TestCase):
    def setUp(self):
        TMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=str(TMP))
        self.addCleanup(self.temp.cleanup)
        self.store = DowntimeStore(
            Path(self.temp.name) / "workspace.sqlite3", test_root=TMP
        )
        self.store._connect().close()
        self.patch = mock.patch.object(workspace_ui, "_session_store", lambda: self.store)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.at = AppTest.from_file(str(APP_PATH))
        self.at.run(timeout=20)
        self.at.radio[0].set_value("统一工作台（v2）").run(timeout=20)
        self.assertEqual(len(self.at.exception), 0)

    def _button(self, label):
        matches = [button for button in self.at.button if button.label == label]
        self.assertEqual(len(matches), 1, label)
        return matches[0]

    def test_prepare_confirm_then_query_last_record(self):
        text = (
            "录入 machine_no=M02；start_time=2026-11-04T09:00:00+08:00；"
            "end_time=2026-11-04T09:12:00+08:00；reason=换型；order_id=WO03"
        )
        self.at.text_area[0].set_value(text)
        self._button("运行").click()
        self.at.run(timeout=20)
        self.assertTrue(any("action=prepare_intake" in item.value for item in self.at.caption))
        self._button("确认写入").click()
        self.at.run(timeout=20)
        self.assertTrue(any("record_id=1" in item.value for item in self.at.success))
        self.at.text_area[0].set_value("刚录入的停机记录")
        self._button("运行").click()
        self.at.run(timeout=20)
        self.assertTrue(any("source=intake_sqlite" in item.value for item in self.at.caption))
        self.assertGreaterEqual(len(self.at.dataframe), 1)

    def test_changed_input_invalidates_old_ready(self):
        text = (
            "录入 machine_no=M01；start_time=2026-11-04T08:00:00+08:00；"
            "end_time=2026-11-04T08:10:00+08:00；reason=故障；order_id=WO01"
        )
        self.at.text_area[0].set_value(text)
        self._button("运行").click()
        self.at.run(timeout=20)
        self.at.text_area[0].set_value(
            "录入 machine_no=M02；start_time=2026-11-04T09:00:00+08:00；"
            "end_time=2026-11-04T09:10:00+08:00；reason=故障；order_id=WO02"
        )
        self._button("确认写入").click()
        self.at.run(timeout=20)
        self.assertEqual(
            self.store.search_window(
                "2026-11-04T00:00:00+08:00", "2026-11-05T00:00:00+08:00"
            )["rows"],
            [],
        )
        self.assertTrue(any("STALE_DRAFT" in item.value for item in self.at.error))

    def test_queue_generates_multiple_independent_drafts(self):
        self.at.text_area[1].set_value(
            "machine_no=M02；start_time=2026-11-04T09:00:00+08:00；"
            "end_time=2026-11-04T09:12:00+08:00；reason=换型；order_id=WO03\n"
            "无法识别的第二行"
        )
        self._button("生成队列草稿").click()
        self.at.run(timeout=20)
        queue = self.at.session_state["workspace_queue"]
        self.assertEqual(len(queue), 2)
        self.assertEqual(queue[0]["status"], "success")
        self.assertEqual(queue[1]["status"], "needs_clarification")
        self.assertNotEqual(
            queue[0]["draft"]["draft_id"], queue[1]["draft"]["draft_id"]
        )


if __name__ == "__main__":
    unittest.main()
