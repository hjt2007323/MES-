import contextlib
import copy
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from mes_assistant import question_runtime, query_planner, semantics
from mes_assistant.fakes import FakeClock
from mes_assistant.llm_client import ModelClientError
from mes_assistant.question_fakes import MockTool, ScriptedModelClient
from mes_assistant.question_runtime import run_question
from mes_assistant.service import ContractInputError

ROOT = Path(__file__).resolve().parents[1]
BASE = json.loads(
    (ROOT / "fixtures" / "m2" / "request.json").read_text(encoding="utf-8")
)


def plan_content(action):
    return json.dumps(action, ensure_ascii=False)


def make_request(*, question="列出各产线当前累计实际产量快照（不按日期）", budget=None, request_id="m2q-test", context_mode=None):
    request = copy.deepcopy(BASE)
    request["question"] = question
    request["request_id"] = request_id
    request["case_id"] = request_id
    if budget is not None:
        request["budget"] = dict(budget)
    if context_mode is not None:
        request["context_mode"] = context_mode
    return request


def run(replies, *, request=None, tool=None, log_sink=None):
    request = make_request() if request is None else request
    clock = FakeClock()
    model = ScriptedModelClient(replies, clock=clock)
    tool = MockTool() if tool is None else tool
    log = io.StringIO() if log_sink is None else log_sink
    result = run_question(
        request,
        model_client=model,
        tool=tool,
        clock=clock,
        service_limits=request["budget"],
        remaining_timeout_ms=request["budget"]["timeout_ms"],
        mode="mock",
        log_sink=log,
    )
    return result, log, model, tool, clock


def event(log):
    return json.loads(log.getvalue().strip())


class QuestionRuntimeSuccessTests(unittest.TestCase):
    def test_success_query_uses_mock_tool_and_one_outer_log(self):
        result, log, model, tool, _ = run(
            [plan_content({"kind": "query", "query_id": "line_snapshot", "params": {}})]
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["mode"], "mock")
        self.assertEqual(result["query_id"], "line_snapshot")
        self.assertEqual(result["counts"], {"model_calls": 1, "queries": 2, "corrections": 0})
        self.assertEqual(result["usage"], {"input_tokens": 10, "output_tokens": 5})
        self.assertEqual(result["table"]["rows"][0], ["L01", "205"])
        self.assertIn("候选", result["answer"])
        self.assertEqual(len(tool.calls), 1)
        self.assertEqual(model.attempts, 1)
        self.assertEqual(len(log.getvalue().splitlines()), 1)
        evt = event(log)
        self.assertEqual(evt["status"], "ok")
        self.assertEqual(evt["query_id"], "line_snapshot")
        self.assertEqual(evt["counts"]["queries"], 2)
        self.assertTrue(any(item["stage"] == "model" for item in result["trace"]))
        self.assertTrue(any(item["stage"] == "query" for item in result["trace"]))
        self.assertEqual(result["review_status"], "pending")

    def test_clarify_does_not_query(self):
        result, log, _, tool, _ = run(
            [plan_content({"kind": "clarify", "message": "请明确产线 line_id。"})]
        )
        self.assertEqual(result["status"], "needs_clarification")
        self.assertIsNone(result["error"])
        self.assertEqual(result["answer"], "请明确产线 line_id。")
        self.assertEqual(result["counts"]["queries"], 0)
        self.assertEqual(tool.calls, [])
        self.assertEqual(event(log)["status"], "needs_clarification")

    def test_refuse_does_not_query(self):
        result, log, _, tool, _ = run(
            [plan_content({"kind": "refuse", "message": "超出范围。"})]
        )
        self.assertEqual(result["status"], "refused")
        self.assertIsNone(result["error"])
        self.assertEqual(result["answer"], "超出范围。")
        self.assertEqual(tool.calls, [])
        self.assertEqual(event(log)["status"], "refused")

    def test_plan_forbidden_is_refused_without_retry(self):
        result, log, model, tool, _ = run(
            [
                plan_content(
                    {
                        "kind": "query",
                        "query_id": "line_snapshot",
                        "params": {},
                        "sql": "SELECT 1",
                    }
                )
            ]
        )
        self.assertEqual(result["status"], "refused")
        self.assertEqual(model.attempts, 1)
        self.assertEqual(result["counts"]["corrections"], 0)
        self.assertEqual(tool.calls, [])

    def test_clarify_allowed_when_query_budget_is_zero(self):
        request = make_request(
            budget={"timeout_ms": 30000, "max_model_calls": 2, "max_queries": 0, "max_corrections": 1}
        )
        result, _, _, tool, _ = run(
            [plan_content({"kind": "clarify", "message": "需要更多信息。"})],
            request=request,
        )
        self.assertEqual(result["status"], "needs_clarification")
        self.assertEqual(tool.calls, [])

    def test_query_budget_is_checked_only_for_query_action(self):
        request = make_request(
            budget={"timeout_ms": 30000, "max_model_calls": 2, "max_queries": 0, "max_corrections": 1}
        )
        result, _, _, tool, _ = run(
            [plan_content({"kind": "query", "query_id": "line_snapshot", "params": {}})],
            request=request,
        )
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["counts"], {"model_calls": 1, "queries": 0, "corrections": 0})
        self.assertEqual(tool.calls, [])


