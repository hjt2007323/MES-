import json
import unittest
from pathlib import Path

from mes_assistant.intake_extraction import (
    classify_multi_event,
    extract_draft,
    validate_record,
)
from mes_assistant.llm_client import ModelClientError, ModelReply

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "intake_live_failure_regressions.json"

TEXT = (
    "M01 于 2026-11-04T08:00:00+08:00 停机，"
    "2026-11-04T08:10:00+08:00 恢复，原因：故障，工单：WO01。"
)
REFERENCE = "2026-11-04T12:00:00+08:00"


def fields_for(text, *, machine="M01", order="WO01"):
    return {
        "machine_no": {"value": machine, "quote": machine},
        "start_time": {"value": "2026-11-04T08:00:00+08:00", "quote": "2026-11-04T08:00:00+08:00"},
        "end_time": {"value": "2026-11-04T08:10:00+08:00", "quote": "2026-11-04T08:10:00+08:00"},
        "reason": {"value": "故障", "quote": "故障"},
        "order_id": {"value": order, "quote": order} if order is not None else {"value": None, "quote": None},
    }


def action(fields=None):
    return json.dumps({"kind": "extract", "fields": fields or fields_for(TEXT)}, ensure_ascii=False)


class FakeModel:
    def __init__(self, content=None, error=None):
        self.content = content
        self.error = error
        self.calls = 0

    def complete(self, messages, *, timeout_ms):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return ModelReply(content=self.content, usage={"prompt_tokens": 1, "completion_tokens": 1})


