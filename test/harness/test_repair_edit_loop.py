from __future__ import annotations

import unittest

from services.feedback_analyze import build_feedback_prompt_overlay, resolve_feedback_mode
from services.repair_edit_loop import (
    build_edit_feedback_overlay,
    classify_verification_failure,
    resolve_max_repair_edit_rounds,
    should_run_repair_edit_loop,
)
from services.verification import (
    VerificationCandidate,
    build_auto_verification_config,
    build_verification_config_with_reproduce_priority,
    select_auto_verification_candidate,
)


class RepairEditLoopTests(unittest.TestCase):
    def test_should_run_for_compile_error(self) -> None:
        self.assertTrue(should_run_repair_edit_loop("compile_error", 0, 2))

    def test_should_not_run_when_rounds_exhausted(self) -> None:
        self.assertFalse(should_run_repair_edit_loop("compile_error", 2, 2))

    def test_classify_compile_error(self) -> None:
        failure = classify_verification_failure(
            {"status": "failed", "provider": "run_build", "error": "compile failed"},
            action="run_build",
        )
        self.assertEqual(failure, "compile_error")

    def test_build_edit_overlay_contains_stderr(self) -> None:
        overlay = build_edit_feedback_overlay(
            verification_result={"error": "syntax error", "stderr": "error: expected ';'"},
            applied_fix_result={"fix_plan": {"summary": "fix", "edits": []}},
            failure_class="compile_error",
        )
        self.assertIn("syntax error", overlay)
        self.assertIn("repair plan", overlay)

    def test_resolve_max_rounds_disabled(self) -> None:
        self.assertEqual(resolve_max_repair_edit_rounds({"enable_repair_edit_loop": False}), 0)


class FeedbackAnalyzeModeTests(unittest.TestCase):
    def test_diagnosis_mode_for_test_failure(self) -> None:
        mode = resolve_feedback_mode({
            "verification": {"failure_class": "test_failure"},
        })
        self.assertEqual(mode, "diagnosis_feedback")

    def test_edit_mode_for_compile_error(self) -> None:
        mode = resolve_feedback_mode({
            "verification": {"failure_class": "compile_error"},
        })
        self.assertEqual(mode, "edit_feedback")

    def test_diagnosis_overlay_allows_context_requests(self) -> None:
        overlay = build_feedback_prompt_overlay({}, feedback_mode="diagnosis_feedback")
        self.assertIn("agent_can_fetch_more=true", overlay)

    def test_edit_overlay_blocks_context_requests(self) -> None:
        overlay = build_feedback_prompt_overlay({}, feedback_mode="edit_feedback")
        self.assertIn("agent_can_fetch_more=false", overlay)


class AutoVerifyTests(unittest.TestCase):
    def test_select_prefers_build(self) -> None:
        selected = select_auto_verification_candidate([
            VerificationCandidate("local_command", "test", ["pytest"], "test", 0.9),
            VerificationCandidate("local_command", "build", ["cmake", "--build", "build"], "build", 0.8),
        ])
        self.assertEqual(selected.mode, "build")

    def test_static_fixture_skips_auto_verify(self) -> None:
        self.assertIsNone(
            build_auto_verification_config(
                "/tmp/workspace",
                code_roots=["/tmp/workspace"],
                problem={"runtime_available": False},
            )
        )

    def test_reproduce_priority(self) -> None:
        config = build_verification_config_with_reproduce_priority(
            {"command": ["make"], "tool": "run_build"},
            workspace="/tmp/ws",
            problem={"runtime_available": True},
        )
        self.assertTrue(config.get("reproduce_priority_applied"))


if __name__ == "__main__":
    unittest.main()
