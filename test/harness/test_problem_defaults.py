from __future__ import annotations

import unittest

from services.context_engine import resolve_agent_loop, resolve_max_agent_rounds


class ProblemDefaultsTests(unittest.TestCase):
    def test_full_scope_defaults_to_context_loop(self):
        self.assertEqual(
            resolve_agent_loop({"scope": "full", "prompt_mode": "fix"}),
            "context_loop",
        )
        self.assertEqual(
            resolve_max_agent_rounds({"scope": "full", "prompt_mode": "fix"}),
            6,
        )

    def test_explicit_single_overrides_full_default(self):
        self.assertEqual(
            resolve_agent_loop({"scope": "full"}, explicit="single"),
            "single",
        )

    def test_parse_stack_only_fix_defaults_context_loop(self):
        # fix 模式现在也走 context_loop（见 resolve_agent_loop 契约）。
        self.assertEqual(
            resolve_agent_loop({"scope": "parse_stack_only", "prompt_mode": "fix"}),
            "context_loop",
        )
        self.assertEqual(
            resolve_max_agent_rounds({"scope": "parse_stack_only", "prompt_mode": "fix"}),
            1,
        )

    def test_parse_stack_only_analysis_single_loop_rounds(self):
        # 非 full、非 analysis/fix 的组合仍回落 single。
        self.assertEqual(
            resolve_agent_loop({"scope": "parse_stack_only", "prompt_mode": "diagnose"}),
            "single",
        )

    def test_configured_rounds_respected(self):
        self.assertEqual(
            resolve_max_agent_rounds({"scope": "full", "max_agent_rounds": 5}),
            5,
        )


if __name__ == "__main__":
    unittest.main()
