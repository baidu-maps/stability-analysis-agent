#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rag.memory_retriever import collect_memory_context, render_memory_context
from tools.vector_memory_retriever_tool import VectorMemoryRetrieverTool
from workflows.crash_analysis_workflow import BaseCrashAnalysisWorkflow, iOSCrashAnalyzeWorkflow


class TestMemoryRetriever(unittest.TestCase):
    def test_render_memory_context_empty(self):
        self.assertEqual(render_memory_context([], [], {}, []), "")

    def test_render_memory_context_rule_hit(self):
        text = render_memory_context(
            [{"rule_id": "r1", "rule_name": "Null deref", "conclusion_payload": {"hint": "检查指针"}}],
            [],
            {},
            [],
        )
        self.assertIn("规则命中", text)
        self.assertIn("Null deref", text)

    def test_collect_defaults_readonly(self):
        out = collect_memory_context(
            parse_result={"frames": []},
            resolved_stack={"frames": []},
            code_context={"snippets": []},
        )
        self.assertTrue(out.get("vector_db_readonly"))

    def test_retrieve_patterns_readonly_skips_usage_update(self):
        from rag.vector_database_integration import StabilityMemorySystem

        mem = object.__new__(StabilityMemorySystem)
        mem.meta_store = MagicMock()
        mem.meta_store.get_pattern.return_value = {
            "pattern_summary": "p",
            "confidence_score": 0.5,
            "validation_state": "verified",
            "hit_count": 1,
            "adopted_count": 0,
            "rejected_count": 0,
        }
        mem.pattern_index = MagicMock()
        mem.pattern_index.query.return_value = [
            {"pattern_id": "pattern_test", "distance": 0.2}
        ]
        StabilityMemorySystem.retrieve_patterns(mem, "sigsegv", n_results=1, record_usage=False)
        mem.meta_store.update_usage.assert_not_called()
        StabilityMemorySystem.retrieve_patterns(mem, "sigsegv", n_results=1, record_usage=True)
        mem.meta_store.update_usage.assert_called_once_with("pattern_test", hit_inc=1)

    def test_collect_minimal_inputs(self):
        out = collect_memory_context(
            parse_result={"frames": []},
            resolved_stack={"frames": []},
            code_context={"snippets": []},
        )
        self.assertTrue(out.get("success"))
        self.assertIsInstance(out.get("memory_context"), str)
        self.assertIn("rule_hits", out)
        if out.get("skipped"):
            self.assertIn(out.get("skip_reason"), ("rag_stack_unavailable", "retrieval_error"))

    def test_tool_execute_minimal(self):
        tool = VectorMemoryRetrieverTool()
        ok, err = tool.validate_input(
            {
                "parse_result": {},
                "resolved_stack": {},
                "code_context": {},
            }
        )
        self.assertTrue(ok, err)
        result = tool.execute(
            {
                "parse_result": {},
                "resolved_stack": {},
                "code_context": {},
            }
        )
        self.assertIn("memory_context", result)
        self.assertIn("success", result)

    def test_include_memory_context_in_final_tip_default_off(self):
        self.assertFalse(BaseCrashAnalysisWorkflow._include_memory_context_in_final_tip({}))
        self.assertFalse(BaseCrashAnalysisWorkflow._include_memory_context_in_final_tip(None))

    def test_include_memory_context_in_final_tip_problem_flag(self):
        self.assertTrue(
            BaseCrashAnalysisWorkflow._include_memory_context_in_final_tip(
                {"include_memory_context_in_final_tip": True}
            )
        )
        self.assertFalse(
            BaseCrashAnalysisWorkflow._include_memory_context_in_final_tip(
                {"include_memory_context_in_final_tip": False}
            )
        )

    def test_build_prompt_final_tip_omits_memory_by_default(self):
        wf = iOSCrashAnalyzeWorkflow()
        memory = "规则命中:\n- 并发：stl_lock"
        text = wf._build_prompt_final_tip(
            {},
            {"resolved_frames": []},
            {"crash_summary": {"error_type": "SIGSEGV"}},
            memory_context=memory,
            problem={},
        )
        self.assertNotIn("## 规则与经验模式参考", text)
        self.assertNotIn("stl_lock", text)

    def test_build_prompt_final_tip_includes_memory_when_enabled(self):
        wf = iOSCrashAnalyzeWorkflow()
        memory = "规则命中:\n- 并发：stl_lock"
        text = wf._build_prompt_final_tip(
            {},
            {"resolved_frames": []},
            {"crash_summary": {"error_type": "SIGSEGV"}},
            memory_context=memory,
            problem={"include_memory_context_in_final_tip": True},
        )
        self.assertIn("## 规则与经验模式参考", text)
        self.assertIn("stl_lock", text)

    def test_cli_include_memory_in_05_flag(self):
        from cli.main import build_parser

        parser = build_parser()
        args = parser.parse_args(
            [
                "--crash-log",
                "-",
                "--include-memory-in-05",
            ]
        )
        self.assertTrue(args.include_memory_in_05)
        args_off = parser.parse_args(["--crash-log", "-", "--no-include-memory-in-05"])
        self.assertFalse(args_off.include_memory_in_05)

    def test_prompt_mode_default_analysis(self):
        self.assertEqual(BaseCrashAnalysisWorkflow._resolve_prompt_mode({}), "analysis")
        self.assertEqual(
            BaseCrashAnalysisWorkflow._resolve_prompt_mode({"prompt_mode": "fix"}),
            "fix",
        )
        self.assertEqual(
            BaseCrashAnalysisWorkflow._resolve_prompt_mode({"prompt_mode": "unknown"}),
            "analysis",
        )

    def test_build_prompt_final_tip_analysis_mode_does_not_force_fix_code(self):
        wf = iOSCrashAnalyzeWorkflow()
        text = wf._build_prompt_final_tip(
            {},
            {"resolved_frames": []},
            {"crash_summary": {"error_type": "SIGSEGV"}},
            problem={},
        )
        self.assertIn("结论置信度", text)
        self.assertIn("仅在证据与上下文足够时", text)
        self.assertIn("## 必须遵守的规则", text)
        self.assertNotIn("## 关键约束", text)
        self.assertNotIn("修复代码必须完整且可编译", text)
        self.assertNotIn("需要修改的函数列表（仅列出需要改动的函数）", text)

    def test_build_prompt_final_tip_fix_mode_keeps_patch_contract(self):
        wf = iOSCrashAnalyzeWorkflow()
        text = wf._build_prompt_final_tip(
            {},
            {"resolved_frames": []},
            {"crash_summary": {"error_type": "SIGSEGV"}},
            problem={"prompt_mode": "fix"},
        )
        self.assertIn("需要修改的函数列表（仅列出需要改动的函数）", text)
        self.assertIn("修复代码必须完整且可编译", text)
        self.assertIn("## 必须遵守的规则", text)
        self.assertNotIn("## 关键约束", text)

    def test_agent_loop_defaults_and_context_loop_prompt(self):
        wf = iOSCrashAnalyzeWorkflow()
        self.assertEqual(BaseCrashAnalysisWorkflow._resolve_agent_loop({}), "context_loop")
        self.assertEqual(
            BaseCrashAnalysisWorkflow._resolve_agent_loop({"prompt_mode": "fix"}),
            "single",
        )
        self.assertEqual(
            BaseCrashAnalysisWorkflow._resolve_agent_loop(
                {"agent_loop": "single", "prompt_mode": "analysis"}
            ),
            "single",
        )
        self.assertEqual(
            BaseCrashAnalysisWorkflow._resolve_agent_loop({"agent_loop": "context_loop"}),
            "context_loop",
        )
        # analysis 模式默认 3 轮，其它模式默认 1 轮
        self.assertEqual(
            BaseCrashAnalysisWorkflow._resolve_max_agent_rounds({"prompt_mode": "analysis"}),
            3,
        )
        self.assertEqual(
            BaseCrashAnalysisWorkflow._resolve_max_agent_rounds({"prompt_mode": "fix"}),
            1,
        )
        self.assertEqual(
            BaseCrashAnalysisWorkflow._resolve_max_agent_rounds({"max_agent_rounds": 3}),
            3,
        )
        text = wf._build_prompt_final_tip(
            {},
            {"resolved_frames": []},
            {"crash_summary": {"error_type": "SIGSEGV"}},
            problem={"agent_loop": "context_loop", "max_agent_rounds": 2},
        )
        self.assertIn("context_requests", text)
        self.assertIn("need_more_context", text)

        weak_text = wf._build_prompt_final_tip(
            {},
            {"resolved_frames": []},
            {
                "crash_summary": {
                    "error_type": "SIGSEGV",
                    "crash_thread_name": "com.anjuke.home",
                    "crash_thread_has_business_frames": False,
                }
            },
            problem={"agent_loop": "context_loop", "prompt_mode": "analysis"},
        )
        self.assertIn("不可用上下文边界", weak_text)
        self.assertIn("com.anjuke.home` 是线程名/进程名标签", weak_text)
        self.assertIn("禁止在 `context_requests` 中请求 `com.anjuke.home::main`", weak_text)
        self.assertIn("不得放入 `context_requests`", weak_text)
        self.assertIn("`type=function` 只能请求函数源码", weak_text)
        self.assertIn("首轮应尽量一次性列出所有高价值补充请求", weak_text)
        self.assertIn("如果还需要 Agent 补充上下文", weak_text)
        self.assertIn("只输出需要 Agent 补充什么内容，以及为什么需要这些内容", weak_text)
        self.assertIn("当输出 `need_more_context=true` 时，禁止输出最终分析报告", weak_text)
        self.assertIn("证据充分且上下文完整时，可给出直接可用的 C/C++ 修复代码块", weak_text)
        self.assertNotIn("不要输出 C/C++ 修复代码块", weak_text)
        self.assertGreater(
            weak_text.index("不可用上下文边界"),
            weak_text.index("## 必须遵守的规则"),
        )

    def test_context_loop_prompt_deduplicates_previous_requests(self):
        previous_analysis = """### 结论
证据不足，需要补充 VTaskQueue::PushTask。

### 需要 Agent 补充的上下文
```json
{
  "need_more_context": true,
  "context_requests": [
    {"type": "function", "symbol": "VTaskQueue::PushTask", "reason": "验证竞态", "priority": "medium"}
  ]
}
```"""
        prompt = BaseCrashAnalysisWorkflow._build_context_loop_prompt(
            "原始提示词",
            previous_analysis,
            1,
            [
                {
                    "request": {
                        "type": "function",
                        "symbol": "VTaskQueue::PushTask",
                        "reason": "验证竞态",
                        "priority": "medium",
                    },
                    "success": False,
                    "error": "未定位到函数定义: VTaskQueue::PushTask",
                }
            ],
        )
        self.assertIn("上一轮模型分析摘要如下", prompt)
        self.assertIn("上一轮模型要求补充的信息", prompt)
        self.assertIn("## 已处理的上下文请求", prompt)
        self.assertIn("### 已尝试但未定位", prompt)
        self.assertIn("`VTaskQueue::PushTask`（function）", prompt)
        self.assertIn("如果仍需要补充上下文，只输出", prompt)
        self.assertIn("为什么需要这些内容", prompt)
        self.assertEqual(prompt.count('"symbol": "VTaskQueue::PushTask"'), 1)
        self.assertEqual(prompt.count("### 需要 Agent 补充的上下文"), 0)

        final_prompt = BaseCrashAnalysisWorkflow._build_context_loop_prompt(
            "原始提示词",
            previous_analysis,
            4,
            [
                {
                    "request": {
                        "type": "function",
                        "symbol": "VTaskQueue::PushTask",
                    },
                    "success": False,
                    "skipped": True,
                    "error": "重复请求",
                }
            ],
            is_final_round=True,
        )
        self.assertIn("当前已经达到允许的最大多轮次数", final_prompt)
        self.assertIn("不得再请求 Agent 补充上下文", final_prompt)
        self.assertIn('"need_more_context": false', final_prompt)

        early_final_prompt = BaseCrashAnalysisWorkflow._build_context_loop_prompt(
            "原始提示词",
            previous_analysis,
            2,
            [
                {
                    "request": {"type": "field", "symbol": "CVMapSchedule::m_lastTask"},
                    "success": False,
                    "skipped": True,
                    "error": "重复请求",
                },
                {
                    "request": {"type": "references", "symbol": "CVMapSchedule::CheckAlive"},
                    "success": False,
                    "skipped": True,
                    "error": "重复请求",
                },
            ],
            is_final_round=True,
            early_final_reason="all_requests_blocked",
        )
        self.assertIn("均已由 Agent 处理过", early_final_prompt)
        self.assertNotIn("当前已经达到允许的最大多轮次数", early_final_prompt)

    def test_all_context_requests_blocked_helper(self):
        blocked = [
            {"success": False, "skipped": True, "request": {"symbol": "A"}},
            {"success": False, "rejected": True, "request": {"symbol": "B"}},
        ]
        failed_lookup = [
            {"success": False, "request": {"symbol": "C"}, "error": "未定位"},
        ]
        mixed = blocked + [{"success": True, "request": {"symbol": "D"}}]
        self.assertTrue(BaseCrashAnalysisWorkflow._all_context_requests_blocked(blocked))
        self.assertFalse(BaseCrashAnalysisWorkflow._all_context_requests_blocked(failed_lookup))
        self.assertFalse(BaseCrashAnalysisWorkflow._all_context_requests_blocked(mixed))
        self.assertFalse(BaseCrashAnalysisWorkflow._all_context_requests_blocked([]))

    def test_rejects_package_like_context_request_symbols(self):
        result = BaseCrashAnalysisWorkflow._resolve_context_requests(
            [
                {
                    "type": "function",
                    "symbol": "com.anjuke.home::main",
                    "reason": "需获取主线程入口函数",
                    "priority": "high",
                },
                {
                    "type": "function",
                    "symbol": "main",
                    "reason": "需获取入口函数",
                    "priority": "high",
                },
                {
                    "type": "function",
                    "symbol": "Activity::onCreate",
                    "reason": "猜测主线程入口",
                    "priority": "high",
                },
                {
                    "type": "references",
                    "symbol": "com.anjuke.home",
                    "reason": "错误地把线程名当符号引用",
                    "priority": "medium",
                },
            ],
            code_roots=[],
            max_requests=5,
        )
        self.assertEqual(len(result), 4)
        self.assertFalse(result[0]["success"])
        self.assertTrue(result[0]["rejected"])
        self.assertEqual(result[0]["reject_reason"], "unavailable_context")
        self.assertIn("线程名/进程名/包名", result[0]["error"])
        self.assertFalse(result[1]["success"])
        self.assertTrue(result[1]["rejected"])
        self.assertIn("未提供文件或行号", result[1]["error"])
        self.assertFalse(result[2]["success"])
        self.assertTrue(result[2]["rejected"])
        self.assertIn("入口函数猜测", result[2]["error"])
        self.assertFalse(result[3]["success"])
        self.assertTrue(result[3]["rejected"])
        self.assertIn("线程名/进程名/包名标签", result[3]["error"])

    def test_context_request_types_and_duplicate_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "MapSchedule.cpp"
            src.write_text(
                "\n".join(
                    [
                        "class CVMapSchedule {",
                        "  CVTask* m_lastTask;",
                        "  void onTaskEventHandler(CVTask* task);",
                        "};",
                        "void CVMapSchedule::onTaskEventHandler(CVTask* task) {",
                        "  m_lastTask = task;",
                        "}",
                    ]
                ),
                encoding="utf-8",
            )
            result = BaseCrashAnalysisWorkflow._resolve_context_requests(
                [
                    {
                        "type": "function",
                        "symbol": "CVMapSchedule::m_lastTask",
                        "reason": "错误地把字段当函数",
                    },
                    {
                        "type": "field",
                        "symbol": "CVMapSchedule::m_lastTask",
                        "reason": "查看字段声明",
                    },
                    {
                        "type": "field",
                        "symbol": "CVMapSchedule::m_lastTask",
                        "reason": "重复请求",
                    },
                ],
                code_roots=[tmp],
                max_requests=5,
            )
        self.assertEqual(len(result), 3)
        self.assertFalse(result[0]["success"])
        self.assertTrue(result[0]["rejected"])
        self.assertEqual(result[0]["reject_reason"], "type_mismatch")
        self.assertTrue(result[1]["success"])
        self.assertEqual(result[1]["context_type"], "field")
        self.assertTrue(result[1]["matches"])
        self.assertFalse(result[2]["success"])
        self.assertTrue(result[2]["skipped"])
        self.assertEqual(result[2]["skip_reason"], "duplicate_request")

        prompt = BaseCrashAnalysisWorkflow._build_context_loop_prompt(
            "原始提示词",
            "上一轮分析",
            2,
            result,
            handled_context=result,
        )
        self.assertIn("## 已处理的上下文请求", prompt)
        self.assertIn("### 已成功补充", prompt)
        self.assertIn("### 已拒绝", prompt)
        self.assertIn("### 已跳过", prompt)
        self.assertIn("不得再次放入 `context_requests`", prompt)

    def test_parse_context_requests_from_fenced_json(self):
        text = """分析略
```json
{
  "need_more_context": true,
  "context_requests": [
    {"type": "function", "symbol": "Foo::bar", "reason": "查看生命周期", "priority": "high"}
  ]
}
```
"""
        parsed = BaseCrashAnalysisWorkflow._parse_context_requests(text)
        self.assertTrue(parsed["need_more_context"])
        self.assertEqual(len(parsed["context_requests"]), 1)
        self.assertEqual(parsed["context_requests"][0]["symbol"], "Foo::bar")

    def test_cli_prompt_mode_flag(self):
        from cli.main import build_parser

        parser = build_parser()
        args = parser.parse_args(["--crash-log", "-"])
        self.assertEqual(args.prompt_mode, "analysis")
        args_fix = parser.parse_args(["--crash-log", "-", "--prompt-mode", "fix"])
        self.assertEqual(args_fix.prompt_mode, "fix")

    def test_cli_agent_loop_flags(self):
        from cli.main import build_parser

        parser = build_parser()
        args = parser.parse_args(["--crash-log", "-"])
        self.assertIsNone(args.agent_loop)
        # 默认 0 表示让 workflow 自行根据 prompt_mode 决定
        self.assertEqual(args.max_agent_rounds, 0)
        args_loop = parser.parse_args(
            [
                "--crash-log",
                "-",
                "--agent-loop",
                "context_loop",
                "--max-agent-rounds",
                "3",
                "--max-context-requests-per-round",
                "4",
            ]
        )
        self.assertEqual(args_loop.agent_loop, "context_loop")
        self.assertEqual(args_loop.max_agent_rounds, 3)
        self.assertEqual(args_loop.max_context_requests_per_round, 4)


if __name__ == "__main__":
    unittest.main()
