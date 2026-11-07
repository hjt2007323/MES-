import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from streamlit.testing.v1 import AppTest

from mes_assistant import intake_ui, intake_runtime
from mes_assistant.intake_store import DowntimeStore, IntakeError, PRODUCTION_DB_PATH
from mes_assistant.llm_client import ModelClientError, ModelReply

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
WORKSPACE = APP_PATH.parents[2]
TMP = WORKSPACE / "execution" / "A" / "A-010" / "revisions" / "v1.3" / "test-tmp" / "app"


class StaticModel:
    def __init__(self, content):
        self.content = content
        self.calls = 0

    def complete(self, messages, *, timeout_ms):
        self.calls += 1
        return ModelReply(content=self.content, usage={"prompt_tokens": 1, "completion_tokens": 1})


class FlakyStore:
    def __init__(self, delegate):
        self.delegate = delegate
        self.fail_read = True

    def commit(self, record, *, submission_key):
        return self.delegate.commit(record, submission_key=submission_key)

    def list_window(self, window_start, window_end, *, limit=200):
        if self.fail_read:
            self.fail_read = False
            raise IntakeError("STORE_UNAVAILABLE", "unavailable")
        return self.delegate.list_window(window_start, window_end, limit=limit)


class FailingStore:
    def commit(self, record, *, submission_key):
        raise IntakeError("STORE_UNAVAILABLE", "unavailable")

    def list_window(self, window_start, window_end, *, limit=200):
        raise IntakeError("STORE_UNAVAILABLE", "unavailable")


