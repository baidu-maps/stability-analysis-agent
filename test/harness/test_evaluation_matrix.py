from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from services.diff_review import review_changed_files
from services.evaluation import evaluate_case, evaluate_report_dir
from services.policy import PolicyEngine
from services.run_snapshot import HarnessRunSnapshot
from services.verification import VerificationRequest, create_verification_provider
from tool_system.runtime import RunTrace


class EvaluationMatrixTests(unittest.TestCase):
    def test_policy_denial_is_counted_in_trace_metrics(self):
        trace = RunTrace("eval-policy")
        trace.emit("tool.policy", kind="policy", name="fix_code_applier", status="denied",
                   decision={"allowed": False, "decision": "approval_required"})
        result = {
            "status": "approval_required",
            "metadata": {"runtime_trace": trace.snapshot()},
            "verification": {"status": "skipped"},
        }
        evaluation = evaluate_case("policy-denial", result=result)
        self.assertEqual(evaluation.runtime["policy_denials"], 1)
        self.assertTrue(evaluation.runtime["approval_required"])

    def test_invalid_context_requests_are_counted(self):
        trace = RunTrace("eval-context")
        trace.emit("agent.context_requests_parsed", request_count=3, invalid_count=2)
        result = {
            "status": "success",
            "metadata": {"runtime_trace": trace.snapshot()},
        }
        evaluation = evaluate_case("invalid-context", result=result)
        self.assertEqual(evaluation.runtime["invalid_context_requests"], 2)
        self.assertEqual(evaluation.runtime["context_request_count"], 3)

    def test_diff_review_unauthorized_files(self):
        review = review_changed_files(["/tmp/outside.py"], allowed_files=["/tmp/allowed.py"])
        self.assertEqual(review.status, "failed")
        result = {
            "status": "success",
            "applied_ai_fixes": {"success": True, "applied": [{"file": "/tmp/outside.py"}]},
            "metadata": {},
        }
        evaluation = evaluate_case("diff-review", result=result, allowed_files=["/tmp/allowed.py"])
        self.assertFalse(evaluation.repair["authorized_files"])

    def test_verification_pending_and_approval_required_status(self):
        pending = evaluate_case("vp", result={"status": "verification_pending", "verification": {"status": "pending"}, "metadata": {}})
        approval = evaluate_case("ar", result={"status": "approval_required", "metadata": {}})
        self.assertTrue(pending.runtime["verification_pending"])
        self.assertTrue(approval.runtime["approval_required"])

    def test_repair_edit_and_auto_verify_metrics(self):
        result = {
            "status": "error",
            "applied_ai_fixes": {"success": False},
            "verification": {
                "status": "failed",
                "auto_selected": True,
                "reproduce_priority_applied": True,
            },
            "repair_edit_rounds": [{"round_index": 0, "success": False}],
            "metadata": {},
        }
        evaluation = evaluate_case("agent-loop", result=result)
        self.assertEqual(evaluation.repair["repair_edit_rounds"], 1)
        self.assertTrue(evaluation.repair["auto_verify_used"])
        self.assertTrue(evaluation.repair["reproduce_priority_applied"])

    def test_reanalyze_and_post_fix_fields(self):
        result = {
            "status": "error",
            "applied_ai_fixes": {"success": False},
            "verification": {
                "status": "failed",
                "post_fix_diagnosis": {"status": "skipped"},
                "reanalyze_diagnosis": {"status": "passed"},
            },
            "metadata": {},
        }
        evaluation = evaluate_case("reanalyze", result=result)
        self.assertEqual(evaluation.repair["reanalyze_diagnosis_status"], "passed")
        self.assertEqual(evaluation.repair["post_fix_diagnosis_status"], "skipped")

    def test_unknown_provider_is_unavailable(self):
        provider = create_verification_provider({
            "provider": "unknown_provider",
        })
        self.assertEqual(provider.name, "none")
        validation = provider.validate(VerificationRequest(workspace="/tmp", changed_files=["a.py"], mode="test"))
        self.assertEqual(validation.status, "unavailable")

    def test_harness_run_snapshot_round_trip(self):
        snapshot = HarnessRunSnapshot(
            run_id="snap-1",
            transport_status="approval_required",
            pending_tool_approval={"tool": "fix_code_applier"},
        )
        restored = HarnessRunSnapshot.from_dict(snapshot.to_dict())
        self.assertEqual(restored.transport_status, "approval_required")
        self.assertEqual(snapshot.to_dict().get("status"), "approval_required")
        self.assertEqual(restored.pending_tool_approval["tool"], "fix_code_applier")

    def test_evaluate_nested_diagnosis_fields(self):
        from services.evaluation import evaluate_case

        result = {
            "crash_diagnosis": {
                "crash_classification": {"primary_pattern": "null_pointer"},
                "stack_summary": {"crash_file": "demo.cpp", "crash_function": "main"},
                "evidence_compass": {
                    "missing_evidence": [{"kind": "trace"}],
                    "layers": {"stack": {"available": True}, "source": {"available": False}},
                    "confidence_ceiling": 0.7,
                },
            },
            "metadata": {"evidence_items": [{"source": "crash_log_parser"}]},
        }
        evaluation = evaluate_case(
            "nested-04a",
            result=result,
            expected_category="null_pointer",
            expected_file="demo.cpp",
            expected_function="main",
        )
        self.assertEqual(evaluation.diagnosis["category"], "correct")
        self.assertEqual(evaluation.diagnosis["missing_evidence_count"], 1)
        self.assertEqual(evaluation.diagnosis["evidence_layers_available"], 1)

    def test_decide_scorer_reject_on_failed_verification(self):
        from services.decide_scorer import score_repair_decision

        score = score_repair_decision(
            applied_ai_fixes={"success": True, "applied": [{"status": "applied"}]},
            diff_review={"status": "passed"},
            verification={"status": "failed", "error": "compile error"},
            run_status="error",
        )
        self.assertEqual(score.decision, "reject")
        self.assertFalse(score.verification_passed)

    def test_manifest_expected_decision_and_judge(self):
        result = {
            "status": "success",
            "crash_diagnosis": {"category": "NullPtr"},
            "applied_ai_fixes": {"success": True, "applied": [{"status": "applied"}]},
            "verification": {"status": "passed"},
            "metadata": {"structured_analysis": {"root_cause": "null"}},
            "context_session": {"termination_reason": "model_final"},
            "judge": {"verdict": "accept", "confidence": "high"},
        }
        from services.decide_scorer import score_repair_decision

        decide = score_repair_decision(
            applied_ai_fixes=result["applied_ai_fixes"],
            diff_review={"status": "passed"},
            verification=result["verification"],
            run_status="success",
        )
        evaluation = evaluate_case(
            "manifest-expect",
            result={**result, "decide": decide.to_dict()},
            expected_decision="accept",
            expected_judge_verdict="accept",
            max_invalid_context_requests=0,
        )
        self.assertEqual(evaluation.repair["decision_match"], "correct")
        self.assertEqual(evaluation.repair["judge_match"], "correct")
        self.assertTrue(evaluation.runtime["invalid_context_within_limit"])

    def test_evaluate_report_dir_loads_judge_sidecar(self):
        root = Path("test/harness/fixtures/reports/success")
        if not root.is_dir():
            self.skipTest("fixture unavailable")
        evaluation = evaluate_report_dir(root, case_id="success-fixture")
        self.assertEqual(evaluation.judge.get("verdict"), "accept")
        demo = Path("examples/crash_cases/demo_basic")
        if not demo.is_dir():
            self.skipTest("demo case unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "04a_crash_diagnosis.json").write_text(json.dumps({"category": "SIGSEGV"}), encoding="utf-8")
            (root / "00_runtime_trace.json").write_text(json.dumps({"events": [], "budget": {}}), encoding="utf-8")
            evaluation = evaluate_report_dir(root, case_id="demo-matrix")
            self.assertEqual(evaluation.case_id, "demo-matrix")


if __name__ == "__main__":
    unittest.main()
