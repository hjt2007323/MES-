"""Offline candidate tests; standard library only."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from ceval.cli import main
from ceval.compare import compare_tables
from ceval.quality import judge_all, judge_case

ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    return json.loads((ROOT / "data" / name).read_text(encoding="utf-8"))


class CandidateInputBindingTest(unittest.TestCase):
    def _run(self, cases, results):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases_path = root / "cases.json"
            results_path = root / "results.json"
            output = root / "out"
            cases_path.write_text(json.dumps({"cases": cases}), encoding="utf-8")
            results_path.write_text(json.dumps({"results": results}), encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = main(
                    [
                        "--cases",
                        str(cases_path),
                        "--results",
                        str(results_path),
                        "--output-dir",
                        str(output),
                    ]
                )
            return code, stderr.getvalue(), list(output.glob("*")) if output.exists() else []

    def test_empty_duplicate_unknown_and_missing_bindings_exit_two(self) -> None:
        cases = load("cases.json")["cases"]
        results = load("results.json")["results"]
        mutations = (
            ("empty_cases", [], results),
            ("empty_results", cases, []),
            ("duplicate_case", cases + [cases[0]], results),
            ("duplicate_result", cases, results + [results[0]]),
            ("unknown_result", cases, results + [{**results[0], "case_id": "unknown"}]),
            ("missing_result", cases, results[:-1]),
            ("invalid_cases_object", {}, results),
        )
        for name, cases_value, results_value in mutations:
            with self.subTest(name=name):
                code, stderr, files = self._run(cases_value, results_value)
                self.assertEqual(code, 2)
                self.assertIn("ERROR input_binding", stderr)
                self.assertEqual(files, [])

    def test_plan_and_top_level_query_id_must_agree(self) -> None:
        cases = load("cases.json")["cases"]
        results = load("results.json")["results"]
        case = next(item for item in cases if item["id"] == "syn-01")
        result = next(item for item in results if item["case_id"] == "syn-01")
        result = json.loads(json.dumps(result))
        result["plan"]["query_id"] = "work_order_progress"
        judged = judge_case(case, result)
        self.assertEqual(judged["verdict"], "fail")
        self.assertEqual(judged["reason_code"], "plan_query_id_mismatch")

    def test_zero_query_count_must_be_integer_zero(self) -> None:
        cases = load("cases.json")["cases"]
        results = load("results.json")["results"]
        case = next(item for item in cases if item["id"] == "syn-03")
        result = next(item for item in results if item["case_id"] == "syn-03")
        result = json.loads(json.dumps(result))
        result["counts"]["queries"] = 0.0
        judged = judge_case(case, result)
        self.assertEqual(judged["verdict"], "fail")
        self.assertEqual(judged["reason_code"], "zero_query_count_invalid")


class CandidateQualityTest(unittest.TestCase):
    def test_synthetic_cases_and_negatives(self) -> None:
        cases = load("cases.json")["cases"]
        results = load("results.json")["results"]
        records = judge_all(cases, results)
        self.assertEqual(len(records), 6)
        self.assertTrue(all(record["expected_matched"] for record in records))
        by_id = {record["case_id"]: record for record in records}
        self.assertEqual(by_id["syn-01"]["reason_code"], "query_match")
        self.assertEqual(by_id["syn-02"]["reason_code"], "params_mismatch")
        self.assertEqual(by_id["syn-03"]["reason_code"], "clarification_match")
        self.assertEqual(by_id["syn-04"]["reason_code"], "refusal_match")
        self.assertEqual(by_id["syn-05"]["reason_code"], "table_row_count_mismatch")
        self.assertEqual(by_id["syn-06"]["reason_code"], "zero_query_violation")
        self.assertEqual(
            sum(record["verdict"] == "pass" for record in records), 3
        )

    def test_empty_table_wrong_params_is_not_saved_by_empty_rows(self) -> None:
        cases = load("cases.json")["cases"]
        results = load("results.json")["results"]
        case = next(item for item in cases if item["id"] == "syn-02")
        result = next(item for item in results if item["case_id"] == "syn-02")
        self.assertEqual(result["table"]["rows"], [])
        judged = judge_case(case, result)
        self.assertEqual(judged["verdict"], "fail")
        self.assertEqual(judged["reason_code"], "params_mismatch")

    def test_zero_query_and_duplicate_row_negatives(self) -> None:
        cases = load("cases.json")["cases"]
        results = load("results.json")["results"]
        refuse_case = next(item for item in cases if item["id"] == "syn-06")
        refuse_result = next(item for item in results if item["case_id"] == "syn-06")
        self.assertEqual(judge_case(refuse_case, refuse_result)["reason_code"], "zero_query_violation")
        query_case = next(item for item in cases if item["id"] == "syn-05")
        query_result = next(item for item in results if item["case_id"] == "syn-05")
        self.assertEqual(judge_case(query_case, query_result)["reason_code"], "table_row_count_mismatch")

    def test_table_order_is_ignored(self) -> None:
        left = {
            "columns": [{"name": "x", "type": "integer", "nullable": False}],
            "rows": [[1], [2]],
            "truncated": False,
            "truncation_reason": None,
        }
        right = {
            "columns": [{"name": "x", "type": "integer", "nullable": False}],
            "rows": [[2], [1]],
            "truncated": False,
            "truncation_reason": None,
        }
        self.assertTrue(compare_tables(left, right)["equal"])
        self.assertFalse(compare_tables(left, right, row_order="preserve")["equal"])

    def test_cli_runs_without_model_db_or_internal_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out"
            code = main(["--output-dir", str(output)])
            self.assertEqual(code, 0)
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["cases"], 6)
            self.assertEqual(summary["expected_matched"], 6)
            self.assertTrue(summary["all_expected_matched"])
            self.assertFalse((output / "cases.jsonl").read_bytes().startswith(b"\xef\xbb\xbf"))
            report = (output / "report.md").read_text(encoding="utf-8")
            self.assertIn("C offline synthetic demo", report)

    def test_candidate_data_has_no_internal_host_path(self) -> None:
        for path in (ROOT / "data").glob("*.json"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(":\\", text)
            self.assertNotIn("execution", text)
            self.assertNotIn("internal", text)


if __name__ == "__main__":
    unittest.main()
