from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from services.evaluation import evaluate_suite, summarize_matrix


REPO_ROOT = Path(__file__).resolve().parents[2]


class EvaluationSuiteTests(unittest.TestCase):
    def test_evaluate_suite_with_fixture_report_root(self):
        manifest = {
            "suite_id": "fixture-suite",
            "cases": [
                {
                    "id": "success-fixture",
                    "report_subdir": "success",
                    "expected_category": "NullPtr",
                    "expected_file": "my_lib.cpp",
                },
                {
                    "id": "missing-case",
                    "report_subdir": "does-not-exist",
                    "expected_category": "NullPtr",
                },
            ],
        }
        fixtures_root = Path(__file__).resolve().parent / "fixtures" / "reports"
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            results = evaluate_suite(manifest_path, report_root=fixtures_root)
            self.assertEqual(len(results), 2)
            summary = summarize_matrix(results)
            self.assertEqual(summary["total_cases"], 2)
            self.assertEqual(summary["cases"][0]["case_id"], "success-fixture")
            self.assertEqual(summary["cases"][0]["diagnosis"]["category"], "correct")
            self.assertGreater(summary.get("evidence_coverage", 0), 0)
            self.assertGreater(summary.get("decide_accept_rate", 0), 0)

    def test_demo_basic_manifest_loads(self):
        manifest = REPO_ROOT / "examples/crash_cases/demo_basic/evaluation_manifest.json"
        if not manifest.is_file():
            self.skipTest("demo manifest unavailable")
        results = evaluate_suite(manifest)
        self.assertGreaterEqual(len(results), 1)
        summary = summarize_matrix(results)
        self.assertEqual(summary["total_cases"], len(results))


if __name__ == "__main__":
    unittest.main()
