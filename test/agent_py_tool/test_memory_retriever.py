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

    def test_prompt_mode_default_fix(self):
        self.assertEqual(BaseCrashAnalysisWorkflow._resolve_prompt_mode({}), "fix")
        self.assertEqual(
            BaseCrashAnalysisWorkflow._resolve_prompt_mode({"prompt_mode": "fix"}),
            "fix",
        )
        self.assertEqual(
            BaseCrashAnalysisWorkflow._resolve_prompt_mode({"prompt_mode": "analysis"}),
            "analysis",
        )
        self.assertEqual(
            BaseCrashAnalysisWorkflow._resolve_prompt_mode({"prompt_mode": "unknown"}),
            "fix",
        )

    def test_build_prompt_final_tip_analysis_mode_does_not_force_fix_code(self):
        wf = iOSCrashAnalyzeWorkflow()
        text = wf._build_prompt_final_tip(
            {},
            {"resolved_frames": []},
            {"crash_summary": {"error_type": "SIGSEGV"}},
            problem={"prompt_mode": "analysis"},
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
        self.assertEqual(BaseCrashAnalysisWorkflow._resolve_agent_loop({}), "single")
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
            problem={"agent_loop": "context_loop", "max_agent_rounds": 2, "prompt_mode": "analysis"},
        )
        self.assertIn("context_requests", text)
        self.assertIn("agent_can_fetch_more", text)

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
        self.assertIn("如果还需要 Agent 自动补充上下文", weak_text)
        self.assertIn("只输出需要 Agent 补充什么内容，以及为什么需要这些内容", weak_text)
        self.assertIn("当输出 `agent_can_fetch_more=true` 时，禁止输出最终分析报告", weak_text)
        self.assertIn("证据充分且上下文完整时，可给出直接可用的 C/C++ 修复代码块", weak_text)
        self.assertNotIn("不要输出 C/C++ 修复代码块", weak_text)
        self.assertGreater(
            weak_text.index("不可用上下文边界"),
            weak_text.index("## 必须遵守的规则"),
        )

    def test_context_loop_prompt_deduplicates_previous_requests(self):
        base_prompt = "\n".join(
            [
                "## 函数源码",
                "",
                "#### 函数源码: void Foo::bar()",
                "- 文件: /tmp/Foo.cpp",
                "- 代码片段:",
                "void Foo::bar() {}",
                "",
                "# 崩溃分析任务",
                "请基于以上信息，并严格遵循前文要求，给出专业的崩溃分析。",
            ]
        )
        resolved = [
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
        ]
        prompt = BaseCrashAnalysisWorkflow._build_context_loop_prompt(
            base_prompt,
            1,
            resolved,
            accumulated_context=resolved,
        )
        self.assertIn("## 其它代码上下文", prompt)
        self.assertIn("#### 其它代码上下文: VTaskQueue::PushTask（function）", prompt)
        self.assertIn("- 状态: 未定位", prompt)
        self.assertIn("## 本轮任务", prompt)
        self.assertNotIn("上一轮模型分析摘要如下", prompt)
        self.assertNotIn("上一轮模型要求补充的信息", prompt)
        self.assertNotIn("## 已处理的上下文请求", prompt)
        self.assertNotIn("## 第 1 轮补充上下文", prompt)
        self.assertLess(prompt.index("## 其它代码上下文"), prompt.index("# 崩溃分析任务"))

        final_prompt = BaseCrashAnalysisWorkflow._build_context_loop_prompt(
            base_prompt,
            4,
            resolved,
            accumulated_context=resolved,
            is_final_round=True,
        )
        self.assertIn("当前已经达到允许的最大多轮次数", final_prompt)
        self.assertIn("不得再请求 Agent 补充上下文", final_prompt)

        early_final_prompt = BaseCrashAnalysisWorkflow._build_context_loop_prompt(
            base_prompt,
            2,
            resolved,
            accumulated_context=resolved,
            is_final_round=True,
            early_final_reason="all_requests_blocked",
        )
        self.assertIn("均已由 Agent 处理过", early_final_prompt)
        self.assertNotIn("当前已经达到允许的最大多轮次数", early_final_prompt)

    def test_build_pre_round_add_res_payload(self):
        payload = BaseCrashAnalysisWorkflow._build_pre_round_add_res(
            source_round=0,
            target_round=1,
            resolved_context=[
                {
                    "request": {
                        "type": "function",
                        "symbol": "Foo::bar",
                        "reason": "test",
                        "priority": "high",
                    },
                    "success": True,
                    "file": "/tmp/Foo.cpp",
                    "snippet_start_line": 10,
                    "snippet_end_line": 20,
                    "function_signature": "void Foo::bar()",
                },
                {
                    "request": {"type": "references", "symbol": "CVList::RemoveAll()"},
                    "success": False,
                    "lookup_exhausted": True,
                    "error": "此前已尝试但未定位",
                },
            ],
        )
        self.assertEqual(payload["source_round"], 0)
        self.assertEqual(payload["target_round"], 1)
        self.assertEqual(len(payload["requests"]), 2)
        self.assertEqual(payload["requests"][0]["status"], "located")
        self.assertTrue(payload["requests"][0]["located"])
        self.assertEqual(payload["requests"][1]["status"], "lookup_exhausted")
        self.assertFalse(payload["requests"][1]["located"])

    def test_all_context_requests_blocked_helper(self):
        blocked = [
            {"success": False, "skipped": True, "skip_reason": "duplicate_request", "request": {"symbol": "A"}},
            {"success": False, "rejected": True, "request": {"symbol": "B"}},
        ]
        failed_lookup = [
            {"success": False, "request": {"symbol": "C"}, "error": "未定位"},
        ]
        exhausted_failed = [
            {"success": False, "lookup_exhausted": True, "request": {"symbol": "C"}, "error": "此前已尝试但未定位"},
        ]
        mixed = blocked + [{"success": True, "request": {"symbol": "D"}}]
        self.assertTrue(BaseCrashAnalysisWorkflow._all_context_requests_blocked(blocked))
        self.assertFalse(BaseCrashAnalysisWorkflow._all_context_requests_blocked(failed_lookup))
        self.assertTrue(BaseCrashAnalysisWorkflow._all_context_requests_blocked(exhausted_failed))
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

        base_prompt = "\n".join(
            [
                "## 函数源码",
                "",
                "#### 函数源码: void CVMapSchedule::onTaskEventHandler(CVTask*)",
                "- 文件: /tmp/MapSchedule.cpp",
                "- 代码片段:",
                "void CVMapSchedule::onTaskEventHandler(CVTask* task) {}",
                "",
                "# 崩溃分析任务",
                "请基于以上信息，并严格遵循前文要求，给出专业的崩溃分析。",
            ]
        )
        prompt = BaseCrashAnalysisWorkflow._build_context_loop_prompt(
            base_prompt,
            2,
            result,
            accumulated_context=result,
        )
        self.assertIn("## 其它代码上下文", prompt)
        self.assertIn("#### 其它代码上下文: CVMapSchedule::m_lastTask（field）", prompt)
        self.assertIn("- 状态: 已定位", prompt)
        self.assertIn("## 本轮任务", prompt)
        self.assertNotIn("## 已处理的上下文请求", prompt)

    def test_parse_context_requests_from_fenced_json(self):
        text = """分析略
```json
{
  "agent_can_fetch_more": true,
  "context_requests": [
    {"type": "function", "symbol": "Foo::bar", "reason": "查看生命周期", "priority": "high"}
  ]
}
```
"""
        parsed = BaseCrashAnalysisWorkflow._parse_context_requests(text)
        self.assertTrue(parsed["agent_can_fetch_more"])
        self.assertTrue(parsed["need_more_context"])
        self.assertEqual(len(parsed["context_requests"]), 1)
        self.assertEqual(parsed["context_requests"][0]["symbol"], "Foo::bar")
        self.assertEqual(
            parsed["context_requests"][0]["expected_return_form"], "function_source"
        )

    def test_parse_context_requests_expected_return_form(self):
        text = """```json
{
  "agent_can_fetch_more": true,
  "context_requests": [
    {
      "type": "field",
      "symbol": "CVMapControl::m_Layers",
      "expected_return_form": "member_declaration",
      "fulfillment_note": "需要容器类型声明",
      "reason": "确认 layers 类型",
      "priority": "high"
    }
  ]
}
```"""
        parsed = BaseCrashAnalysisWorkflow._parse_context_requests(text)
        req = parsed["context_requests"][0]
        self.assertEqual(req["expected_return_form"], "member_declaration")
        self.assertEqual(req["fulfillment_note"], "需要容器类型声明")

    def test_pre_round_add_res_includes_return_form_metadata(self):
        resolved = [
            {
                "request": {
                    "type": "field",
                    "symbol": "CBaseLayer::m_iRef",
                    "reason": "t",
                    "priority": "high",
                    "expected_return_form": "member_declaration",
                },
                "success": True,
                "context_type": "field",
                "matches": [
                    {
                        "file": "/tmp/BaseLayer.h",
                        "line_number": 1,
                        "match_kind": "declaration",
                        "line_text": "VAtomicInt32 m_iRef;",
                    }
                ],
            }
        ]
        payload = BaseCrashAnalysisWorkflow._build_pre_round_add_res(
            source_round=0,
            target_round=1,
            resolved_context=resolved,
        )
        entry = payload["requests"][0]
        self.assertEqual(entry["expected_return_form"], "member_declaration")
        self.assertEqual(entry["actual_return_form"], "member_declaration")
        self.assertTrue(entry.get("fulfillment_matched"))

    def test_parse_context_requests_legacy_need_more_context(self):
        text = """```json
{
  "need_more_context": true,
  "context_requests": [
    {"type": "function", "symbol": "Foo::bar", "reason": "legacy", "priority": "high"}
  ]
}
```"""
        parsed = BaseCrashAnalysisWorkflow._parse_context_requests(text)
        self.assertTrue(parsed["agent_can_fetch_more"])

    def test_duplicate_failed_context_request_stays_unlocated(self):
        outcomes: dict = {}
        req = {
            "type": "references",
            "symbol": "CVList::RemoveAll()",
            "reason": "查看实现",
            "priority": "medium",
        }
        first = BaseCrashAnalysisWorkflow._resolve_context_requests(
            [req],
            code_roots=[],
            max_requests=5,
            request_outcomes=outcomes,
        )
        self.assertFalse(first[0]["success"])
        self.assertFalse(first[0].get("skipped"))
        second = BaseCrashAnalysisWorkflow._resolve_context_requests(
            [req],
            code_roots=[],
            max_requests=5,
            request_outcomes=outcomes,
        )
        self.assertFalse(second[0]["success"])
        self.assertTrue(second[0].get("lookup_exhausted"))
        self.assertFalse(second[0].get("skipped"))

    def test_qualified_release_prefers_correct_class_not_demo(self):
        with tempfile.TemporaryDirectory() as tmp:
            demo = Path(tmp) / "demo" / "win32" / "apptest" / "cloudcontrol" / "CloudControlTest.cpp"
            demo.parent.mkdir(parents=True, exist_ok=True)
            demo.write_text(
                "\n".join(
                    [
                        "VVoid CCloudControlTest::Release()",
                        "{",
                        "}",
                    ]
                ),
                encoding="utf-8",
            )
            src = Path(tmp) / "engine-dev" / "src" / "app" / "map" / "basemap" / "common" / "BaseLayer.cpp"
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text(
                "\n".join(
                    [
                        "VULong CBaseLayer::Release() {",
                        "  return 0;",
                        "}",
                    ]
                ),
                encoding="utf-8",
            )
            result = BaseCrashAnalysisWorkflow._resolve_context_requests(
                [
                    {
                        "type": "function",
                        "symbol": "CBaseLayer::Release",
                        "reason": "查看 Release 实现",
                        "priority": "high",
                    }
                ],
                code_roots=[tmp],
                max_requests=5,
            )
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["success"], result[0].get("error"))
        self.assertIn("BaseLayer.cpp", str(result[0].get("file") or ""))
        snippet = "\n".join(result[0].get("snippet") or [])
        self.assertIn("CBaseLayer::Release", snippet)
        self.assertNotIn("CCloudControlTest::Release", snippet)

    def test_qualified_invoke_not_confused_with_unrelated_invoke(self):
        with tempfile.TemporaryDirectory() as tmp:
            decoy = (
                Path(tmp)
                / "engine-dev"
                / "src"
                / "app"
                / "walk"
                / "antengine"
                / "base"
                / "thread"
                / "thread_manager.h"
            )
            decoy.parent.mkdir(parents=True, exist_ok=True)
            decoy.write_text(
                "\n".join(
                    [
                        "class ThreadManager {",
                        "public:",
                        "    bool Invoke(WALK_TID tid, const std::function<void()> &func);",
                        "};",
                    ]
                ),
                encoding="utf-8",
            )
            src = (
                Path(tmp)
                / "engine-dev"
                / "src"
                / "app"
                / "map"
                / "basemap"
                / "vmap"
                / "VMapControl.cpp"
            )
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text(
                "\n".join(
                    [
                        "VVoid CVMapControl::Invoke(const std::function<void ()> &&fn) {",
                        "  fn();",
                        "}",
                    ]
                ),
                encoding="utf-8",
            )
            result = BaseCrashAnalysisWorkflow._resolve_context_requests(
                [
                    {
                        "type": "function",
                        "symbol": "CVMapControl::Invoke",
                        "reason": "确认异步调度",
                        "priority": "high",
                    }
                ],
                code_roots=[tmp],
                max_requests=5,
            )
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["success"], result[0].get("error"))
        self.assertIn("VMapControl.cpp", str(result[0].get("file") or ""))
        snippet = "\n".join(result[0].get("snippet") or [])
        self.assertIn("CVMapControl::Invoke", snippet)
        self.assertNotIn("ThreadManager", snippet)

    def test_qualified_field_prefers_class_member_declaration(self):
        with tempfile.TemporaryDirectory() as tmp:
            decoy = (
                Path(tmp)
                / "engine-dev"
                / "demo"
                / "win32"
                / "enginetest"
                / "mapcontrol"
                / "MapControl.cpp"
            )
            decoy.parent.mkdir(parents=True, exist_ok=True)
            decoy.write_text(
                "\n".join(
                    [
                        "class CVMapControl {",
                        "public:",
                        "    int m_iRef;",
                        "};",
                        "CVMapControl::CVMapControl() { m_iRef = 0; }",
                        "void CVMapControl::Release() { if (m_iRef == 0) return; m_iRef--; }",
                    ]
                ),
                encoding="utf-8",
            )
            header = (
                Path(tmp)
                / "engine-dev"
                / "inc"
                / "app"
                / "map"
                / "basemap"
                / "common"
                / "BaseLayer.h"
            )
            header.parent.mkdir(parents=True, exist_ok=True)
            header.write_text(
                "\n".join(
                    [
                        "class CBaseLayer {",
                        "public:",
                        "    CBaseLayer();",
                        "protected:",
                        "    VAtomicInt32 m_iRef;",
                        "};",
                    ]
                ),
                encoding="utf-8",
            )
            result = BaseCrashAnalysisWorkflow._resolve_context_requests(
                [
                    {
                        "type": "field",
                        "symbol": "CBaseLayer::m_iRef",
                        "reason": "确认成员类型",
                        "priority": "high",
                    }
                ],
                code_roots=[tmp],
                max_requests=5,
            )
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["success"], result[0].get("error"))
        matches = result[0].get("matches") or []
        self.assertTrue(matches)
        first = matches[0]
        self.assertIn("BaseLayer.h", str(first.get("file") or ""))
        self.assertEqual(first.get("match_kind"), "declaration")
        self.assertIn("VAtomicInt32", str(first.get("line_text") or ""))
        self.assertNotIn("MapControl.cpp", str(first.get("file") or ""))

    def test_qualified_field_layers_returns_container_declaration(self):
        with tempfile.TemporaryDirectory() as tmp:
            header = (
                Path(tmp)
                / "engine-dev"
                / "src"
                / "app"
                / "map"
                / "basemap"
                / "vmap"
                / "VMapControl.h"
            )
            header.parent.mkdir(parents=True, exist_ok=True)
            header.write_text(
                "\n".join(
                    [
                        "class CVMapControl {",
                        "protected:",
                        "    CVList<CBaseLayer*, CBaseLayer*> m_Layers;",
                        "};",
                    ]
                ),
                encoding="utf-8",
            )
            result = BaseCrashAnalysisWorkflow._resolve_context_requests(
                [
                    {
                        "type": "field",
                        "symbol": "CVMapControl::m_Layers",
                        "reason": "确认 layers 容器类型",
                        "priority": "high",
                    }
                ],
                code_roots=[tmp],
                max_requests=5,
            )
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["success"], result[0].get("error"))
        matches = result[0].get("matches") or []
        self.assertTrue(matches)
        snippet = "\n".join(matches[0].get("context") or [])
        self.assertIn("m_Layers", snippet)
        self.assertIn("CVList", snippet)
        self.assertEqual(matches[0].get("match_kind"), "declaration")

    def test_destructor_without_parens_still_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "engine-dev" / "src" / "app" / "map" / "basemap" / "common" / "BaseLayer.cpp"
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text(
                "\n".join(
                    [
                        "CBaseLayer::~CBaseLayer() {",
                        "  m_flag = -1;",
                        "}",
                    ]
                ),
                encoding="utf-8",
            )
            result = BaseCrashAnalysisWorkflow._resolve_context_requests(
                [
                    {
                        "type": "function",
                        "symbol": "CBaseLayer::~CBaseLayer",
                        "reason": "查看析构",
                        "priority": "high",
                    }
                ],
                code_roots=[tmp],
                max_requests=5,
            )
        self.assertTrue(result[0]["success"], result[0].get("error"))

    def test_member_method_reference_not_rejected_as_package_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = (
                Path(tmp)
                / "engine-dev"
                / "src"
                / "app"
                / "map"
                / "basemap"
                / "vmap"
                / "VMapControl.cpp"
            )
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text(
                "\n".join(
                    [
                        "void CVMapControl::ReleaseAllLayers() {",
                        "  m_Layers.RemoveAll();",
                        "}",
                    ]
                ),
                encoding="utf-8",
            )
            for symbol in ("m_Layers.RemoveAll", "m_Layers.RemoveAll()"):
                result = BaseCrashAnalysisWorkflow._resolve_context_requests(
                    [
                        {
                            "type": "references",
                            "symbol": symbol,
                            "reason": "查看 RemoveAll 调用",
                            "priority": "high",
                        }
                    ],
                    code_roots=[tmp],
                    max_requests=5,
                )
                self.assertEqual(len(result), 1, symbol)
                self.assertTrue(result[0]["success"], (symbol, result[0].get("error")))
                self.assertIn("RemoveAll", "\n".join(result[0].get("matches", [{}])[0].get("context") or []))

    def test_template_remove_all_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            header = Path(tmp) / "engine-dev" / "inc" / "vi" / "vos" / "VTempl.h"
            header.parent.mkdir(parents=True, exist_ok=True)
            header.write_text(
                "\n".join(
                    [
                        "class CVList {",
                        "};",
                        "template<class TYPE, class ARG_TYPE>",
                        "VVoid CVList<TYPE, ARG_TYPE>::RemoveAll() {",
                        "  VDestructElements<TYPE>(V_NULL, 0);",
                        "}",
                    ]
                ),
                encoding="utf-8",
            )
            result = BaseCrashAnalysisWorkflow._resolve_context_requests(
                [
                    {
                        "type": "function",
                        "symbol": "CVList<CBaseLayer*, CBaseLayer*>::RemoveAll",
                        "reason": "查看 RemoveAll",
                        "priority": "high",
                    }
                ],
                code_roots=[tmp],
                max_requests=5,
            )
        self.assertTrue(result[0]["success"], result[0].get("error"))
        snippet = "\n".join(result[0].get("snippet") or [])
        self.assertIn("CVList", snippet)
        self.assertIn("RemoveAll", snippet)

    def test_cvlist_remove_all_without_template_args_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            header = Path(tmp) / "engine-dev" / "inc" / "vi" / "vos" / "VTempl.h"
            header.parent.mkdir(parents=True, exist_ok=True)
            header.write_text(
                "\n".join(
                    [
                        "template<class TYPE, class ARG_TYPE>",
                        "class CVList {",
                        "};",
                        "template<class TYPE, class ARG_TYPE>",
                        "VVoid CVList<TYPE, ARG_TYPE>::RemoveAll() {",
                        "  VDestructElements<TYPE>(V_NULL, 0);",
                        "}",
                        "CVList<int, int>::CVList() {}",
                    ]
                ),
                encoding="utf-8",
            )
            result = BaseCrashAnalysisWorkflow._resolve_context_requests(
                [
                    {
                        "type": "function",
                        "symbol": "CVList::RemoveAll",
                        "reason": "查看 RemoveAll",
                        "priority": "high",
                    }
                ],
                code_roots=[tmp],
                max_requests=5,
            )
        self.assertTrue(result[0]["success"], result[0].get("error"))
        snippet = "\n".join(result[0].get("snippet") or [])
        self.assertIn("RemoveAll", snippet)
        self.assertNotIn("CVList() {}", snippet)

    def test_field_cvlist_returns_class_declaration_not_ctor(self):
        with tempfile.TemporaryDirectory() as tmp:
            header = Path(tmp) / "engine-dev" / "inc" / "vi" / "vos" / "VTempl.h"
            header.parent.mkdir(parents=True, exist_ok=True)
            header.write_text(
                "\n".join(
                    [
                        "template<class TYPE, class ARG_TYPE>",
                        "class CVList {",
                        "public:",
                        "  CVList();",
                        "  VVoid RemoveAll();",
                        "};",
                        "template<class TYPE, class ARG_TYPE>",
                        "CVList<TYPE, ARG_TYPE>::CVList() {}",
                    ]
                ),
                encoding="utf-8",
            )
            result = BaseCrashAnalysisWorkflow._resolve_context_requests(
                [
                    {
                        "type": "field",
                        "symbol": "CVList",
                        "reason": "查看 CVList 容器结构",
                        "priority": "high",
                    }
                ],
                code_roots=[tmp],
                max_requests=5,
            )
        self.assertTrue(result[0]["success"], result[0].get("error"))
        matches = result[0].get("matches") or []
        self.assertTrue(matches)
        self.assertEqual(matches[0].get("match_kind"), "class_declaration")
        snippet = "\n".join(matches[0].get("context") or [])
        self.assertIn("class CVList", snippet)
        self.assertNotIn("CVList<TYPE, ARG_TYPE>::CVList", snippet)
        enriched = BaseCrashAnalysisWorkflow._attach_return_form_metadata(result[0])
        self.assertTrue(enriched.get("fulfillment_matched"))

    def test_template_function_duplicate_key_across_symbol_forms(self):
        with tempfile.TemporaryDirectory() as tmp:
            header = Path(tmp) / "engine-dev" / "inc" / "vi" / "vos" / "VTempl.h"
            header.parent.mkdir(parents=True, exist_ok=True)
            header.write_text(
                "\n".join(
                    [
                        "template<class TYPE, class ARG_TYPE>",
                        "VVoid CVList<TYPE, ARG_TYPE>::RemoveAll() {",
                        "  VDestructElements<TYPE>(V_NULL, 0);",
                        "}",
                    ]
                ),
                encoding="utf-8",
            )
            outcomes: dict = {}
            first = BaseCrashAnalysisWorkflow._resolve_context_requests(
                [
                    {
                        "type": "function",
                        "symbol": "CVList<CBaseLayer*, CBaseLayer*>::RemoveAll",
                        "reason": "查看 RemoveAll",
                    }
                ],
                code_roots=[tmp],
                max_requests=5,
                request_outcomes=outcomes,
            )
            second = BaseCrashAnalysisWorkflow._resolve_context_requests(
                [
                    {
                        "type": "function",
                        "symbol": "CVList::RemoveAll",
                        "reason": "重复写法",
                    }
                ],
                code_roots=[tmp],
                max_requests=5,
                request_outcomes=outcomes,
            )
        self.assertTrue(first[0]["success"])
        self.assertFalse(second[0]["success"])
        self.assertEqual(second[0].get("skip_reason"), "duplicate_request")

    def test_bare_m_layers_prefers_vmap_over_gmap(self):
        with tempfile.TemporaryDirectory() as tmp:
            gmap = Path(tmp) / "engine-dev" / "src" / "app" / "map" / "basemap" / "gmap" / "GMapControl.h"
            vmap = Path(tmp) / "engine-dev" / "src" / "app" / "map" / "basemap" / "vmap" / "VMapControl.h"
            gmap.parent.mkdir(parents=True, exist_ok=True)
            vmap.parent.mkdir(parents=True, exist_ok=True)
            gmap.write_text("class CGMapControl {\n  int m_Layers;\n};\n", encoding="utf-8")
            vmap.write_text("class CVMapControl {\n  CVList<CBaseLayer*, CBaseLayer*> m_Layers;\n};\n", encoding="utf-8")
            result = BaseCrashAnalysisWorkflow._resolve_context_requests(
                [
                    {
                        "type": "field",
                        "symbol": "m_Layers",
                        "reason": "查看 layers 类型",
                        "priority": "high",
                    }
                ],
                code_roots=[tmp],
                max_requests=5,
                stack_priority_classes=["CVMapControl"],
            )
        self.assertTrue(result[0]["success"])
        self.assertIn("VMapControl.h", str(result[0]["matches"][0].get("file") or ""))

    def test_destructor_context_request_prefers_dtor_not_ctor(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "BaseLayer.cpp"
            src.write_text(
                "\n".join(
                    [
                        "class CBaseLayer {",
                        "public:",
                        "  CBaseLayer();",
                        "  ~CBaseLayer();",
                        "};",
                        "CBaseLayer::CBaseLayer() {",
                        "  m_flag = 0;",
                        "}",
                        "CBaseLayer::~CBaseLayer() {",
                        "  m_flag = -1;",
                        "}",
                    ]
                ),
                encoding="utf-8",
            )
            result = BaseCrashAnalysisWorkflow._resolve_context_requests(
                [
                    {
                        "type": "function",
                        "symbol": "CBaseLayer::~CBaseLayer()",
                        "reason": "查看析构",
                        "priority": "high",
                    }
                ],
                code_roots=[tmp],
                max_requests=5,
            )
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["success"])
        snippet = "\n".join(result[0].get("snippet") or [])
        self.assertIn("~CBaseLayer", snippet)
        self.assertNotIn("CBaseLayer::CBaseLayer()", snippet)

    def test_cli_prompt_mode_flag(self):
        from cli.main import build_parser

        parser = build_parser()
        args = parser.parse_args(["--crash-log", "-"])
        self.assertEqual(args.prompt_mode, "fix")
        args_analysis = parser.parse_args(["--crash-log", "-", "--prompt-mode", "analysis"])
        self.assertEqual(args_analysis.prompt_mode, "analysis")
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
