from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services.feedback_analyze import (
    build_feedback_prompt_overlay,
    should_run_feedback_analyze,
)


class FeedbackAnalyzeTests(unittest.TestCase):
    def test_should_run_on_failed_verification(self):
        self.assertTrue(
            should_run_feedback_analyze(
                {"verification": {"status": "failed"}},
                verification_status="failed",
            )
        )

    def test_should_not_run_when_disabled(self):
        self.assertFalse(
            should_run_feedback_analyze(
                {"verification": {"status": "failed"}},
                verification_status="failed",
                problem={"enable_feedback_analyze": False},
            )
        )

    def test_overlay_includes_judge_questions(self):
        text = build_feedback_prompt_overlay(
            {"analysis": "prior root cause"},
            judge={"questions": ["Which executable check proves the repair?"]},
        )
        self.assertIn("Judge 追问", text)
        self.assertIn("executable check", text)
        self.assertIn("prior root cause", text)

    @patch("services.feedback_analyze.call_analyze_llm_with_phase")
    def test_run_feedback_analyze_updates_result(self, mock_llm):
        mock_llm.return_value = (
            SimpleNamespace(content='{"agent_can_fetch_more": false, "context_requests": [], "analysis": "revised"}'),
            "prompt-used",
        )
        from services.feedback_analyze import run_feedback_analyze

        context = MagicMock()
        context.observations = MagicMock()
        context.observations.markdown.return_value = ""
        result = {"analysis": "old", "final_prompt": "# task"}
        out = run_feedback_analyze(
            context=context,
            result=result,
            problem={"scope": "full"},
            trace=None,
        )
        self.assertIsNotNone(out)
        self.assertEqual(result["_feedback_analyze_count"], 1)
        self.assertIn("revised", result["analysis"])


if __name__ == "__main__":
    unittest.main()
