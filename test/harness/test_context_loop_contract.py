from __future__ import annotations

import unittest

from services.context_loop_contract import (
    assemble_loop_prompt,
    build_json_format_reminder,
    build_round0_output_format_lines,
    parse_agent_decision,
    prompt_has_json_contract,
    build_investigation_state_section,
)


class ContextLoopContractTests(unittest.TestCase):
    def test_round0_output_format_contains_json_example(self):
        lines = build_round0_output_format_lines(agent_loop="context_loop")
        blob = "\n".join(lines)
        self.assertIn("agent_can_fetch_more", blob)
        self.assertIn("context_requests", blob)

    def test_assemble_loop_prompt_includes_task_and_reminder(self):
        base = "# 崩溃分析任务\n\n请分析。"
        out = assemble_loop_prompt(
            base,
            evidence_package={"items": [{"kind": "source_code", "content": "void f() {}", "source": "test"}]},
            is_final_round=False,
        )
        content = out["content"]
        self.assertIn("## 本轮任务", content)
        self.assertIn("## 输出契约", content)
        self.assertIn("## 其它代码上下文", content)
        self.assertEqual(out["evidence_item_count"], 1)

    def test_json_reminder_on_final_round(self):
        reminder = build_json_format_reminder(is_final_round=True)
        self.assertIn("agent_can_fetch_more", reminder)
        self.assertIn("false", reminder)

    def test_parse_agent_decision_matches_contract(self):
        text = '{"agent_can_fetch_more": true, "context_requests": [{"type": "function", "symbol": "Foo::bar"}]}'
        parsed = parse_agent_decision(text)
        self.assertTrue(parsed["agent_can_fetch_more"])
        self.assertEqual(len(parsed["context_requests"]), 1)

    def test_prompt_has_json_contract(self):
        self.assertTrue(prompt_has_json_contract('{"agent_can_fetch_more": true, "context_requests": []}'))
        self.assertFalse(prompt_has_json_contract("plain text"))

    def test_investigation_plan_and_claim_are_visible_without_source_injection(self):
        section = build_investigation_state_section({
            "verification_claim": {"statement": "callback no longer accesses released object", "minimum_level": "L2"},
            "investigation_plan": [{"kind": "find_callers", "target": "Foo::run", "reason": "trace ownership"}],
        })
        self.assertIn("验证声明", section)
        self.assertIn("find_callers", section)
        self.assertIn("Foo::run", section)
        self.assertNotIn("void Foo::run()", section)


if __name__ == "__main__":
    unittest.main()
