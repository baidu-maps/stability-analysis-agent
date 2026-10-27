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

    def test_round0_prompt_forbids_file_and_medium(self):
        from services.context_loop_contract import build_round0_output_format_lines, build_round_task_section

        blob = "\n".join(build_round0_output_format_lines(agent_loop="context_loop"))
        self.assertIn("禁止：`type=file`", blob)
        self.assertIn("priority=medium", blob)
        self.assertIn("propose_fix", blob)
        self.assertNotIn("示例代码不能代替", blob)
        task = build_round_task_section(is_final_round=True, early_final_reason="invalid_schema")
        self.assertIn("read_file", task)
        self.assertIn("propose_fix", task)
        self.assertIn("critical", task)
        self.assertIn("完整函数", task)

    def test_apply_ai_fixes_requires_complete_functions_at_prompt_end(self):
        from services.context_loop_contract import (
            assemble_loop_prompt,
            build_round0_output_format_lines,
        )
        from workflows.anr_freeze_workflow import _build_anr_prompt

        lines = build_round0_output_format_lines(agent_loop="context_loop", apply_ai_fixes=True)
        blob = "\n".join(lines)
        self.assertIn("修复代码（自动改码时必填）", blob)
        self.assertIn("objc", blob)
        self.assertIn("不会创建评审", blob)
        self.assertIn("callStackSymbols", blob)
        self.assertIn("预先打开的 fd", blob)
        self.assertIn("### `BMKSignalHandler.m`", blob)
        self.assertIn("每个围栏前必须单独一行写源文件名", blob)
        prompt = _build_anr_prompt(
            {"meta_info": {"log_kind": "watchdog"}, "crash_info": {}},
            {},
            {},
            problem={"agent_loop": "context_loop", "apply_ai_fixes": True},
        )
        self.assertIn("修复代码（自动改码时必填）", prompt)
        self.assertIn("callStackSymbols", prompt)
        assembled = assemble_loop_prompt("分析", apply_ai_fixes=True)["content"]
        self.assertIn("不会创建评审", assembled)
        self.assertIn("callStackSymbols", assembled)
        self.assertGreater(assembled.rfind("callStackSymbols"), assembled.rfind("## 本轮任务"))

    def test_anr_prompt_includes_matching_domain_knowledge(self):
        import json
        import tempfile
        from pathlib import Path

        from workflows.anr_freeze_workflow import _build_anr_prompt

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack = root / "ios-bmk-crash"
            crash = pack / "crash-pattern"
            crash.mkdir(parents=True)
            (pack / "pack.json").write_text(json.dumps({
                "id": "ios-bmk-crash",
                "display_name": "iOS BMK",
                "match": {"stack_hints": ["_sigtramp"]},
                "content_globs": ["crash-pattern/**/*.md"],
            }), encoding="utf-8")
            (crash / "signal.md").write_text(
                "---\ntitle: signal\nsymbols: [_sigtramp]\n---\n"
                "_sigtramp 时不得把 ObjC 留在 handler，记录用预先打开的 fd 加 write。\n",
                encoding="utf-8",
            )
            prompt = _build_anr_prompt(
                {"meta_info": {"log_kind": "watchdog"}},
                {"resolved_threads": [{"frames": [{"function": "_sigtramp"}]}]},
                {},
                problem={
                    "agent_loop": "context_loop",
                    "apply_ai_fixes": True,
                    "knowledge_pack_dirs": [str(root)],
                },
            )
        self.assertIn("代码库领域知识", prompt)
        self.assertIn("预先打开的 fd 加 write", prompt)

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