class QuestionRuntimeCorrectionTests(unittest.TestCase):
    def test_one_invalid_plan_is_corrected(self):
        result, log, model, tool, _ = run(
            [
                "not-json",
                plan_content({"kind": "query", "query_id": "line_snapshot", "params": {}}),
            ]
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["counts"], {"model_calls": 2, "queries": 2, "corrections": 1})
        self.assertEqual(model.attempts, 2)
        self.assertEqual(result["usage"], {"input_tokens": 20, "output_tokens": 10})
        self.assertEqual(len(tool.calls), 1)
        self.assertEqual(len(log.getvalue().splitlines()), 1)

    def test_second_invalid_plan_is_error(self):
        result, _, model, tool, _ = run(["not-json", "still-not-json"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "PLAN_INVALID")
        self.assertEqual(result["counts"], {"model_calls": 2, "queries": 0, "corrections": 1})
        self.assertEqual(tool.calls, [])

    def test_correction_budget_zero_returns_budget_exhausted(self):
        request = make_request(
            budget={
                "timeout_ms": 30000,
                "max_model_calls": 2,
                "max_queries": 2,
                "max_corrections": 0,
            }
        )
        result, _, model, tool, _ = run(["not-json"], request=request)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["counts"]["model_calls"], 1)
        self.assertEqual(result["counts"]["corrections"], 0)
        self.assertEqual(tool.calls, [])

    def test_only_one_correction_is_allowed(self):
        request = make_request(
            budget={
                "timeout_ms": 30000,
                "max_model_calls": 1,
                "max_queries": 2,
                "max_corrections": 1,
            }
        )
        result, _, model, tool, _ = run(["not-json"], request=request)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["counts"]["model_calls"], 1)
        self.assertEqual(model.attempts, 1)