class IntakeAppTests(unittest.TestCase):
    def setUp(self):
        TMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=str(TMP))
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "intake.sqlite3"
        self.store = DowntimeStore(self.db_path, test_root=TMP)
        self.override = None
        self.patch = mock.patch.object(intake_ui, "_session_store", self._active_store)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.at = AppTest.from_file(str(APP_PATH))
        self.at.run(timeout=20)
        self.at.radio[0].set_value("停机录入（合成沙箱）").run(timeout=20)
        self.assertEqual(len(self.at.exception), 0)

    def _active_store(self):
        return self.override or self.store

    def _button(self, label):
        matches = [button for button in self.at.button if button.label == label]
        self.assertEqual(len(matches), 1, label)
        return matches[0]

    def _generate(self, model=None, text=None):
        if text is not None:
            self.at.text_area[0].set_value(text)
        if model is None:
            self._button("生成草稿").click()
            self.at.run(timeout=20)
        else:
            with mock.patch.object(intake_ui, "_session_model", lambda: model):
                self._button("生成草稿").click()
                self.at.run(timeout=20)

    def _field(self, label):
        matches = [item for item in self.at.text_input if item.label == label]
        self.assertEqual(len(matches), 1, label)
        return matches[0]

    def _mode(self, value):
        matches = [item for item in self.at.radio if item.label == "运行模式"]
        self.assertEqual(len(matches), 1)
        matches[0].set_value(value)
        self.at.run(timeout=20)

    def _rows(self):
        return self.store.list_window(
            "2026-11-04T00:00:00+08:00", "2026-11-05T00:00:00+08:00"
        )["rows"]

    def _extract_model(self, *, reason_value="换型", reason_quote="换型"):
        text = self.at.text_area[0].value
        fields = {
            "machine_no": {"value": "M02", "quote": "M02"},
            "start_time": {
                "value": "2026-11-04T09:00:00+08:00",
                "quote": "2026-11-04T09:00:00+08:00",
            },
            "end_time": {
                "value": "2026-11-04T09:12:00+08:00",
                "quote": "2026-11-04T09:12:00+08:00",
            },
            "reason": {"value": reason_value, "quote": reason_quote},
            "order_id": {"value": "WO03", "quote": "WO03"},
        }
        return StaticModel(json.dumps({"kind": "extract", "fields": fields}, ensure_ascii=False))

    def test_default_store_path_is_workspace_production(self):
        self.assertEqual(intake_ui.DEFAULT_DB_PATH, PRODUCTION_DB_PATH)
        self.assertEqual(
            intake_ui.DEFAULT_DB_PATH,
            WORKSPACE / "runtime" / "a07-intake" / "downtime.sqlite3",
        )

    def test_load_draft_has_no_write_or_extra_model_call_on_rerun(self):
        self._generate()
        self.assertEqual(len(self.at.exception), 0)
        self.assertEqual(len(self.at.success), 1)
        self.assertEqual(self.at.session_state["intake_model"].calls, 1)
        self.assertEqual(len(self._rows()), 0)
        captions = " ".join(item.value for item in self.at.caption)
        self.assertIn("machine_no：M02", captions)
        self.assertIn("原文=M02", captions)
        self.assertIn("reason：换型", captions)
        self.assertIn("mode=offline", captions)
        self.assertIn("source=offline_template", captions)
        self.assertEqual(len(self.at.text_input), 8)
        self.at.run(timeout=20)
        self.assertEqual(self.at.session_state["intake_model"].calls, 1)
        self.assertEqual(len(self._rows()), 0)

    def test_mode_switch_invalidates_old_draft(self):
        self._generate()
        old_id = self.at.session_state["intake_draft"]["draft_id"]
        calls = self.at.session_state["intake_model"].calls
        self._mode("real")
        self.assertEqual(self.at.session_state["intake_draft"]["draft_id"], old_id)
        self.assertEqual(self.at.session_state["intake_model"].calls, calls)
        self.assertTrue(any("运行模式已变更" in item.value for item in self.at.warning))
        self.assertEqual([b for b in self.at.button if b.label == "确认并写入"], [])
        self.assertEqual(self._rows(), [])

    def test_real_config_missing_is_zero_dispatch(self):
        self._mode("real")
        with mock.patch.object(
            intake_runtime.ModelConfig,
            "from_env",
            side_effect=ModelClientError("CONFIG_MISSING", "missing"),
        ):
            self._button("生成草稿").click()
            self.at.run(timeout=20)
        draft = self.at.session_state["intake_draft"]
        self.assertEqual(draft["state"], "error")
        self.assertEqual(draft["run_meta"]["client_calls"], 0)
        self.assertEqual(draft["run_meta"]["execution_source"], "http_model")
        self.assertEqual(draft["run_meta"]["error_code"], "CONFIG_MISSING")
        self.assertTrue(any("error=CONFIG_MISSING" in item.value for item in self.at.caption))
        self.assertEqual([b for b in self.at.button if b.label == "确认并写入"], [])
        self.assertEqual(self._rows(), [])

    def test_confirm_then_duplicate_reuses_record_and_reads_back(self):
        self._generate()
        self._button("确认并写入").click()
        self.at.run(timeout=20)
        self.assertEqual(len(self._rows()), 1)
        self.assertTrue(any("record_id=1" in item.value for item in self.at.success))
        self.assertTrue(any("draft=" in item.value for item in self.at.success))
        self.assertTrue(any("source=offline_template" in item.value for item in self.at.success))
        self.assertGreaterEqual(len(self.at.dataframe), 1)
        self.assertIn("查回状态：ok", " ".join(item.value for item in self.at.caption))

        self._button("确认并写入").click()
        self.at.run(timeout=20)
        self.assertEqual(len(self._rows()), 1)
        self.assertTrue(any("replayed=True" in item.value for item in self.at.success))

    def test_unvalidated_edit_is_blocked_then_revalidated(self):
        self._generate()
        self._field("reason").set_value("卡料")
        self._button("确认并写入").click()
        self.at.run(timeout=20)
        self.assertEqual(self._rows(), [])
        self.assertTrue(any("确认被阻止" in item.value for item in self.at.error))
        self._button("重新校验").click()
        self.at.run(timeout=20)
        self._button("确认并写入").click()
        self.at.run(timeout=20)
        self.assertEqual(self._rows()[0][4], "卡料")
        self.assertTrue(any("卡料" in str(item.value) for item in self.at.success))

    def test_two_complete_templates_are_ambiguous_and_not_written(self):
        two = (
            "machine_no=M02；start_time=2026-11-04T09:00:00+08:00；"
            "end_time=2026-11-04T09:12:00+08:00；reason=换型；order_id=WO03；"
            "machine_no=M01；start_time=2026-11-04T10:00:00+08:00；"
            "end_time=2026-11-04T10:15:00+08:00；reason=故障；order_id=WO01"
        )
        self._generate(text=two)
        self.assertTrue(self.at.session_state["intake_draft"]["terminal"])
        self.assertEqual([b for b in self.at.button if b.label == "确认并写入"], [])
        self.assertEqual(self._rows(), [])

    def test_duplicate_field_is_ambiguous_and_not_written(self):
        duplicate = (
            "machine_no=M02；start_time=2026-11-04T09:00:00+08:00；"
            "end_time=2026-11-04T09:12:00+08:00；reason=换型；reason=卡料；"
            "order_id=WO03"
        )
        self._generate(text=duplicate)
        self.assertTrue(self.at.session_state["intake_draft"]["terminal"])
        self.assertEqual([b for b in self.at.button if b.label == "确认并写入"], [])
        self.assertEqual(self._rows(), [])

    def test_empty_duplicate_reason_is_ambiguous_and_not_written(self):
        base = (
            "machine_no=M02；start_time=2026-11-04T09:00:00+08:00；"
            "end_time=2026-11-04T09:12:00+08:00；reason=%s；order_id=WO03"
        )
        for reason in ("；reason=换型", "换型；reason=", "；reason="):
            with self.subTest(reason=reason):
                self._generate(text=base % reason)
                self.assertTrue(self.at.session_state["intake_draft"]["terminal"])
                self.assertEqual(
                    [b for b in self.at.button if b.label == "确认并写入"], []
                )
                self.assertEqual(self._rows(), [])

    def test_single_optional_empty_is_null_and_confirmable(self):
        text = (
            "machine_no=M02；start_time=2026-11-04T09:00:00+08:00；"
            "end_time=2026-11-04T09:12:00+08:00；reason=换型；order_id="
        )
        self._generate(text=text)
        self.assertEqual(self.at.session_state["intake_draft"]["state"], "ready")
        self.assertIsNone(self.at.session_state["intake_draft"]["record"]["order_id"])
        self._button("确认并写入").click()
        self.at.run(timeout=20)
        self.assertEqual(len(self._rows()), 1)
        self.assertIsNone(self._rows()[0][5])

    def test_edit_marks_user_and_revalidates(self):
        self._generate()
        self._field("reason").set_value("卡料")
        self._button("重新校验").click()
        self.at.run(timeout=20)
        self.assertTrue(any("人工修改字段：reason" in item.value for item in self.at.caption))
        self._button("确认并写入").click()
        self.at.run(timeout=20)
        self.assertEqual(self._rows()[0][4], "卡料")

    def test_same_draft_changed_payload_conflicts(self):
        self._generate()
        self._button("确认并写入").click()
        self.at.run(timeout=20)
        self._field("reason").set_value("卡料")
        self._button("重新校验").click()
        self.at.run(timeout=20)
        self._button("确认并写入").click()
        self.at.run(timeout=20)
        self.assertTrue(any("SUBMISSION_CONFLICT" in item.value for item in self.at.error))
        self.assertEqual(len(self._rows()), 1)
        self.assertEqual(self._rows()[0][4], "换型")

    def test_store_failure_is_explicit(self):
        self._generate()
        self.override = FailingStore()
        self._button("确认并写入").click()
        self.at.run(timeout=20)
        self.assertTrue(any("STORE_UNAVAILABLE" in item.value for item in self.at.error))
        self.assertEqual(len(self._rows()), 0)

    def test_editable_window_readback(self):
        self._generate()
        self._button("确认并写入").click()
        self.at.run(timeout=20)
        self._field("window_start").set_value("2026-11-04T00:00:00+08:00")
        self._field("window_end").set_value("2026-11-05T00:00:00+08:00")
        self._button("查回").click()
        self.at.run(timeout=20)
        self.assertGreaterEqual(len(self.at.dataframe), 1)

    def test_simulated_old_ready_multi_event_blocks_confirm(self):
        fixture = json.loads(
            (Path(__file__).resolve().parent / "fixtures" / "intake_live_failure_regressions.json").read_text(
                encoding="utf-8"
            )
        )
        for case_id in ("i09", "same-device-two-events"):
            with self.subTest(case_id=case_id):
                case = next(item for item in fixture["cases"] if item["case_id"] == case_id)
                self.at.text_area[0].set_value(case["text"])
                fields = case["model_action"]["fields"]
                record = {field: fields[field]["value"] for field in fields}
                self.at.session_state["intake_draft"] = {
                    "draft_id": "reconstructed-%s-ready" % case_id,
                    "state": "ready",
                    "record": record,
                    "evidence": {field: None for field in fields},
                    "issues": [],
                    "mode": "model",
                    "terminal": False,
                    "pending_fields": [],
                    "sources": {field: "model" for field in fields},
                    "source_text": case["text"],
                    "source_reference": "2026-11-04T12:00:00+08:00",
                    "source_mode": "offline",
                    "run_meta": {
                        "mode": "offline",
                        "execution_source": "reconstructed_test_double",
                        "provider": None,
                        "model": None,
                        "client_calls": 1,
                        "elapsed_ms": 1,
                        "usage": None,
                        "error_code": None,
                    },
                }
                self.at.run(timeout=20)
                self._button("确认并写入").click()
                self.at.run(timeout=20)
                self.assertEqual(self._rows(), [])
                self.assertTrue(any("确认被阻止" in item.value for item in self.at.error))

    def test_multiple_event_is_terminal(self):
        model = StaticModel('{"kind":"clarify","issue":"MULTIPLE_EVENTS"}')
        self._generate(model=model)
        self.assertTrue(self.at.session_state["intake_draft"]["terminal"])
        self.assertEqual([b for b in self.at.button if b.label == "确认并写入"], [])
        self.assertEqual([b for b in self.at.button if b.label == "重新校验"], [])
        self.assertEqual(self._rows(), [])

    def test_fake_evidence_stays_pending_when_revalidated_unchanged(self):
        model = self._extract_model(reason_value="故障", reason_quote="不存在")
        self._generate(model=model)
        self.assertEqual(self._field("reason").value, "")
        self.assertIn("reason", self.at.session_state["intake_draft"]["pending_fields"])
        self.assertEqual([b for b in self.at.button if b.label == "确认并写入"], [])
        self._button("重新校验").click()
        self.at.run(timeout=20)
        self.assertIn("reason", self.at.session_state["intake_draft"]["pending_fields"])
        self.assertEqual([b for b in self.at.button if b.label == "确认并写入"], [])
        self.assertEqual(self._rows(), [])

    def test_changed_source_blocks_old_draft(self):
        self._generate()
        old_id = self.at.session_state["intake_draft"]["draft_id"]
        self.at.text_area[0].set_value("M01 8:00 停机，8:10 恢复。")
        self.at.run(timeout=20)
        self.assertEqual(self.at.session_state["intake_draft"]["draft_id"], old_id)
        self.assertTrue(any("旧草稿不可确认" in item.value for item in self.at.warning))
        self.assertEqual([b for b in self.at.button if b.label == "确认并写入"], [])
        self.assertEqual(self._rows(), [])

    def test_changed_reference_blocks_old_draft(self):
        self._generate()
        old_id = self.at.session_state["intake_draft"]["draft_id"]
        self._field("reference_time").set_value("2026-11-04T13:00:00+08:00")
        self.at.run(timeout=20)
        self.assertEqual(self.at.session_state["intake_draft"]["draft_id"], old_id)
        self.assertTrue(any("旧草稿不可确认" in item.value for item in self.at.warning))
        self.assertEqual([b for b in self.at.button if b.label == "确认并写入"], [])
        self.assertEqual(self._rows(), [])

    def test_optional_order_can_be_explicitly_cleared(self):
        self._generate()
        self._field("order_id").set_value("")
        self._button("重新校验").click()
        self.at.run(timeout=20)
        self._button("确认并写入").click()
        self.at.run(timeout=20)
        self.assertIsNone(self._rows()[0][5])
        self.assertTrue(any("人工修改字段：order_id" in item.value for item in self.at.caption))

    def test_new_draft_resets_old_field_values(self):
        self._generate()
        old_id = self.at.session_state["intake_draft"]["draft_id"]
        self._field("reason").set_value("卡料")
        self._generate()
        new_id = self.at.session_state["intake_draft"]["draft_id"]
        self.assertNotEqual(old_id, new_id)
        self.assertEqual(self._field("reason").value, "换型")
        self.assertEqual(self._rows(), [])

    def test_missing_required_field_can_be_filled_and_confirmed(self):
        model = self._extract_model(reason_value=None, reason_quote=None)
        self._generate(model=model)
        self.assertEqual(self._field("reason").value, "")
        self._field("reason").set_value("卡料")
        self._button("重新校验").click()
        self.at.run(timeout=20)
        self._button("确认并写入").click()
        self.at.run(timeout=20)
        self.assertEqual(self._rows()[0][4], "卡料")

    def test_readback_failure_can_retry(self):
        flaky = FlakyStore(self.store)
        self._generate()
        self.override = flaky
        self._button("确认并写入").click()
        self.at.run(timeout=20)
        self.assertTrue(any("查回失败" in item.value for item in self.at.warning))
        self._button("查回").click()
        self.at.run(timeout=20)
        self.assertIn("查回状态：ok", " ".join(item.value for item in self.at.caption))

    def test_ambiguous_input_has_no_confirm(self):
        self.at.text_area[0].set_value("只写了换型，没有设备或时间")
        self._generate()
        self.assertGreaterEqual(len(self.at.warning), 1)
        self.assertEqual(self._rows(), [])
        self.assertEqual([b for b in self.at.button if b.label == "确认并写入"], [])


if __name__ == "__main__":
    unittest.main()
