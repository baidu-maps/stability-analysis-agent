"""Parser tests for control JSON that follows prose and code fences."""
from __future__ import annotations

import unittest

from services.agent_output_parser import extract_json_object, parse_agent_decision, parse_analysis_report


class AgentOutputParserTests(unittest.TestCase):
    """extract_json_object must keep the trailing control contract."""

    def test_trailing_contract_survives_prose_and_cpp_braces(self):
        """A lambda brace and a cpp fence must not hide the control JSON."""
        text = (
            "调用 `PushTask(CVTaskFn::MakeTask([=]{ ... CancelTask(false); }));`。\n"
            "```cpp\n"
            "void CGridLayer::CancelTask(bool wait) {\n"
            "    if (m_loadingBlockTask.empty()) {\n"
            "        return;\n"
            "    }\n"
            "}\n"
            "```\n"
            '{"agent_can_fetch_more": false, "context_requests": [], '
            '"next_action": {"kind": "propose_fix", "reason": "读写两端都已核对"}}'
        )
        payload = extract_json_object(text)
        self.assertIsNotNone(payload)
        self.assertIs(payload["agent_can_fetch_more"], False)
        self.assertEqual(payload["next_action"]["kind"], "propose_fix")
        parsed = parse_agent_decision(text)
        self.assertFalse(parsed["degraded"])
        self.assertIs(parsed["raw_payload"]["agent_can_fetch_more"], False)
        self.assertEqual(parsed["next_action"]["kind"], "propose_fix")

    def test_json_fence_is_not_confused_with_cpp_fence(self):
        """Only a complete JSON object is taken from a fenced answer."""
        text = (
            "```cpp\n"
            "void Foo::bar() { return; }\n"
            "```\n"
            "```json\n"
            '{"agent_can_fetch_more": false, "context_requests": []}\n'
            "```\n"
        )
        payload = extract_json_object(text)
        self.assertIs(payload["agent_can_fetch_more"], False)
        self.assertEqual(payload["context_requests"], [])

    def test_repair_plan_with_braces_inside_strings_stays_intact(self):
        """prefer_key=edits returns the plan even after a control contract."""
        plan = (
            '{"summary": "lock both sides", "edits": [{"file": "GridLayer.cpp", '
            '"edit_type": "function_replacement", '
            '"function_signature": "void CGridLayer::CancelTask(bool wait)", '
            '"replacement_code": "void CGridLayer::CancelTask(bool wait) { return; }"}]}'
        )
        control = '{"agent_can_fetch_more": false, "context_requests": []}'
        text = plan + "\n" + control
        payload = extract_json_object(text, prefer_key="edits")
        self.assertEqual(payload["summary"], "lock both sides")
        self.assertEqual(len(payload["edits"]), 1)
        self.assertIn("{ return; }", payload["edits"][0]["replacement_code"])
        contract = extract_json_object(text)
        self.assertIs(contract["agent_can_fetch_more"], False)

    def test_analysis_report_is_not_replaced_by_control_contract(self):
        """A trailing control object must not erase a structured report."""
        text = (
            '{"summary_zh": "竞态", "root_cause": "未加锁", "confidence": 0.8}\n'
            '{"agent_can_fetch_more": false, "context_requests": []}'
        )
        report, error = parse_analysis_report(text)
        self.assertIsNone(error)
        self.assertEqual(report.summary_zh, "竞态")
        self.assertIsNone(parse_analysis_report('{"agent_can_fetch_more": false}')[0])


if __name__ == "__main__":
    unittest.main()
