import copy
import io
import json
import unittest
from pathlib import Path

from mes_assistant import query_catalog, ui_presenter

ROOT = Path(__file__).resolve().parents[1]
REQUEST = json.loads(
    (ROOT / "fixtures" / "m1" / "request.json").read_text(encoding="utf-8")
)
M2_REQUEST = json.loads(
    (ROOT / "fixtures" / "m2" / "request.json").read_text(encoding="utf-8")
)


def make_response(
    query_id,
    rows,
    *,
    status="ok",
    truncated=False,
    reason=None,
    error=None,
    answer="候选解释。",
    sql="SELECT 1;",
    counts=None,
    semantic_policy_version=None,
    context_mode=None,
    context_evidence=None,
    plan=None,
):
    description = query_catalog.describe_query(query_id)
    return {
        "schema_version": "0.1",
        "run_id": "ui-test",
        "request_id": "ui-request",
        "query_id": query_id,
        "mode": "real",
        "status": status,
        "answer": answer,
        "table": {
            "columns": description["columns"],
            "rows": rows,
            "truncated": truncated,
            "truncation_reason": reason,
        }
        if status == "ok"
        else None,
        "sql": sql if status == "ok" else None,
        "metrics": [
            {
                "id": description["metric_id"],
                "definition_version": description["definition_version"],
            }
        ],
        "error": error,
        "counts": counts
        if counts is not None
        else {"model_calls": 0, "queries": 2, "corrections": 0},
        "trace": [{"event_id": "1", "stage": "query", "status": status or "ok"}],
        "data_version": "mes-a07-boundary-v1-pending",
        "review_status": "pending",
        "semantic_policy_version": semantic_policy_version,
        "context_mode": context_mode,
        "context_evidence": context_evidence,
        "plan": plan,
    }


class RequestAndSubmissionTests(unittest.TestCase):
    def test_new_request_is_unique_and_preserves_base(self):
        first = ui_presenter.new_request(REQUEST, "line_snapshot")
        second = ui_presenter.new_request(REQUEST, "line_snapshot")
        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertTrue(first["request_id"].startswith("ui-"))
        self.assertEqual(first["case_id"], "line_snapshot")
        self.assertEqual(second["case_id"], "line_snapshot")
        self.assertEqual(REQUEST["request_id"], "m1-req-001")
        self.assertEqual(first["run_id"], REQUEST["run_id"])

    def test_build_params_for_three_scenarios(self):
        self.assertEqual(
            ui_presenter.build_params("line_snapshot"), {}
        )
        self.assertEqual(
            ui_presenter.build_params("work_order_progress", line_id=2),
            {"line_id": 2},
        )
        self.assertEqual(
            ui_presenter.build_params(
                "downtime_details",
                window_start=" 2026-11-01T00:00:00+08:00 ",
                window_end="2026-11-02T00:00:00+08:00",
            ),
            {
                "window_start": "2026-11-01T00:00:00+08:00",
                "window_end": "2026-11-02T00:00:00+08:00",
            },
        )
        with self.assertRaises(KeyError):
            ui_presenter.build_params("unknown")

    def test_run_submission_calls_injected_query_once(self):
        calls = []

        def query_fn(request, query_id, params, *, log_sink):
            calls.append((request, query_id, params, log_sink))
            return {"status": "ok"}

        sink = object()
        result = ui_presenter.run_submission(
            query_fn, REQUEST, "line_snapshot", {}, log_sink=sink
        )
        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "line_snapshot")
        self.assertEqual(calls[0][2], {})
        self.assertIs(calls[0][3], sink)
        with self.assertRaises(TypeError):
            ui_presenter.run_submission("not-callable", REQUEST, "line_snapshot", {}, log_sink=sink)