class QuestionRuntimeBudgetTimeoutTests(unittest.TestCase):
    def test_zero_model_budget(self):
        request = make_request(
            budget={"timeout_ms": 30000, "max_model_calls": 0, "max_queries": 2, "max_corrections": 1}
        )
        result, log, model, tool, _ = run(["never-used"], request=request)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["counts"], {"model_calls": 0, "queries": 0, "corrections": 0})
        self.assertEqual(model.attempts, 0)
        self.assertEqual(tool.calls, [])

    def test_remaining_timeout_zero_is_timeout(self):
        request = make_request()
        clock = FakeClock()
        result = run_question(
            request,
            model_client=ScriptedModelClient(["never-used"], clock=clock),
            tool=MockTool(),
            clock=clock,
            service_limits=request["budget"],
            remaining_timeout_ms=0,
            mode="mock",
            log_sink=io.StringIO(),
        )
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["counts"]["model_calls"], 0)

    def test_model_clock_advance_beyond_deadline_is_timeout(self):
        request = make_request(
            budget={"timeout_ms": 2, "max_model_calls": 2, "max_queries": 2, "max_corrections": 1}
        )
        result, _, model, tool, _ = run(
            [
                {
                    "content": plan_content(
                        {"kind": "query", "query_id": "line_snapshot", "params": {}}
                    ),
                    "advance_ms": 3,
                }
            ],
            request=request,
        )
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["counts"]["model_calls"], 1)
        self.assertEqual(result["counts"]["queries"], 0)
        self.assertEqual(tool.calls, [])

    def test_log_failure_returns_log_write_failed(self):
        class BadLog:
            def write(self, _text):
                raise OSError("disk full")

        result, _, _, _, _ = run(
            [plan_content({"kind": "query", "query_id": "line_snapshot", "params": {}})],
            log_sink=BadLog(),
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "LOG_WRITE_FAILED")
        self.assertIsNone(result["table"])
        self.assertEqual(result["counts"]["queries"], 2)
        self.assertEqual(result["usage"], {"input_tokens": 10, "output_tokens": 5})


class QuestionRuntimeModelErrorTests(unittest.TestCase):
    def test_config_missing(self):
        class Missing:
            attempts = 0

            def complete(self, _messages, *, timeout_ms):
                raise ModelClientError("CONFIG_MISSING", "no config")

        request = make_request()
        result = run_question(
            request,
            model_client=Missing(),
            tool=MockTool(),
            clock=FakeClock(),
            service_limits=request["budget"],
            remaining_timeout_ms=request["budget"]["timeout_ms"],
            mode="mock",
            log_sink=io.StringIO(),
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "MODEL_CONFIG_MISSING")
        self.assertEqual(result["counts"]["model_calls"], 0)

    def test_network_failure_is_model_failed_without_retry(self):
        class Failing:
            def __init__(self):
                self.attempts = 0

            def complete(self, _messages, *, timeout_ms):
                self.attempts += 1
                raise ModelClientError("NETWORK_ERROR", "network down")

        request = make_request()
        result = run_question(
            request,
            model_client=Failing(),
            tool=MockTool(),
            clock=FakeClock(),
            service_limits=request["budget"],
            remaining_timeout_ms=request["budget"]["timeout_ms"],
            mode="mock",
            log_sink=io.StringIO(),
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "MODEL_FAILED")
        self.assertEqual(result["counts"]["model_calls"], 1)
        self.assertTrue(
            any(
                item.get("stage") == "model" and item.get("error_code") == "NETWORK_ERROR"
                for item in result["trace"]
            )
        )

    def test_query_error_is_preserved(self):
        class ErrorTool:
            def __call__(self, query_id, params, *, remaining_timeout_ms, max_rows=200):
                return {
                    "query_id": query_id,
                    "data_version": "mes-a07-boundary-v1-pending",
                    "status": "error",
                    "table": None,
                    "sql": None,
                    "error": {
                        "code": "DB_ERROR",
                        "stage": "query",
                        "message": "database error",
                        "retryable": False,
                    },
                    "counts": {"queries": 1},
                    "timing": {"internal_ms": 1},
                    "cleanup": [],
                }

        result, _, _, _, _ = run(
            [plan_content({"kind": "query", "query_id": "line_snapshot", "params": {}})],
            tool=ErrorTool(),
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "DB_ERROR")
        self.assertEqual(result["counts"]["queries"], 1)
        self.assertIsNone(result["table"])


