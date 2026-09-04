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
            5,
        )

    def test_explicit_single_overrides_full_default(self):
        self.assertEqual(
            resolve_agent_loop({"scope": "full"}, explicit="single"),
            "single",
        )

    def test_parse_stack_only_defaults_single(self):
        self.assertEqual(
            resolve_agent_loop({"scope": "parse_stack_only", "prompt_mode": "fix"}),
            "single",
        )
        self.assertEqual(
            resolve_max_agent_rounds({"scope": "parse_stack_only", "prompt_mode": "fix"}),
            1,
        )

    def test_configured_rounds_respected(self):
        self.assertEqual(
            resolve_max_agent_rounds({"scope": "full", "max_agent_rounds": 5}),
            5,
        )


if __name__ == "__main__":
    unittest.main()