class PresentResultTests(unittest.TestCase):
    def test_snapshot_chart_uses_actual_qty_and_null_is_unknown(self):
        response = make_response(
            "line_snapshot",
            [["L01", "205"], ["L02", None], ["L03", "0"]],
        )
        vm = ui_presenter.present_result(response)
        self.assertEqual(vm["status"], "ok")
        self.assertEqual(vm["source"], "real_success")
        self.assertTrue(vm["is_real"])
        self.assertTrue(vm["synthetic"])
        self.assertEqual(vm["display_rows"][1], ["L02", "未知"])
        self.assertEqual(
            vm["chart"],
            [
                {"line_no": "L01", "actual_qty": 205.0},
                {"line_no": "L03", "actual_qty": 0.0},
            ],
        )
        self.assertIn("候选解释", vm["answer"])
        self.assertEqual(vm["sql"], "SELECT 1;")
        self.assertFalse(vm["empty"])

    def test_work_order_null_ratio_is_unknown_and_has_no_chart(self):
        response = make_response(
            "work_order_progress",
            [["WO01", "L01", 100, 80, "0.8000", "生产中"],
             ["WO02", "L01", 0, 5, None, "生产中"]],
        )
        vm = ui_presenter.present_result(response)
        self.assertEqual(vm["query_id"], "work_order_progress")
        self.assertEqual(vm["display_rows"][1][4], "未知")
        self.assertEqual(vm["chart"], [])
        self.assertFalse(vm["empty"])

    def test_downtime_boundary_rows_are_preserved_in_display(self):
        rows = [
            [1, "M01", "2026-11-01T10:00:00+08:00", None, "故障", None],
            [2, "M01", "2026-11-01T10:00:00+08:00", "2026-11-01T11:00:00+08:00", "保养", "WO01"],
            [3, "M02", "2026-11-01T14:00:00+08:00", "2026-11-01T13:00:00+08:00", "缺料", None],
        ]
        vm = ui_presenter.present_result(
            make_response("downtime_details", rows)
        )
        self.assertEqual(len(vm["display_rows"]), 3)
        self.assertEqual(vm["display_rows"][0][3], "未知")
        self.assertEqual(vm["display_rows"][1][3], "2026-11-01T11:00:00+08:00")
        self.assertEqual(vm["chart"], [])

    def test_truncated_and_empty_flags(self):
        truncated = ui_presenter.present_result(
            make_response(
                "line_snapshot",
                [["L01", "1"]],
                truncated=True,
                reason="row_limit",
            )
        )
        self.assertTrue(truncated["truncated"])
        self.assertEqual(truncated["truncation_reason"], "row_limit")
        empty = ui_presenter.present_result(
            make_response("line_snapshot", [])
        )
        self.assertTrue(empty["empty"])
        self.assertFalse(empty["truncated"])
        self.assertEqual(empty["display_rows"], [])

    def test_error_response(self):
        response = make_response(
            "line_snapshot",
            [],
            status="error",
            error={
                "code": "DB_CONNECTION",
                "stage": "query",
                "message": "数据库不可用",
                "retryable": False,
            },
            answer="",
            sql=None,
            counts={"model_calls": 0, "queries": None, "corrections": 0},
        )
        vm = ui_presenter.present_result(response)
        self.assertEqual(vm["status"], "error")
        self.assertEqual(vm["source"], "real_configured")
        self.assertFalse(vm["is_real"])
        self.assertEqual(vm["error_text"], "DB_CONNECTION: 数据库不可用")
        self.assertTrue(vm["empty"])
        self.assertEqual(vm["display_rows"], [])

    def test_non_dict_response_is_error_view(self):
        vm = ui_presenter.present_result("not-a-dict")
        self.assertEqual(vm["status"], "error")
        self.assertEqual(vm["error"]["code"], "UI_RESPONSE_INVALID")
        self.assertEqual(vm["source"], "invalid")
        self.assertTrue(vm["empty"])
        self.assertTrue(vm["synthetic"])


