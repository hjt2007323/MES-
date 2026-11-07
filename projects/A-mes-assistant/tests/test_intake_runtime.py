import json
import unittest

from mes_assistant.intake_extraction import extract_draft
from mes_assistant.intake_runtime import OfflineIntakeModel, run_intake_request
from mes_assistant.llm_client import ModelClientError, ModelReply

TEXT = (
    "machine_no=M02；start_time=2026-11-04T09:00:00+08:00；"
    "end_time=2026-11-04T09:12:00+08:00；reason=换型；order_id=WO03"
)
REFERENCE = "2026-11-04T12:00:00+08:00"


class WrappedDouble:
    def __init__(self, *, usage=None):
        self.inner = OfflineIntakeModel()
        self.calls = 0
        self.usage = usage

    def complete(self, messages, *, timeout_ms):
        self.calls += 1
        reply = self.inner.complete(messages, timeout_ms=timeout_ms)
        return ModelReply(content=reply.content, usage=self.usage)


class ErrorDouble:
    def __init__(self, code):
        self.code = code
        self.calls = 0

    def complete(self, messages, *, timeout_ms):
        self.calls += 1
        raise ModelClientError(self.code, "fixed")


class IntakeRuntimeTests(unittest.TestCase):
    def test_default_offline_uses_template_and_no_billing_usage(self):
        result = run_intake_request(TEXT, reference_time=REFERENCE)
        self.assertEqual(result["draft"]["state"], "ready")
        self.assertEqual(result["run"]["mode"], "offline")
        self.assertEqual(result["run"]["execution_source"], "offline_template")
        self.assertEqual(result["run"]["client_calls"], 1)
        self.assertIsNone(result["run"]["usage"])
        self.assertIsNone(result["run"]["provider"])
        self.assertIsNone(result["run"]["model"])
        self.assertIsNone(result["run"]["error_code"])

    def test_multi_event_text_is_stopped_before_model_call(self):
        result = run_intake_request(
            "今天M01 8:00到8:10故障停机；M02 9:00到9:10卡料停机。",
            reference_time=REFERENCE,
            mode="offline",
        )
        self.assertEqual(result["draft"]["issues"], ["MULTIPLE_EVENTS"])
        self.assertTrue(result["draft"]["terminal"])
        self.assertEqual(result["run"]["client_calls"], 0)

    def test_injected_double_is_test_double_with_observed_usage(self):
        model = WrappedDouble(usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})
        result = run_intake_request(
            TEXT, reference_time=REFERENCE, mode="offline", model_client=model
        )
        self.assertEqual(result["run"]["execution_source"], "test_double")
        self.assertEqual(result["run"]["client_calls"], 1)
        self.assertEqual(
            result["run"]["usage"],
            {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        )
        self.assertEqual(model.calls, 1)

    def test_real_without_config_is_fixed_zero_call(self):
        result = run_intake_request(
            TEXT, reference_time=REFERENCE, mode="real", env={}
        )
        self.assertEqual(result["draft"]["state"], "error")
        self.assertTrue(result["draft"]["terminal"])
        self.assertEqual(result["run"]["error_code"], "CONFIG_MISSING")
        self.assertEqual(result["run"]["client_calls"], 0)
        self.assertEqual(result["run"]["execution_source"], "http_model")
        self.assertNotIn("SILICONFLOW_API_KEY", json.dumps(result, ensure_ascii=False))

    def test_real_model_mismatch_is_zero_call(self):
        result = run_intake_request(
            TEXT,
            reference_time=REFERENCE,
            mode="real",
            env={
                "SILICONFLOW_API_KEY": "secret-not-for-output",
                "MES_LLM_MODEL": "Other/Model",
            },
            expected_provider="siliconflow",
            expected_model="Qwen/Qwen3-30B-A3B-Instruct-2507",
        )
        self.assertEqual(result["run"]["error_code"], "CONFIG_MODEL_MISMATCH")
        self.assertEqual(result["run"]["client_calls"], 0)
        self.assertEqual(result["run"]["provider"], "siliconflow")
        self.assertEqual(result["run"]["model"], "Other/Model")
        self.assertNotIn("secret-not-for-output", json.dumps(result, ensure_ascii=False))

    def test_real_with_injected_double_is_test_double_not_http(self):
        model = WrappedDouble()
        result = run_intake_request(
            TEXT,
            reference_time=REFERENCE,
            mode="real",
            model_client=model,
            env={"SILICONFLOW_API_KEY": "should-not-be-read"},
        )
        self.assertEqual(result["run"]["execution_source"], "test_double")
        self.assertEqual(result["run"]["client_calls"], 1)
        self.assertEqual(result["draft"]["state"], "ready")

    def test_fixed_model_error_code_is_preserved(self):
        result = run_intake_request(
            TEXT,
            reference_time=REFERENCE,
            mode="real",
            model_client=ErrorDouble("HTTP_ERROR"),
            env={},
        )
        self.assertEqual(result["run"]["error_code"], "HTTP_ERROR")
        self.assertEqual(result["draft"]["state"], "error")
        self.assertEqual(result["run"]["client_calls"], 1)

    def test_invalid_mode_and_input_are_fixed_errors(self):
        bad = run_intake_request(TEXT, reference_time=REFERENCE, mode="other")
        self.assertEqual(bad["run"]["error_code"], "INPUT_INVALID")
        self.assertEqual(bad["run"]["client_calls"], 0)
        empty = run_intake_request("   ", reference_time=REFERENCE, mode="offline")
        self.assertEqual(empty["run"]["error_code"], "INPUT_INVALID")
        self.assertEqual(empty["run"]["client_calls"], 0)

    def test_ui_import_alias_remains(self):
        from mes_assistant import intake_ui

        self.assertIs(intake_ui.OfflineIntakeModel, OfflineIntakeModel)

    def test_extraction_still_used_directly(self):
        draft = extract_draft(
            TEXT,
            reference_time=REFERENCE,
            model_client=OfflineIntakeModel(),
        )
        self.assertEqual(draft["state"], "ready")


if __name__ == "__main__":
    unittest.main()
