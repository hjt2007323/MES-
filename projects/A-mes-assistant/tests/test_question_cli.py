import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mes_assistant import question_cli
from mes_assistant.llm_client import ModelClientError

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
TEST_TMP = WORKSPACE / ".test-tmp" / "ui" / "cli"
FIXTURES = ROOT / "fixtures" / "m2"


class QuestionCliTests(unittest.TestCase):
    def setUp(self):
        TEST_TMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=str(TEST_TMP))
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def run_cli(self, args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = question_cli.main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def log(self, name="events.jsonl"):
        return str(self.dir / name)

    def test_mock_success(self):
        code, out, err = self.run_cli(
            [
                "--request", str(FIXTURES / "request.json"),
                "--mode", "mock",
                "--script", str(FIXTURES / "scripts" / "success.json"),
                "--log", self.log(),
            ]
        )
        self.assertEqual((code, err), (0, ""))
        payload = json.loads(out)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["mode"], "mock")
        self.assertEqual(payload["query_id"], "line_snapshot")
        self.assertEqual(payload["counts"]["queries"], 2)
        self.assertEqual(len(Path(self.log()).read_text(encoding="utf-8").splitlines()), 1)

    def test_mock_clarify_and_refuse(self):
        for name, expected in (("clarify", "needs_clarification"), ("refuse", "refused")):
            with self.subTest(name=name):
                code, out, err = self.run_cli(
                    [
                        "--request", str(FIXTURES / "request.json"),
                        "--mode", "mock",
                        "--script", str(FIXTURES / "scripts" / (name + ".json")),
                        "--log", self.log(name + ".jsonl"),
                    ]
                )
                self.assertEqual((code, err), (0, ""))
                payload = json.loads(out)
                self.assertEqual(payload["status"], expected)
                self.assertIsNone(payload["error"])
                self.assertEqual(payload["counts"]["queries"], 0)

    def test_mock_correction_counts(self):
        code, out, _ = self.run_cli(
            [
                "--request", str(FIXTURES / "request.json"),
                "--mode", "mock",
                "--script", str(FIXTURES / "scripts" / "correction.json"),
                "--log", self.log("correction.jsonl"),
            ]
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["counts"], {"model_calls": 2, "queries": 2, "corrections": 1})

    def test_mock_budget_and_timeout(self):
        code, out, _ = self.run_cli(
            [
                "--request", str(FIXTURES / "request-budget.json"),
                "--mode", "mock",
                "--script", str(FIXTURES / "scripts" / "success.json"),
                "--log", self.log("budget.jsonl"),
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], "budget_exhausted")
        code, out, _ = self.run_cli(
            [
                "--request", str(FIXTURES / "request-timeout.json"),
                "--mode", "mock",
                "--script", str(FIXTURES / "scripts" / "timeout.json"),
                "--log", self.log("timeout.jsonl"),
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], "timeout")

    def test_real_mode_is_unavailable_without_key(self):
        code, out, err = self.run_cli(
            [
                "--request", str(FIXTURES / "request.json"),
                "--mode", "real",
                "--log", self.log("real.jsonl"),
            ]
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(err, "REAL_UNAVAILABLE\n")

    def test_real_mode_with_script_is_unavailable(self):
        code, out, err = self.run_cli(
            [
                "--request", str(FIXTURES / "request.json"),
                "--mode", "real",
                "--script", str(FIXTURES / "scripts" / "success.json"),
                "--log", self.log("real-script.jsonl"),
            ]
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(err, "REAL_UNAVAILABLE\n")

    def test_bad_json_duplicate_nan_and_deep_are_input_errors(self):
        bad = self.dir / "bad.json"
        bad.write_text("not-json", encoding="utf-8")
        duplicate = self.dir / "duplicate.json"
        duplicate.write_text('{"a":1,"a":2}', encoding="utf-8")
        nan = self.dir / "nan.json"
        nan.write_text('{"a":NaN}', encoding="utf-8")
        deep = self.dir / "deep.json"
        deep.write_text("[" * 2000 + "]" * 2000, encoding="utf-8")
        for path in (bad, duplicate, nan, deep):
            with self.subTest(path=path.name):
                code, out, err = self.run_cli(
                    [
                        "--request", str(path),
                        "--mode", "mock",
                        "--script", str(FIXTURES / "scripts" / "success.json"),
                        "--log", self.log("bad.jsonl"),
                    ]
                )
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertEqual(err, "INPUT_ERROR\n")

    def test_missing_script_and_malformed_script_are_input_errors(self):
        code, out, err = self.run_cli(
            [
                "--request", str(FIXTURES / "request.json"),
                "--mode", "mock",
                "--log", self.log("missing.jsonl"),
            ]
        )
        self.assertEqual((code, out, err), (2, "", "INPUT_ERROR\n"))
        script = self.dir / "script.json"
        script.write_text('{"replies":[{"content":1}]}', encoding="utf-8")
        code, out, err = self.run_cli(
            [
                "--request", str(FIXTURES / "request.json"),
                "--mode", "mock",
                "--script", str(script),
                "--log", self.log("bad-script.jsonl"),
            ]
        )
        self.assertEqual((code, out, err), (2, "", "INPUT_ERROR\n"))

    def test_log_open_failure_is_execution_error(self):
        code, out, err = self.run_cli(
            [
                "--request", str(FIXTURES / "request.json"),
                "--mode", "mock",
                "--script", str(FIXTURES / "scripts" / "success.json"),
                "--log", str(self.dir),
            ]
        )
        self.assertEqual(code, 3)
        self.assertEqual(out, "")
        self.assertEqual(err, "EXECUTION_ERROR\n")


if __name__ == "__main__":
    unittest.main()
