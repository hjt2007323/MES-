import copy
import json
import tempfile
import unittest
from pathlib import Path

from evaluation import v2_eval as v2


def good_response(case):
    expected = case["expected"]
    response = {
        "status": expected["status"],
        "action": expected["action"],
        "answer": "synthetic",
        "source": expected.get("source"),
        "filters": dict(expected.get("filters", {})),
        "record_id": expected.get("record_id"),
        "issues": [expected["issue"]] if "issue" in expected else [],
        "table": None,
        "model_calls": 1,
        "query_calls": 1 if expected.get("source") else 0,
        "usage": {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None},
    }
    if expected.get("source"):
        row_count = expected.get("row_count", 0)
        response["table"] = {
            "columns": ["downtime_id"],
            "rows": [[index + 1] for index in range(row_count)],
            "truncated": expected.get("truncated", False),
            "truncation_reason": expected.get("truncation_reason"),
        }
    if "query_calls" in expected:
        response["query_calls"] = expected["query_calls"]
    return response


class V2EvalTests(unittest.TestCase):
    def setUp(self):
        self.spec = v2.load_spec()

    def test_spec_is_frozen_12_plus_3_sessions(self):
        self.assertEqual(len(self.spec["cases"]), 12)
        self.assertEqual([case["id"] for case in self.spec["cases"]], ["q%02d" % i for i in range(1, 13)])
        self.assertEqual(len(self.spec["sessions"]), 3)
        self.assertEqual(self.spec["reference_time"], "2026-11-04T12:00:00+08:00")

    def test_all_12_synthetic_responses_pass(self):
        responses = {case["id"]: good_response(case) for case in self.spec["cases"]}
        results = v2.judge_cases(self.spec, responses)
        summary = v2.summarize(self.spec, results)
        self.assertEqual(summary["pass"], 12)
        self.assertEqual(summary["fail"], 0)

    def test_wrong_source_rows_and_filters_fail(self):
        responses = {case["id"]: good_response(case) for case in self.spec["cases"]}
        responses["q01"]["source"] = "mysql_history"
        self.assertEqual(v2.judge_cases(self.spec, responses)[0]["verdict"], "fail")

        responses = {case["id"]: good_response(case) for case in self.spec["cases"]}
        responses["q02"]["table"]["rows"] = []
        self.assertEqual(v2.judge_cases(self.spec, responses)[1]["verdict"], "fail")

        responses = {case["id"]: good_response(case) for case in self.spec["cases"]}
        responses["q03"]["filters"]["machine_no"] = "M01"
        self.assertEqual(v2.judge_cases(self.spec, responses)[2]["verdict"], "fail")

    def test_write_action_truncation_and_issue_fail(self):
        responses = {case["id"]: good_response(case) for case in self.spec["cases"]}
        responses["q07"]["action"] = "commit"
        self.assertEqual(v2.judge_cases(self.spec, responses)[6]["reason_code"], "write_action")

        responses = {case["id"]: good_response(case) for case in self.spec["cases"]}
        responses["q12"]["table"]["truncated"] = False
        self.assertEqual(v2.judge_cases(self.spec, responses)[11]["verdict"], "fail")

        responses = {case["id"]: good_response(case) for case in self.spec["cases"]}
        responses["q09"]["issues"] = []
        self.assertEqual(v2.judge_cases(self.spec, responses)[8]["verdict"], "fail")

    def test_response_budget_cap(self):
        responses = {case["id"]: good_response(case) for case in self.spec["cases"]}
        responses["q01"]["model_calls"] = 3
        results = v2.judge_cases(self.spec, responses)
        self.assertEqual(results[0]["reason_code"], "model_calls")

    def test_cli_writes_summary(self):
        responses = {case["id"]: good_response(case) for case in self.spec["cases"]}
        with tempfile.TemporaryDirectory() as tmp:
            responses_path = Path(tmp) / "responses.jsonl"
            responses_path.write_text(
                "".join(json.dumps({"case_id": cid, "response": response}, ensure_ascii=False) + "\n" for cid, response in responses.items()),
                encoding="utf-8",
            )
            output = Path(tmp) / "out"
            code = v2.main(["--responses", str(responses_path), "--output-dir", str(output)])
            self.assertEqual(code, 0)
            summary = json.loads((output / "v2-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["pass"], 12)
            self.assertEqual(summary["fail"], 0)


if __name__ == "__main__":
    unittest.main()
