#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gate B 结论完备性单元测试。"""

from __future__ import annotations

import unittest

from services.conclusion_readiness import (
    detect_version_gated_apis,
    evaluate_conclusion_readiness,
    pipeline_skip_metadata_conclusion,
    prompt_os_constraint_lines,
)


def _code_with_snippet(text: str) -> dict:
    """构造含 snippet 的最小 code_context。"""
    return {
        "graph": {
            "nodes": [
                {
                    "id": "node-1",
                    "snippet": text.splitlines(),
                }
            ],
            "evidence_summary": {"crash_function_snippet_complete": True},
        }
    }


class ConclusionReadinessTests(unittest.TestCase):
    """覆盖缺 os_version / 有 os_version / 提示词硬约束。"""

    def test_missing_os_version_with_version_gated_api_blocks(self) -> None:
        """源码含 @available 且无系统版本时 Gate B 不通过。"""
        code = _code_with_snippet(
            "if (@available(iOS 15.0, *)) {\n"
            "  range = CAFrameRateRangeMake(30, 60, 60);\n"
            "}\n"
        )
        report = evaluate_conclusion_readiness(
            parse_result={"meta_info": {}},
            code_context=code,
            scope="full",
        )
        self.assertFalse(report["ready"])
        ids = [g["id"] for g in report["gaps"]]
        self.assertIn("missing_os_version_for_api_gate", ids)
        self.assertIn("系统版本", report["user_message"])
        skip = pipeline_skip_metadata_conclusion(report)
        self.assertTrue(skip["pipeline_skipped"])
        self.assertEqual(skip["pipeline_skip_reason"], "conclusion_not_ready")

    def test_os_version_present_allows_llm_with_prompt_constraints(self) -> None:
        """有系统版本时放行，并生成 Evidence Contract 硬约束。"""
        parse_result = {
            "meta_info": {"os_type": "ios", "os_version": "14.3"},
            "registers": {"values": {"pc": "0x1"}},
        }
        code = _code_with_snippet(
            "if (@available(iOS 15.0, *)) {\n"
            "  range = CAFrameRateRangeMake(30, 60, 60);\n"
            "}\n"
        )
        report = evaluate_conclusion_readiness(
            parse_result=parse_result,
            code_context=code,
            scope="full",
        )
        self.assertTrue(report["ready"])
        self.assertEqual(report["os_version"], "14.3")
        self.assertTrue(report["pc_null_like"])
        markers = detect_version_gated_apis(code)
        self.assertIn("CAFrameRateRangeMake", markers)
        lines = prompt_os_constraint_lines(parse_result, code)
        joined = "\n".join(lines)
        self.assertIn("14.3", joined)
        self.assertIn("@available", joined)

    def test_no_version_gated_marker_passes_without_os(self) -> None:
        """无版本门控标记时，即使缺 os_version 也不阻断。"""
        code = _code_with_snippet("void foo() { bar(); }\n")
        report = evaluate_conclusion_readiness(
            parse_result={"meta_info": {}},
            code_context=code,
            scope="full",
        )
        self.assertTrue(report["ready"])
        self.assertEqual(report["gaps"], [])

    def test_parse_scope_skips_gate(self) -> None:
        """parse_stack_only 不做结论完备性拦截。"""
        code = _code_with_snippet("@available(iOS 15.0, *)\nCAFrameRateRangeMake(1,2,3)\n")
        report = evaluate_conclusion_readiness(
            parse_result={"meta_info": {}},
            code_context=code,
            scope="parse_stack_only",
        )
        self.assertTrue(report["ready"])


if __name__ == "__main__":
    unittest.main()
