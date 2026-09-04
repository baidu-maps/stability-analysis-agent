from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from services.analyze_pipeline import run_analyze_context_loop
from services.context_loop_contract import build_round0_output_format_lines


class AnalyzePipelineTests(unittest.TestCase):
    def test_round0_contract_documents_grep_and_read_file(self) -> None:
        lines = build_round0_output_format_lines(agent_loop="context_loop")
        blob = "\n".join(lines)
        self.assertIn("`grep`", blob)
        self.assertIn("`read_file`", blob)
        self.assertIn("grep_matches", blob)

    @patch("services.analyze_pipeline.run_agent_context_loop")
    def test_run_analyze_context_loop_injects_repo_map(self, mock_loop) -> None:
        from services.agent_context_loop import ContextLoopResult

        mock_loop.return_value = ContextLoopResult(
            analysis_text="done",
            prompt_used="prompt with repo map",
        )
        context = MagicMock()
        context.config = {"evidence_max_chars": 24000}
        context.select_prompt = lambda p: p
        context.observations = None

        prepare = {
            "initial_prompt": "base prompt",
            "code_roots": ["examples/crash_cases/agent_cross_file_uaf/code"],
            "resolved_stack": {
                "threads": [{
                    "frames": [{"function": "Session::onCallback", "file": "src/session.cpp"}],
                }],
            },
            "step": 5,
            "total_steps": 6,
        }
        run_analyze_context_loop(
            context=context,
            prepare=prepare,
            problem={"scope": "full", "agent_loop": "context_loop"},
        )
        call_kwargs = mock_loop.call_args.kwargs
        prompt = call_kwargs.get("initial_prompt") or call_kwargs.get("hooks")
        if isinstance(prompt, str):
            self.assertIn("base prompt", prompt)
        engine = call_kwargs.get("context_engine")
        self.assertIsNotNone(engine)
        self.assertTrue(getattr(engine, "repo_map", None) or engine.config.max_requests >= 8)


if __name__ == "__main__":
    unittest.main()