class QuestionRuntimeTimeoutHardeningTests(unittest.TestCase):
    def test_model_timeout_maps_to_timeout_even_before_total_deadline(self):
        class TimeoutModel:
            def __init__(self):
                self.attempts = 0

            def complete(self, _messages, *, timeout_ms):
                self.attempts += 1
                raise ModelClientError("MODEL_TIMEOUT", "subcall timed out")

        request = make_request()
        result = run_question(
            request,
            model_client=TimeoutModel(),
            tool=MockTool(),
            clock=FakeClock(),
            service_limits=request["budget"],
            remaining_timeout_ms=request["budget"]["timeout_ms"],
            mode="mock",
            log_sink=io.StringIO(),
        )
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["error"]["code"], "OPERATION_TIMEOUT")
        self.assertEqual(result["error"]["stage"], "model")
        self.assertEqual(result["counts"], {"model_calls": 1, "queries": 0, "corrections": 0})
        self.assertTrue(
            any(
                item.get("stage") == "model" and item.get("error_code") == "MODEL_TIMEOUT"
                for item in result["trace"]
            )
        )

    def test_final_result_formatting_deadline_is_not_ok(self):
        request = make_request(
            budget={"timeout_ms": 100, "max_model_calls": 2, "max_queries": 2, "max_corrections": 1}
        )
        clock = FakeClock()
        original = question_runtime._build_result

        def slow_build(*args, **kwargs):
            result = original(*args, **kwargs)
            clock.advance(100)
            return result

        with mock.patch.object(question_runtime, "_build_result", side_effect=slow_build):
            result = run_question(
                request,
                model_client=ScriptedModelClient(
                    [plan_content({"kind": "query", "query_id": "line_snapshot", "params": {}})],
                    clock=clock,
                ),
                tool=MockTool(),
                clock=clock,
                service_limits=request["budget"],
                remaining_timeout_ms=request["budget"]["timeout_ms"],
                mode="mock",
                log_sink=io.StringIO(),
            )
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["error"]["stage"], "format")
        self.assertIsNone(result["table"])
        self.assertIsNone(result["sql"])
        self.assertEqual(result["answer"], "问答在总预算内未完成。")
        self.assertEqual(result["counts"]["queries"], 2)
        self.assertTrue(any(item.get("stage") == "format" for item in result["trace"]))

    def test_30s_hard_cap_wins_over_120s_request_and_service(self):
        request = make_request(
            budget={"timeout_ms": 120000, "max_model_calls": 2, "max_queries": 2, "max_corrections": 1}
        )
        clock = FakeClock()
        result = run_question(
            request,
            model_client=ScriptedModelClient(
                [
                    {
                        "content": plan_content(
                            {"kind": "query", "query_id": "line_snapshot", "params": {}}
                        ),
                        "advance_ms": 31000,
                    }
                ],
                clock=clock,
            ),
            tool=MockTool(),
            clock=clock,
            service_limits={
                "timeout_ms": 120000,
                "max_model_calls": 2,
                "max_queries": 2,
                "max_corrections": 1,
            },
            remaining_timeout_ms=120000,
            mode="mock",
            log_sink=io.StringIO(),
        )
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["counts"]["model_calls"], 1)
        self.assertEqual(result["counts"]["queries"], 0)
        self.assertLessEqual(result["timing"]["internal_ms"], 40000)

    def test_tool_unknown_queries_is_null_not_zero(self):
        class UnknownTool:
            def __call__(self, query_id, params, *, remaining_timeout_ms, max_rows=200):
                return {
                    "query_id": query_id,
                    "data_version": "mes-a07-boundary-v1-pending",
                    "status": "timeout",
                    "table": None,
                    "sql": None,
                    "error": {
                        "code": "DB_TIMEOUT",
                        "stage": "query",
                        "message": "timeout",
                        "retryable": False,
                    },
                    "counts": {"queries": None},
                    "timing": {"internal_ms": 1},
                    "cleanup": [],
                }

        result, _, _, _, _ = run(
            [plan_content({"kind": "query", "query_id": "line_snapshot", "params": {}})],
            tool=UnknownTool(),
        )
        self.assertEqual(result["status"], "timeout")
        self.assertIsNone(result["counts"]["queries"])