class QuestionPresenterTests(unittest.TestCase):
    def test_build_question_request_is_unique_and_does_not_mutate_base(self):
        first = ui_presenter.build_question_request(
            M2_REQUEST, "列出产量快照", "2026-11-02T00:00:00+08:00"
        )
        second = ui_presenter.build_question_request(
            M2_REQUEST, "列出产量快照", "2026-11-02T00:00:00+08:00"
        )
        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertTrue(first["request_id"].startswith("q-"))
        self.assertEqual(first["question"], "列出产量快照")
        self.assertEqual(first["reference_time"], "2026-11-02T00:00:00+08:00")
        self.assertEqual(first["context_mode"], "full")
        self.assertEqual(M2_REQUEST["question"], "列出各产线当前累计实际产量快照（不按日期）")
        with self.assertRaises(ValueError):
            ui_presenter.build_question_request(
                M2_REQUEST, "  ", "2026-11-02T00:00:00+08:00"
            )

    def test_run_question_submission_calls_once(self):
        calls = []

        def fn(request, *, log_sink):
            calls.append((request, log_sink))
            return {"status": "ok"}

        sink = object()
        result = ui_presenter.run_question_submission(
            fn, M2_REQUEST, log_sink=sink
        )
        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0][1], sink)

    def test_question_view_success_keeps_source_and_usage(self):
        response = make_response("line_snapshot", [["L01", "205"]])
        response["usage"] = {"input_tokens": 10, "output_tokens": 5}
        vm = ui_presenter.question_view(response)
        self.assertTrue(vm["question"])
        self.assertEqual(vm["source"], "real_success")
        self.assertTrue(vm["is_real"])
        self.assertEqual(vm["usage"], {"input_tokens": 10, "output_tokens": 5})
        self.assertEqual(vm["model_calls"], 0)
        self.assertFalse(vm["clarification"])
        self.assertFalse(vm["refused"])

    def test_question_view_clarify_and_refuse(self):
        clarify = {
            "schema_version": "0.1",
            "run_id": "m2",
            "request_id": "q-1",
            "query_id": None,
            "mode": "real",
            "status": "needs_clarification",
            "answer": "请明确产线。",
            "table": None,
            "sql": None,
            "error": None,
            "counts": {"model_calls": 1, "queries": None, "corrections": 0},
            "usage": {"input_tokens": 1, "output_tokens": 2},
            "trace": [],
            "data_version": "mes-a07-boundary-v1-pending",
            "review_status": "pending",
        }
        vm = ui_presenter.question_view(clarify)
        self.assertTrue(vm["clarification"])
        self.assertFalse(vm["refused"])
        self.assertEqual(vm["source"], "real_configured")
        self.assertFalse(vm["is_real"])
        self.assertEqual(vm["answer"], "请明确产线。")
        case = dict(clarify)
        case["status"] = "refused"
        case["answer"] = "超出范围。"
        vm = ui_presenter.question_view(case)
        self.assertTrue(vm["refused"])
        self.assertFalse(vm["clarification"])

    def test_question_view_m3_context_and_plan(self):
        response = make_response(
            "line_snapshot",
            [["L01", "205"]],
            semantic_policy_version="demo-metrics-v1-user-reviewed-20261103",
            context_mode="focused",
            context_evidence={
                "context_mode": "focused",
                "selected_query_ids": ["line_snapshot"],
                "matched_terms": ["产量"],
                "reason": "matched",
                "semantic_policy_version": "demo-metrics-v1-user-reviewed-20261103",
                "registry_sha256": "a" * 64,
            },
            plan={"kind": "query", "query_id": "line_snapshot", "params": {}},
        )
        vm = ui_presenter.question_view(response)
        self.assertTrue(vm["demo_policy_confirmed"])
        self.assertEqual(vm["context_mode"], "focused")
        self.assertEqual(vm["context_evidence"]["selected_query_ids"], ["line_snapshot"])
        self.assertEqual(vm["plan"]["kind"], "query")
        self.assertIn("line_snapshot", vm["plan_text"])

    def test_question_view_error_does_not_claim_success(self):
        error = {
            "schema_version": "0.1",
            "run_id": "m2",
            "request_id": "q-2",
            "query_id": None,
            "mode": "real",
            "status": "error",
            "answer": "失败",
            "table": None,
            "sql": None,
            "error": {
                "code": "MODEL_CONFIG_MISSING",
                "stage": "model",
                "message": "配置缺失",
                "retryable": False,
            },
            "counts": {"model_calls": 0, "queries": None, "corrections": 0},
            "usage": None,
            "trace": [],
            "data_version": "mes-a07-boundary-v1-pending",
            "review_status": "pending",
        }
        vm = ui_presenter.question_view(error)
        self.assertEqual(vm["source"], "real_configured")
        self.assertFalse(vm["is_real"])
        self.assertEqual(vm["error_text"], "MODEL_CONFIG_MISSING: 配置缺失")
        self.assertTrue(vm["empty"])


class QuestionMockTests(unittest.TestCase):
    def _run(self, question):
        request = copy.deepcopy(M2_REQUEST)
        request["question"] = question
        request["request_id"] = "q-mock-test"
        log = io.StringIO()
        result = ui_presenter.mock_question(request, log_sink=log)
        return result, log

    def test_mock_snapshot_clarify_and_refuse(self):
        result, log = self._run("列出各产线当前累计实际产量快照（不按日期）")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["mode"], "mock")
        self.assertEqual(result["query_id"], "line_snapshot")
        self.assertEqual(result["counts"]["queries"], 2)
        self.assertEqual(len(log.getvalue().splitlines()), 1)

        clarify, _ = self._run("我想看工单完成情况。")
        self.assertEqual(clarify["status"], "needs_clarification")
        self.assertEqual(clarify["counts"]["queries"], 0)

        refuse, _ = self._run("请查询库存余量。")
        self.assertEqual(refuse["status"], "refused")
        self.assertIsNone(refuse["error"])


if __name__ == "__main__":
    unittest.main()
