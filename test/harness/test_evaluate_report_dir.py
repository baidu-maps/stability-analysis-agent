from __future__ import annotations

import unittest
from pathlib import Path

from services.evaluation import evaluate_report_dir, write_evaluation_artifact


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "reports"


class EvaluateReportDirTests(unittest.TestCase):
    def test_success_fixture(self):
        root = FIXTURES / "success"
        result = evaluate_report_dir(
            root,
            case_id="success-fixture",
            expected_category="NullPtr",
            expected_file="my_lib.cpp",
            expected_function="crash_nullptr",
        )
        self.assertEqual(result.diagnosis["category"], "correct")
        self.assertEqual(result.diagnosis["location"], "correct")
        self.assertEqual(result.repair["verification"], "passed")
        self.assertEqual(result.runtime["llm_calls"], 1)
        self.assertGreaterEqual(result.diagnosis.get("evidence_item_count", 0), 2)
        self.assertEqual(result.repair.get("decide"), "accept")

    def test_approval_required_fixture(self):
        root = FIXTURES / "approval_required"
        result = evaluate_report_dir(root, case_id="approval-fixture")
        self.assertTrue(result.runtime["approval_required"])
        self.assertEqual(result.runtime["policy_denials"], 1)

    def test_verification_failed_fixture(self):
        root = FIXTURES / "verification_failed"
        result = evaluate_report_dir(root, case_id="verify-failed-fixture")
        self.assertEqual(result.repair["reanalyze_diagnosis_status"], "passed")
        self.assertEqual(result.repair["verification"], "failed")

    def test_write_evaluation_artifact(self):
        import tempfile

        root = FIXTURES / "success"
        with tempfile.TemporaryDirectory() as tmp:
            copy_root = Path(tmp)
            for name in ("00_run_summary.json", "04a_crash_diagnosis.json", "00_runtime_trace.json"):
                (copy_root / name).write_text((root / name).read_text(encoding="utf-8"), encoding="utf-8")
            evaluation = evaluate_report_dir(copy_root, case_id="artifact-write")
            path = write_evaluation_artifact(copy_root, evaluation)
            self.assertTrue(path.is_file())
            self.assertEqual(path.name, "00_evaluation.json")


if __name__ == "__main__":
    unittest.main()
