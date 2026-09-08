import tempfile
import unittest

from services.simple_verification import (
    IssueVerificationScript,
    RegressionVerificationScript,
    ReproductionScenario,
    VerificationContext,
    VerificationResult,
    VerificationRunner,
    VerificationStatus,
)
from prompts.report_schema import get_report_instruction


class Issue(IssueVerificationScript):
    def run(self, context):
        return True


class Regression(RegressionVerificationScript):
    def run(self, context):
        return VerificationResult(VerificationStatus.PASSED, "ok")


class UnconfirmedIssue(IssueVerificationScript):
    def run(self, context):
        return VerificationResult(VerificationStatus.PASSED, "ran")


class Reproduction(IssueVerificationScript):
    def run(self, context):
        phase = context.metadata.get("verification_phase")
        return VerificationResult(
            VerificationStatus.PASSED,
            phase,
            reproduced=phase == "before_fix",
        )


class SimpleVerificationTests(unittest.TestCase):
    def context(self, confirmed=True):
        return VerificationContext(
            task_id="task-1", workspace=tempfile.gettempdir(), report_dir=tempfile.gettempdir(),
            reproduction=ReproductionScenario(description="repro", confirmed=confirmed),
        )

    def test_runs_issue_before_regression(self):
        runner = VerificationRunner(issue=Issue(), regression=Regression())
        report = runner.run(self.context())
        self.assertEqual(report["status"], "verified")
        self.assertEqual(report["issue_verification"]["status"], "passed")
        self.assertEqual(report["regression_verification"]["status"], "passed")

    def test_unconfirmed_description_does_not_block_configured_script(self):
        runner = VerificationRunner(issue=UnconfirmedIssue())
        report = runner.run(self.context(confirmed=False))
        self.assertEqual(report["status"], "issue_verified")
        self.assertEqual(report["issue_verification"]["status"], "passed")

    def test_no_scripts_has_no_effectiveness_evidence(self):
        report = VerificationRunner().run(self.context())
        self.assertEqual(report["status"], "skipped")

    def test_bool_result_is_normalized(self):
        report = VerificationRunner(issue=Issue()).run(self.context())
        self.assertEqual(report["issue_verification"]["status"], "passed")

    def test_nested_effectiveness_config_is_loaded(self):
        runner = VerificationRunner.from_mapping({
            "verification": {
                "safety": {},
                "effectiveness": {"issue": Issue()},
            }
        })
        report = runner.run(self.context())
        self.assertEqual(report["status"], "issue_verified")

    def test_reproduction_is_interpreted_by_phase(self):
        runner = VerificationRunner(issue=Reproduction())
        baseline = runner.run_issue_baseline(self.context())
        self.assertEqual(baseline["status"], "passed")
        self.assertTrue(baseline["reproduced"])
        report = runner.run(self.context(), issue_baseline=baseline)
        self.assertEqual(report["status"], "issue_verified")
        self.assertFalse(report["issue_verification"]["reproduced"])

    def test_llm_reproduction_instruction_can_be_suppressed_for_configured_script(self):
        self.assertIn("Crash 复现场景", get_report_instruction(include_reproduction=True))
        self.assertNotIn("Crash 复现场景", get_report_instruction(include_reproduction=False))
        self.assertIn("需补充材料", get_report_instruction(include_reproduction=False))

    def test_configured_scripts_can_be_skipped_without_pending(self):
        report = VerificationRunner(issue=Issue(), regression=Regression()).run(
            self.context(), run_issue=False, run_regression=False,
        )
        self.assertEqual(report["status"], "skipped")

    def test_unconfirmed_pre_fix_reproduction_is_inconclusive(self):
        runner = VerificationRunner(issue=Reproduction())
        report = runner.run(
            self.context(),
            issue_baseline={"status": "inconclusive", "reproduced": False},
        )
        self.assertEqual(report["status"], "inconclusive")

    def test_pre_fix_success_without_reproduction_evidence_is_inconclusive(self):
        runner = VerificationRunner(issue=Issue(), regression=Regression())
        report = runner.run(
            self.context(),
            issue_baseline={"status": "passed"},
        )
        self.assertEqual(report["status"], "inconclusive")


if __name__ == "__main__":
    unittest.main()