class IntakeExtractionTests(unittest.TestCase):
    def test_ready_draft_has_unique_evidence(self):
        draft = extract_draft(TEXT, reference_time=REFERENCE, model_client=FakeModel(action()))
        self.assertEqual(draft["state"], "ready")
        self.assertEqual(draft["mode"], "model")
        self.assertEqual(draft["record"]["machine_no"], "M01")
        self.assertEqual(draft["record"]["order_id"], "WO01")
        self.assertEqual(draft["issues"], [])
        self.assertFalse(draft["terminal"])
        self.assertEqual(draft["pending_fields"], [])
        evidence = draft["evidence"]["reason"]
        self.assertEqual(TEXT[evidence["start"]:evidence["end"]], "故障")
        self.assertEqual(evidence["quote"], "故障")

    def test_missing_field_needs_clarification(self):
        fields = fields_for(TEXT)
        fields["machine_no"] = {"value": None, "quote": None}
        draft = extract_draft(TEXT, reference_time=REFERENCE, model_client=FakeModel(action(fields)))
        self.assertEqual(draft["state"], "needs_clarification")
        self.assertIn("MISSING_MACHINE_NO", draft["issues"])
        self.assertIsNone(draft["record"]["machine_no"])

    def test_reverse_time_is_not_swapped(self):
        fields = fields_for(TEXT)
        fields["start_time"] = {"value": "2026-11-04T08:10:00+08:00", "quote": "2026-11-04T08:10:00+08:00"}
        fields["end_time"] = {"value": "2026-11-04T08:00:00+08:00", "quote": "2026-11-04T08:00:00+08:00"}
        draft = extract_draft(TEXT, reference_time=REFERENCE, model_client=FakeModel(action(fields)))
        self.assertIn("INVALID_TIME_ORDER", draft["issues"])
        self.assertEqual(draft["state"], "needs_clarification")
        self.assertEqual(draft["record"]["start_time"], "2026-11-04T08:10:00+08:00")
        self.assertEqual(draft["record"]["end_time"], "2026-11-04T08:00:00+08:00")

    def test_fake_or_duplicate_quote_is_not_confirmable(self):
        fields = fields_for(TEXT)
        fields["reason"] = {"value": "故障", "quote": "不存在"}
        draft = extract_draft(TEXT, reference_time=REFERENCE, model_client=FakeModel(action(fields)))
        self.assertIn("EVIDENCE_MISMATCH", draft["issues"])
        self.assertIsNone(draft["evidence"]["reason"])
        self.assertIsNone(draft["record"]["reason"])
        self.assertIn("reason", draft["pending_fields"])

        duplicate_text = "故障 故障"
        fields = fields_for(duplicate_text)
        fields["reason"] = {"value": "故障", "quote": "故障"}
        draft = extract_draft(duplicate_text, reference_time=REFERENCE, model_client=FakeModel(action(fields)))
        self.assertIn("EVIDENCE_MISMATCH", draft["issues"])
        self.assertIsNone(draft["record"]["reason"])
        self.assertIn("reason", draft["pending_fields"])

    def test_unknown_device_order_and_bad_time(self):
        fields = fields_for(TEXT)
        fields["machine_no"] = {"value": "M99", "quote": "M01"}
        fields["order_id"] = {"value": "WO99", "quote": "WO01"}
        fields["start_time"] = {"value": "2026/11/04 08:00", "quote": "2026-11-04T08:00:00+08:00"}
        draft = extract_draft(TEXT, reference_time=REFERENCE, model_client=FakeModel(action(fields)))
        self.assertIn("UNKNOWN_DEVICE", draft["issues"])
        self.assertIn("UNKNOWN_ORDER", draft["issues"])
        self.assertIn("INVALID_TIME_FORMAT", draft["issues"])

    def test_strict_shape_extra_keys_duplicate_json_and_nonfinite(self):
        valid = json.loads(action())
        extra_top = dict(valid, extra=True)
        extra_field = json.loads(action())
        extra_field["fields"]["machine_no"]["extra"] = True
        duplicate = '{"kind":"extract","kind":"extract","fields":{}}'
        nonfinite = '{"kind":"clarify","issue":"AMBIGUOUS_CONTEXT","x":NaN}'
        for content in (
            json.dumps(extra_top, ensure_ascii=False),
            json.dumps(extra_field, ensure_ascii=False),
            duplicate,
            nonfinite,
            "not-json",
        ):
            with self.subTest(content=content[:20]):
                draft = extract_draft(TEXT, reference_time=REFERENCE, model_client=FakeModel(content))
                self.assertEqual(draft["state"], "error")
                self.assertIn("PARSE_INVALID", draft["issues"])
                self.assertTrue(draft["terminal"])

    def test_timeout_and_model_failure_are_errors(self):
        timeout = extract_draft(
            TEXT,
            reference_time=REFERENCE,
            model_client=FakeModel(error=ModelClientError("MODEL_TIMEOUT", "timeout")),
        )
        self.assertEqual(timeout["state"], "error")
        self.assertIn("MODEL_TIMEOUT", timeout["issues"])
        self.assertTrue(timeout["terminal"])
        failure = extract_draft(
            TEXT,
            reference_time=REFERENCE,
            model_client=FakeModel(error=ModelClientError("NETWORK_ERROR", "down")),
        )
        self.assertEqual(failure["state"], "error")
        self.assertIn("MODEL_FAILED", failure["issues"])

    def test_clarify_and_refuse_actions(self):
        clarify = extract_draft(
            TEXT,
            reference_time=REFERENCE,
            model_client=FakeModel('{"kind":"clarify","issue":"MULTIPLE_EVENTS"}'),
        )
        self.assertEqual(clarify["state"], "needs_clarification")
        self.assertEqual(clarify["issues"], ["MULTIPLE_EVENTS"])
        self.assertTrue(clarify["terminal"])
        refuse = extract_draft(
            TEXT,
            reference_time=REFERENCE,
            model_client=FakeModel('{"kind":"refuse","issue":"OUT_OF_SCOPE"}'),
        )
        self.assertEqual(refuse["state"], "refused")
        self.assertEqual(refuse["issues"], ["OUT_OF_SCOPE"])
        self.assertTrue(refuse["terminal"])

    def test_input_bounds_and_reference_are_checked(self):
        model = FakeModel(action())
        with self.assertRaises(ValueError):
            extract_draft("   ", reference_time=REFERENCE, model_client=model)
        with self.assertRaises(ValueError):
            extract_draft("x" * 2001, reference_time=REFERENCE, model_client=model)
        with self.assertRaises(ValueError):
            extract_draft(TEXT, reference_time="bad", model_client=model)
        self.assertEqual(model.calls, 0)

    def test_new_same_device_and_ambiguous_repeat_regressions(self):
        cases = {
            item["case_id"]: item
            for item in json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
        }
        same = cases["same-device-two-events"]
        self.assertTrue(classify_multi_event(same["text"]))
        model = FakeModel(json.dumps(same["model_action"], ensure_ascii=False))
        draft = extract_draft(
            same["text"], reference_time=REFERENCE, model_client=model
        )
        self.assertEqual(draft["issues"], ["MULTIPLE_EVENTS"])
        self.assertTrue(draft["terminal"])
        self.assertEqual(model.calls, 0)

        ambiguous = cases["ambiguous-device-repeat"]
        self.assertFalse(classify_multi_event(ambiguous["text"]))
        model = FakeModel(json.dumps(ambiguous["model_action"], ensure_ascii=False))
        draft = extract_draft(
            ambiguous["text"], reference_time=REFERENCE, model_client=model
        )
        self.assertEqual(draft["issues"], ["MISSING_MACHINE_NO"])
        self.assertFalse(draft["terminal"])
        self.assertEqual(model.calls, 1)

    def test_reconstructed_pilot_failures_align_protocol_and_guard(self):
        cases = {
            item["case_id"]: item
            for item in json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
        }
        for case_id in ("i07", "i08"):
            case = cases[case_id]
            model = FakeModel(json.dumps(case["model_action"], ensure_ascii=False))
            draft = extract_draft(
                case["text"], reference_time=REFERENCE, model_client=model
            )
            self.assertEqual(draft["issues"], [case["expected_issue"]])
            self.assertEqual(draft["state"], case["expected_state"])
            self.assertEqual(draft["terminal"], case["expected_terminal"])
            self.assertEqual(model.calls, 1)

        case = cases["i09"]
        model = FakeModel(json.dumps(case["model_action"], ensure_ascii=False))
        draft = extract_draft(
            case["text"], reference_time=REFERENCE, model_client=model
        )
        self.assertEqual(draft["issues"], ["MULTIPLE_EVENTS"])
        self.assertEqual(draft["state"], "needs_clarification")
        self.assertTrue(draft["terminal"])
        self.assertEqual(model.calls, 0)

    def test_multi_event_guard_positive_and_legitimate_single_forms(self):
        self.assertTrue(
            classify_multi_event(
                "今天M01 8:00到8:10故障停机；M02 9:00到9:10卡料停机。"
            )
        )
        self.assertTrue(
            classify_multi_event(
                "今天M01 8:00到8:10故障停机，M02 9:00到9:10卡料停机。"
            )
        )
        self.assertTrue(
            classify_multi_event(
                "今天一号机08:00到08:10故障停机；二号机09:00到09:10卡料停机。"
            )
        )
        self.assertFalse(classify_multi_event("今天8:12到8:25卡料停机，可能是1号机或2号机。"))
        self.assertFalse(classify_multi_event(TEXT))
        self.assertFalse(
            classify_multi_event(
                "M01 8:00到8:10故障停机；M01 8:00到8:10再次提及。"
            )
        )
        self.assertFalse(
            classify_multi_event("M01（1号机）8:00到8:10故障停机。")
        )

    def test_clarify_missing_codes_are_accepted(self):
        for issue in ("MISSING_END_TIME", "MISSING_MACHINE_NO"):
            draft = extract_draft(
                "今天2号机8:12卡料停机，结束时间没记。",
                reference_time=REFERENCE,
                model_client=FakeModel(
                    json.dumps({"kind": "clarify", "issue": issue})
                ),
            )
            self.assertEqual(draft["issues"], [issue])
            self.assertFalse(draft["terminal"])


        record = {
            "machine_no": "M02",
            "start_time": "2026-11-04T10:00:00+08:00",
            "end_time": "2026-11-04T10:30:00+08:00",
            "reason": "换型",
            "order_id": None,
        }
        normalized, issues = validate_record(record)
        self.assertEqual(issues, [])
        self.assertEqual(normalized["machine_no"], "M02")
        self.assertIsNone(normalized["order_id"])


if __name__ == "__main__":
    unittest.main()
