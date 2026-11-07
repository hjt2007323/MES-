import copy
import json
import tempfile
import unittest
from pathlib import Path

from evaluation import intake_eval as ev
from evaluation import intake_live_eval as lv


class LiveEvalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.inputs_path = self.root / "live-inputs.json"
        lv.export_live_inputs(output_path=self.inputs_path)
        self.inputs, self.fixture = lv.load_live_inputs(self.inputs_path)
        self.cases = self.fixture["cases"]
        self.input_sha = lv.sha256_file(self.inputs_path)

    def _json(self, path, value):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _drafts(self):
        return ev.scripted_drafts(self.cases)

    def _run(self, *, source="test_double", calls=1, usage=None):
        if source == "test_double":
            provider, model = "test-double", "test-double"
        else:
            provider, model = lv.PROVIDER, lv.MODEL
        return {
            "mode": "real",
            "execution_source": source,
            "provider": provider,
            "model": model,
            "client_calls": calls,
            "elapsed_ms": 1,
            "usage": usage,
            "error_code": None,
        }

    def _rows(self, *, source="test_double", calls=1, usage=None):
        return [
            {"case_id": draft["case_id"], "draft": draft, "run": self._run(source=source, calls=calls, usage=usage)}
            for draft in self._drafts()
        ]

    def _budget(self, *, status="approved"):
        budget = {
            "contract_version": lv.LIVE_CONTRACT,
            "status": status,
            "run_id": lv.RUN_ID,
            "config_id": lv.CONFIG_ID,
            "provider": lv.PROVIDER,
            "model": lv.MODEL,
            "dataset_sha256": lv.LIVE_DATASET_SHA,
            "inputs_path": "execution/C/C-010/live-inputs.json",
            "input_sha256": self.input_sha,
            "reference_time": lv.REFERENCE_TIME,
            "case_ids": list(lv.CASE_IDS),
            "mode": "real",
            "max_requests": 12,
            "max_client_calls": 12,
            "max_calls_per_case": 1,
            "call_timeout_ms": 10000,
            "max_completion_tokens_per_call": 512,
            "max_completion_tokens_total": 6144,
            "automatic_retries": 0,
            "write_policy": "draft_only_no_commit",
            "result_directory": "execution/A/A-011/experiment/m5-pilot-001",
        }
        path = self.root / "budget.json"
        self._json(path, budget)
        return path

    def _batch(self, rows, *, state="complete", budget_status="approved", source_sha="a" * 64):
        results_path = self.root / "results.jsonl"
        results_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        observed_ids = [row["case_id"] for row in rows]
        ledger_cases = []
        for case_id in lv.CASE_IDS:
            observed = case_id in observed_ids
            ledger_cases.append(
                {
                    "case_id": case_id,
                    "reserved": observed,
                    "observed": observed,
                    "client_calls": next((row["run"]["client_calls"] for row in rows if row["case_id"] == case_id), None),
                }
            )
        ledger_path = self.root / "ledger.json"
        self._json(ledger_path, {"contract_version": lv.LIVE_CONTRACT, "run_id": lv.RUN_ID, "cases": ledger_cases})
        budget_path = self._budget(status=budget_status)
        manifest = {
            "contract_version": lv.LIVE_CONTRACT,
            "run_id": lv.RUN_ID,
            "config_id": lv.CONFIG_ID,
            "provider": lv.PROVIDER,
            "model": lv.MODEL,
            "state": state,
            "stop_reason": None if state == "complete" else "BUDGET_INVALID",
            "planned": 12,
            "observed": len(rows),
            "source_sha256": source_sha,
            "dataset_sha256": lv.LIVE_DATASET_SHA,
            "input_sha256": self.input_sha,
            "budget_sha256": lv.sha256_file(budget_path),
            "results_sha256": lv.sha256_file(results_path),
            "ledger_sha256": lv.sha256_file(ledger_path),
        }
        manifest_path = self.root / "manifest.json"
        self._json(manifest_path, manifest)
        return {
            "manifest": manifest_path,
            "ledger": ledger_path,
            "results": results_path,
            "budget": budget_path,
        }

    def _validate(self, paths, *, expected_source_sha="a" * 64, source_root=None, budget=True):
        return lv.validate_batch(
            inputs_path=self.inputs_path,
            manifest_path=paths["manifest"],
            ledger_path=paths["ledger"],
            results_path=paths["results"],
            budget_path=paths["budget"] if budget else None,
            expected_source_sha256=expected_source_sha,
            source_root=source_root,
        )

    def _refresh_manifest(self, paths):
        manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
        manifest["results_sha256"] = lv.sha256_file(paths["results"])
        manifest["ledger_sha256"] = lv.sha256_file(paths["ledger"])
        self._json(paths["manifest"], manifest)

    def test_export_has_no_answers_and_binds_frozen_fixture(self):
        payload = json.loads(self.inputs_path.read_text(encoding="utf-8"))
        self.assertEqual(set(payload), {"contract_version", "dataset_sha256", "reference_time", "timezone", "cases"})
        self.assertEqual(payload["dataset_sha256"], lv.LIVE_DATASET_SHA)
        self.assertEqual([case["case_id"] for case in payload["cases"]], list(lv.CASE_IDS))
        for case in payload["cases"]:
            self.assertEqual(set(case), {"case_id", "text"})
            self.assertNotIn("expected", json.dumps(case))
        self.assertEqual(self.input_sha, lv.sha256_file(self.inputs_path))
        self.assertEqual(self.fixture["cases"][0]["id"], "i01")

    def test_export_without_internal_spec_still_uses_frozen_fixture(self):
        public = self.root / "public-inputs.json"
        spec_missing = self.root / "coordination" / "M5-录入案例规格.json"
        result = lv.export_live_inputs(output_path=public, spec_path=spec_missing)
        self.assertEqual(result["dataset_sha256"], lv.LIVE_DATASET_SHA)
        self.assertEqual(result["input_sha256"], lv.sha256_file(public))
        self.assertFalse(spec_missing.exists())
        payload = json.loads(public.read_text(encoding="utf-8"))
        self.assertEqual([case["case_id"] for case in payload["cases"]], list(lv.CASE_IDS))
        self.assertTrue(all(set(case) == {"case_id", "text"} for case in payload["cases"]))

    def test_complete_test_double_metrics_and_usage_unknown(self):
        paths = self._batch(self._rows(usage={"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}))
        batch = self._validate(paths)
        summary = lv.summarize_live(batch)
        self.assertEqual(summary["state"], "complete")
        self.assertEqual(summary["case_pass"], 12)
        self.assertEqual(summary["record_exact"], 6)
        self.assertEqual(summary["field_exact"], 30)
        self.assertEqual(summary["evidence_valid"], 30)
        self.assertEqual(summary["error_ready"], 0)
        self.assertTrue(summary["safety_gate"])
        self.assertEqual(summary["status"], "ok")
        self.assertTrue(summary["test_double"])
        self.assertFalse(summary["real_quality"])
        self.assertEqual(summary["client_calls"], 12)
        self.assertEqual(summary["client_calls_semantics"], "complete_calls")
        self.assertTrue(summary["usage_unknown"])
        self.assertIsNone(summary["usage"]["total_tokens"])

    def test_zero_calls_and_unknown_tokens(self):
        paths = self._batch(self._rows(calls=0, usage=None))
        summary = lv.summarize_live(self._validate(paths))
        self.assertEqual(summary["client_calls"], 0)
        self.assertTrue(summary["usage_unknown"])
        self.assertEqual(summary["usage"], {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None})

    def test_missing_case_incomplete_keeps_denominator_and_null_rates(self):
        rows = self._rows()[:11]
        summary = lv.summarize_live(self._validate(self._batch(rows, state="incomplete")))
        self.assertEqual(summary["state"], "incomplete")
        self.assertIsNone(summary["rates"])
        self.assertEqual(summary["coverage"]["observed"], 11)
        self.assertEqual(summary["coverage"]["planned"], 12)
        self.assertIn("i12", [item["case_id"] for item in summary["badcases"]])

    def test_pending_budget_is_rejected(self):
        paths = self._batch(self._rows(), budget_status="pending_user_approval")
        with self.assertRaises(lv.LiveEvalError) as captured:
            self._validate(paths)
        self.assertEqual(captured.exception.code, "BUDGET_INVALID")

    def test_duplicate_mixed_source_and_forged_results_are_rejected(self):
        rows = self._rows()
        duplicate = self._rows()
        duplicate[1] = copy.deepcopy(duplicate[0])
        with self.assertRaises(lv.LiveEvalError) as captured:
            self._validate(self._batch(duplicate))
        self.assertEqual(captured.exception.code, "RESULT_DUPLICATE")

        mixed = self._rows()
        mixed[1]["run"]["execution_source"] = "http_model"
        mixed[1]["run"]["provider"] = lv.PROVIDER
        mixed[1]["run"]["model"] = lv.MODEL
        with self.assertRaises(lv.LiveEvalError) as captured:
            self._validate(self._batch(mixed))
        self.assertEqual(captured.exception.code, "RESULT_MIXED_SOURCE")

        forged = self._rows()
        forged[0]["draft"]["submitted"] = True
        with self.assertRaises(lv.LiveEvalError) as captured:
            self._validate(self._batch(forged))
        self.assertEqual(captured.exception.code, "RESULT_FORGED")

    def test_manifest_result_sha_mismatch_rejected(self):
        paths = self._batch(self._rows())
        manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
        manifest["results_sha256"] = "0" * 64
        self._json(paths["manifest"], manifest)
        with self.assertRaises(lv.LiveEvalError) as captured:
            self._validate(paths)
        self.assertEqual(captured.exception.code, "MANIFEST_RESULTS_SHA")

    def test_error_ready_is_changes_requested_badcase(self):
        rows = self._rows()
        ready = copy.deepcopy(rows[0]["draft"])
        ready["case_id"] = "i07"
        rows[6]["draft"] = ready
        summary = lv.summarize_live(self._validate(self._batch(rows)))
        self.assertEqual(summary["error_ready"], 1)
        self.assertEqual(summary["status"], "changes_requested")
        self.assertFalse(summary["safety_gate"])
        self.assertIn("i07", summary["error_ready_ids"])
        self.assertIn("i07", [item["case_id"] for item in summary["badcases"]])

    def test_wrong_field_and_evidence_are_counted(self):
        rows = self._rows()
        rows[0]["draft"]["record"]["machine_no"] = "M02"
        summary = lv.summarize_live(self._validate(self._batch(rows)))
        self.assertEqual(summary["case_pass"], 11)
        self.assertEqual(summary["record_exact"], 5)
        self.assertEqual(summary["field_exact"], 29)
        self.assertEqual(summary["evidence_valid"], 30)

        rows = self._rows()
        rows[0]["draft"]["evidence"]["reason"] = {
            "quote": "not-in-source",
            "start": 0,
            "end": 1,
        }
        summary = lv.summarize_live(self._validate(self._batch(rows)))
        self.assertEqual(summary["evidence_valid"], 29)
        self.assertEqual(summary["case_pass"], 11)

    def test_rule_and_scripted_are_separate_and_bounded(self):
        rule = lv.summarize_drafts(self.cases, ev.bound_rule_drafts(self.cases), mode="rule", source="rule_template")
        scripted = lv.summarize_drafts(self.cases, ev.scripted_drafts(self.cases), mode="script", source="scripted_oracle")
        self.assertEqual(rule["case_pass"], 2)
        self.assertEqual(rule["record_exact"], 2)
        self.assertEqual(rule["field_exact"], 10)
        self.assertEqual(rule["evidence_valid"], 10)
        self.assertEqual(rule["error_ready"], 0)
        self.assertEqual(scripted["case_pass"], 12)
        self.assertEqual(scripted["record_exact"], 6)
        self.assertEqual(scripted["field_exact"], 30)
        self.assertEqual(scripted["evidence_valid"], 30)

    def test_ledger_result_call_mismatch_and_completion_cap_rejected(self):
        paths = self._batch(self._rows())
        ledger = json.loads(paths["ledger"].read_text(encoding="utf-8"))
        ledger["cases"][0]["client_calls"] = 2
        self._json(paths["ledger"], ledger)
        self._refresh_manifest(paths)
        with self.assertRaises(lv.LiveEvalError) as captured:
            self._validate(paths)
        self.assertEqual(captured.exception.code, "LEDGER_CLIENT_CALLS")

        paths = self._batch(self._rows())
        results = [json.loads(line) for line in paths["results"].read_text(encoding="utf-8").splitlines() if line.strip()]
        if not isinstance(results[0]["run"]["usage"], dict):
            results[0]["run"]["usage"] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        results[0]["run"]["usage"]["completion_tokens"] = 513
        paths["results"].write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in results), encoding="utf-8")
        self._refresh_manifest(paths)
        with self.assertRaises(lv.LiveEvalError) as captured:
            self._validate(paths)
        self.assertEqual(captured.exception.code, "RESULT_COMPLETION_CAP")

    def test_missing_budget_or_source_is_unverified(self):
        paths = self._batch(self._rows(source="http_model"))
        summary = lv.summarize_live(self._validate(paths, budget=False, expected_source_sha=None))
        self.assertEqual(summary["state"], "unverified")
        self.assertEqual(summary["status"], "unverified")
        self.assertIsNone(summary["rates"])
        self.assertFalse(summary["real_quality"])
        self.assertIn("budget", summary["certification_reason"])

        summary = lv.summarize_live(self._validate(paths, expected_source_sha=None))
        self.assertEqual(summary["state"], "unverified")
        self.assertIn("source reference", summary["certification_reason"])

        manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
        manifest["source_sha256"] = "0" * 64
        self._json(paths["manifest"], manifest)
        summary = lv.summarize_live(self._validate(paths, expected_source_sha="0" * 64))
        self.assertEqual(summary["state"], "unverified")
        self.assertIn("all zero", summary["certification_reason"])

    def test_unknown_ledger_calls_is_not_coerced_to_zero(self):
        paths = self._batch(self._rows())
        ledger = json.loads(paths["ledger"].read_text(encoding="utf-8"))
        ledger["cases"][0]["client_calls"] = None
        self._json(paths["ledger"], ledger)
        self._refresh_manifest(paths)
        summary = lv.summarize_live(self._validate(paths))
        self.assertTrue(summary["ledger_calls_unknown"])

    def test_fixture_bytes_sha_mismatch_rejected(self):
        fixture = copy.deepcopy(self.fixture)
        fixture["cases"][0]["expected"]["record"]["machine_no"] = "M09"
        bad = self.root / "bad-fixture.json"
        self._json(bad, fixture)
        with self.assertRaises(lv.LiveEvalError) as captured:
            lv.load_live_inputs(self.inputs_path, fixture_path=bad)
        self.assertEqual(captured.exception.code, "FIXTURE_SHA_MISMATCH")

    def test_input_rejects_duplicate_or_altered_text(self):
        payload = json.loads(self.inputs_path.read_text(encoding="utf-8"))
        altered = copy.deepcopy(payload)
        altered["cases"][0]["case_id"] = "i99"
        bad = self.root / "bad-inputs.json"
        self._json(bad, altered)
        with self.assertRaises(lv.LiveEvalError) as captured:
            lv.load_live_inputs(bad)
        self.assertEqual(captured.exception.code, "CASE_ID")

        altered = copy.deepcopy(payload)
        altered["cases"][0]["text"] = "changed"
        self._json(bad, altered)
        with self.assertRaises(lv.LiveEvalError) as captured:
            lv.load_live_inputs(bad)
        self.assertEqual(captured.exception.code, "INPUT_TEXT_MISMATCH")


if __name__ == "__main__":
    unittest.main()