class M3ContextTests(unittest.TestCase):
    def test_full_context_and_adopted_query_plan(self):
        result, log, _, _, _ = run(
            [plan_content({"kind": "query", "query_id": "line_snapshot", "params": {}})]
        )
        self.assertEqual(result["semantic_policy_version"], semantics.SEMANTIC_POLICY_VERSION)
        self.assertEqual(result["context_mode"], "full")
        self.assertEqual(result["context_evidence"]["reason"], "full")
        self.assertEqual(
            result["context_evidence"]["selected_query_ids"],
            ["line_snapshot", "work_order_progress", "downtime_details"],
        )
        self.assertEqual(
            result["plan"],
            {"kind": "query", "query_id": "line_snapshot", "params": {}},
        )
        self.assertEqual(result["review_status"], "pending")
        self.assertEqual(event(log)["status"], "ok")

    def test_focused_context_selects_matching_query_only(self):
        request = make_request(
            question="列出各产线当前累计实际产量快照，不按日期。",
            context_mode="focused",
        )
        result, _, _, _, _ = run(
            [plan_content({"kind": "query", "query_id": "line_snapshot", "params": {}})],
            request=request,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["context_mode"], "focused")
        self.assertEqual(result["context_evidence"]["reason"], "matched")
        self.assertEqual(result["context_evidence"]["selected_query_ids"], ["line_snapshot"])
        self.assertEqual(result["plan"]["kind"], "query")
        self.assertEqual(result["plan"]["query_id"], "line_snapshot")

    def test_clarify_plan_is_adopted_kind_only(self):
        result, _, _, tool, _ = run(
            [plan_content({"kind": "clarify", "message": "请明确产线 line_id。"})]
        )
        self.assertEqual(result["status"], "needs_clarification")
        self.assertEqual(result["plan"], {"kind": "clarify"})
        self.assertEqual(result["counts"]["queries"], 0)
        self.assertEqual(result["context_evidence"]["context_mode"], "full")
        self.assertEqual(tool.calls, [])

    def test_invalid_context_mode_rejected_before_model(self):
        request = make_request(context_mode="other")
        clock = FakeClock()
        model = ScriptedModelClient(
            [plan_content({"kind": "clarify", "message": "不应被调用。"})], clock=clock
        )
        tool = MockTool()
        with self.assertRaises(ContractInputError):
            run_question(
                request,
                model_client=model,
                tool=tool,
                clock=clock,
                service_limits=request["budget"],
                remaining_timeout_ms=request["budget"]["timeout_ms"],
                mode="mock",
                log_sink=io.StringIO(),
            )
        self.assertEqual(model.attempts, 0)
        self.assertEqual(tool.calls, [])


class BPlannerIntegrationTests(unittest.TestCase):
    def test_b_planner_drives_success_and_one_correction(self):
        success = plan_content(
            {"kind": "query", "query_id": "line_snapshot", "params": {}}
        )
        with mock.patch.object(
            query_planner, "parse_plan", wraps=query_planner.parse_plan
        ) as spy:
            result, log, model, tool, _ = run([success])
            success_parse_calls = spy.call_count
            corrected, _, _, _, _ = run(["not-json", success])
            correction_parse_calls = spy.call_count
        self.assertGreaterEqual(success_parse_calls, 1)
        self.assertGreaterEqual(correction_parse_calls, 3)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["query_id"], "line_snapshot")
        self.assertEqual(model.attempts, 1)
        self.assertEqual(len(tool.calls), 1)
        self.assertEqual(corrected["status"], "ok")
        self.assertEqual(corrected["counts"]["model_calls"], 2)
        self.assertEqual(corrected["counts"]["corrections"], 1)
        self.assertEqual(len(log.getvalue().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
