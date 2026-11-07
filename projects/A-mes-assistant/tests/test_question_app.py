import json
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from streamlit.testing.v1 import AppTest

from mes_assistant import ui_presenter
from mes_assistant.llm_client import ModelClientError

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
WORKSPACE = APP_PATH.parents[2]
TEST_LOG = WORKSPACE / "execution" / "A" / "A-008" / "test-tmp" / "question-ui-events.jsonl"
TEST_LOG.parent.mkdir(parents=True, exist_ok=True)
if TEST_LOG.exists():
    TEST_LOG.unlink()


@contextmanager
def open_test_log(_path):
    TEST_LOG.parent.mkdir(parents=True, exist_ok=True)
    with TEST_LOG.open("a", encoding="utf-8") as sink:
        yield sink


def question_response(request, *, status="ok", mode="mock", answer="候选解释。", error=None):
    table = None
    if status == "ok":
        table = {
            "columns": [
                {"name": "line_no", "type": "string", "nullable": False},
                {"name": "actual_qty", "type": "decimal", "nullable": False},
            ],
            "rows": [["L01", "205"], ["L02", "80"], ["L03", "0"]],
            "truncated": False,
            "truncation_reason": None,
        }
    return {
        "schema_version": "0.1",
        "run_id": "m2-ui-test",
        "request_id": request["request_id"],
        "case_id": request["case_id"],
        "data_version": "mes-a07-boundary-v1-pending",
        "reference_time": request["reference_time"],
        "timezone": "Asia/Shanghai",
        "mode": mode,
        "status": status,
        "answer": answer,
        "table": table,
        "sql": "SELECT /* mock */ 1;" if table else None,
        "metrics": [],
        "error": error,
        "timing": {"internal_ms": 1},
        "counts": {
            "model_calls": 1,
            "queries": 2 if table else None,
            "corrections": 0,
        },
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "config_id": "m2-ui",
        "query_id": "line_snapshot" if table else None,
        "review_status": "pending",
        "semantic_policy_version": "demo-metrics-v1-user-reviewed-20261103",
        "context_mode": "full",
        "context_evidence": {
            "context_mode": "full",
            "selected_query_ids": [
                "line_snapshot",
                "work_order_progress",
                "downtime_details",
            ],
            "matched_terms": [],
            "reason": "full",
            "semantic_policy_version": "demo-metrics-v1-user-reviewed-20261103",
            "registry_sha256": "a" * 64,
        },
        "plan": {"kind": "query", "query_id": "line_snapshot", "params": {}}
        if status == "ok"
        else None,
        "trace": [{"event_id": "model-1", "stage": "model", "status": "ok"}],
    }


class FakeQuestion:
    def __init__(self):
        self.calls = []
        self.status = "ok"
        self.mode = "mock"
        self.answer = "候选解释。"
        self.error = None

    def __call__(self, request, *, log_sink):
        self.calls.append(request["request_id"])
        log_sink.write(
            json.dumps(
                {"test": "question-app", "request_id": request["request_id"]},
                ensure_ascii=False,
            )
            + "\n"
        )
        log_sink.flush()
        return question_response(
            request,
            status=self.status,
            mode=self.mode,
            answer=self.answer,
            error=self.error,
        )


class QuestionAppTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeQuestion()
        self.patch = mock.patch.object(
            ui_presenter, "mock_question", self.fake
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.log_patch = mock.patch.object(
            ui_presenter, "open_log_sink", open_test_log
        )
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)
        self.at = AppTest.from_file(str(APP_PATH))
        self.at.run(timeout=20)

    def submit(self):
        self.at.form_submit_button[0].click()
        self.at.run(timeout=20)

    def test_load_is_offline_and_no_query(self):
        self.assertEqual(len(self.at.exception), 0)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.at.radio[0].value, "自然语言问答")
        self.assertTrue(any("输入中文问题" in item.value for item in self.at.info))

    def test_mock_success(self):
        self.submit()
        self.assertEqual(len(self.at.exception), 0)
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(len(self.at.dataframe), 1)
        self.assertEqual(self.at.dataframe[0].value.shape, (3, 2))
        self.assertEqual(len(self.at.error), 0)
        self.assertTrue(any("候选解释" in item.value for item in self.at.markdown))
        captions = " ".join(item.value for item in self.at.caption)
        self.assertIn("mode=mock", captions)
        self.assertIn("演示口径已确认", captions)
        self.assertIn("合成数据", captions)
        self.assertIn("line_snapshot", captions)
        self.assertEqual(len(self.at.form_submit_button), 1)

    def test_rerun_does_not_query_again(self):
        self.submit()
        self.assertEqual(len(self.fake.calls), 1)
        self.at.run(timeout=20)
        self.assertEqual(len(self.fake.calls), 1)

    def test_clarify_and_refuse_are_shown_as_warnings(self):
        self.fake.status = "needs_clarification"
        self.fake.answer = "请明确产线 line_id。"
        self.submit()
        self.assertEqual(len(self.at.warning), 1)
        self.assertIn("请明确产线", self.at.warning[0].value)
        self.assertEqual(len(self.at.dataframe), 0)

        fresh = AppTest.from_file(str(APP_PATH))
        fresh.run(timeout=20)
        self.fake.status = "refused"
        self.fake.answer = "超出范围。"
        fresh.form_submit_button[0].click()
        fresh.run(timeout=20)
        self.assertTrue(any("超出范围" in item.value for item in fresh.warning))
        self.assertEqual(len(fresh.dataframe), 0)

    def test_error_does_not_claim_real_success(self):
        self.fake.status = "error"
        self.fake.mode = "real"
        self.fake.answer = "失败"
        self.fake.error = {
            "code": "MODEL_FAILED",
            "stage": "model",
            "message": "模型失败",
            "retryable": False,
        }
        self.submit()
        self.assertEqual(len(self.at.exception), 0)
        self.assertEqual(len(self.at.error), 1)
        self.assertEqual(len(self.at.dataframe), 0)
        captions = " ".join(item.value for item in self.at.caption)
        self.assertIn("请求配置 mode=real", captions)
        self.assertNotIn("本次来自真实", captions)

    def test_named_query_entry_is_preserved(self):
        self.at.radio[0].set_value("命名查询").run(timeout=20)
        self.assertEqual(len(self.at.exception), 0)
        self.assertEqual(len(self.at.form_submit_button), 1)
        self.assertTrue(any("命名查询" in item.value for item in self.at.subheader))

    def test_real_config_missing_is_explicit(self):
        self.at.radio[1].set_value("real")
        with mock.patch.object(
            ui_presenter,
            "real_question",
            side_effect=ModelClientError("CONFIG_MISSING", "missing"),
        ):
            self.at.form_submit_button[0].click()
            self.at.run(timeout=20)
        self.assertEqual(len(self.at.exception), 0)
        self.assertEqual(len(self.at.error), 1)
        self.assertIn("MODEL_CONFIG_MISSING", self.at.error[0].value)
        captions = " ".join(item.value for item in self.at.caption)
        self.assertIn("请求配置 mode=real", captions)
        self.assertNotIn("本次来自真实", captions)


if __name__ == "__main__":
    unittest.main()
