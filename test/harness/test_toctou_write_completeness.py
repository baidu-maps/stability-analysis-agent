#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TOCTOU 写路径终局完整性门禁单测。"""

from __future__ import annotations

import unittest

from services.agent_context_loop import (
    CallableContextLoopHooks,
    ContextLoopConfig,
    run_agent_context_loop,
)
from services.context_engine import ContextEngine, ContextEngineConfig
from services.toctou_write_completeness import (
    analysis_claims_shared_state_race,
    list_write_path_keepalive_signatures,
    missing_write_path_fix_signatures,
    should_revise_for_incomplete_write_fixes,
)


class ToctouWriteCompletenessTests(unittest.TestCase):
    """写路径保活解析与缺失补丁判定。"""

    def test_list_write_path_keepalive_signatures(self) -> None:
        """从保活段解析写路径签名。"""
        prompt = (
            "### 写路径函数源码（压缩保活，可纳入需要修改的函数）\n"
            "#### 写路径函数源码: VVoid CNaviEngineControl::HandleRemoveRouteMessage(\n"
            "CNaviEngineControl* pclThis)\n"
            "- 代码片段:\n"
            "VVoid CNaviEngineControl::HandleRemoveRouteMessage(...) {\n"
            "  pclThis->m_pclCurRoute = V_NULL;\n"
            "}\n"
            "\n"
            "# 崩溃分析任务\n"
        )
        # header is single-line in real prompts; use single-line form
        prompt = (
            "### 写路径函数源码（压缩保活，可纳入需要修改的函数）\n"
            "#### 写路径函数源码: "
            "VVoid CNaviEngineControl::HandleRemoveRouteMessage( CNaviEngineControl* pclThis )\n"
            "- 代码片段:\n"
            "void body() {}\n"
            "# 崩溃分析任务\n"
        )
        sigs = list_write_path_keepalive_signatures(prompt)
        self.assertEqual(len(sigs), 1)
        self.assertIn("HandleRemoveRouteMessage", sigs[0])

    def test_missing_when_only_reader_fixed(self) -> None:
        """只交读侧完整函数体时，写路径应被判缺失。"""
        write_sig = (
            "VVoid CNaviEngineControl::HandleRemoveRouteMessage( "
            "CNaviEngineControl* pclThis , const NC_RemoveRoute_Message_t& stMessage )"
        )
        analysis = (
            "根因是共享变量竞态：HandleRemoveRouteMessage 置空 m_pclCurRoute。\n"
            "## 6. 修复建议\n"
            "### 6.1 直接修复（代码级）\n"
            "```cpp\n"
            "CVArray<RenderStep> CNaviEngineControl::GetRenderSteps()\n"
            "{\n"
            "    CVArray<RenderStep> routes;\n"
            "    m_clRouteInfoMutex.Lock(V_INFINITY_WAIT);\n"
            "    if (m_pclCurRoute) {\n"
            "        routes = m_pclCurRoute->GetRenderSteps();\n"
            "    }\n"
            "    m_clRouteInfoMutex.Unlock();\n"
            "    return routes;\n"
            "}\n"
            "```\n"
        )
        self.assertTrue(analysis_claims_shared_state_race(analysis))
        missing = missing_write_path_fix_signatures(analysis, [write_sig])
        self.assertEqual(missing, [write_sig])

    def test_evidence_block_with_lock_does_not_count(self) -> None:
        """根因证据里的写侧「已加锁」幻觉块不得算作已提交修复。"""
        write_sig = "VVoid CNaviEngineControl::HandleRemoveRouteMessage(CNaviEngineControl* pclThis)"
        analysis = (
            "存在竞态。\n"
            "### 3. 证据\n"
            "```cpp\n"
            "VVoid CNaviEngineControl::HandleRemoveRouteMessage(CNaviEngineControl* pclThis)\n"
            "{\n"
            "    pclThis->m_clRouteInfoMutex.Lock(V_INFINITY_WAIT);\n"
            "    pclThis->m_pclCurRoute = V_NULL;\n"
            "    pclThis->m_clRouteInfoMutex.Unlock();\n"
            "}\n"
            "```\n"
            "## 6. 修复建议\n"
            "### 6.1 直接修复（代码级）\n"
            "```cpp\n"
            "CVArray<RenderStep> CNaviEngineControl::GetRenderSteps()\n"
            "{\n"
            "    m_clRouteInfoMutex.Lock(V_INFINITY_WAIT);\n"
            "    CVArray<RenderStep> routes;\n"
            "    if (m_pclCurRoute) { routes = m_pclCurRoute->GetRenderSteps(); }\n"
            "    m_clRouteInfoMutex.Unlock();\n"
            "    return routes;\n"
            "}\n"
            "```\n"
        )
        self.assertEqual(missing_write_path_fix_signatures(analysis, [write_sig]), [write_sig])

    def test_not_missing_when_writer_has_full_block(self) -> None:
        """写路径完整 cpp 块存在时不应续轮。"""
        write_sig = "VVoid CNaviEngineControl::HandleRemoveRouteMessage(CNaviEngineControl* pclThis)"
        analysis = (
            "存在 TOCTOU 竞态。\n"
            "## 修复代码\n"
            "```cpp\n"
            "VVoid CNaviEngineControl::HandleRemoveRouteMessage(CNaviEngineControl* pclThis)\n"
            "{\n"
            "    pclThis->m_clRouteInfoMutex.Lock(V_INFINITY_WAIT);\n"
            "    pclThis->m_pclCurRoute = V_NULL;\n"
            "    pclThis->m_clRouteInfoMutex.Unlock();\n"
            "}\n"
            "```\n"
        )
        self.assertEqual(missing_write_path_fix_signatures(analysis, [write_sig]), [])

    def test_context_loop_forces_revision_then_accepts(self) -> None:
        """首轮只交读侧时续轮；次轮补齐写侧后收工。"""
        from contextlib import contextmanager
        from types import SimpleNamespace

        stable = (
            "前置上下文\n"
            "疑似同步原语成员（禁止臆造新 mutex/lock）：m_clRouteInfoMutex\n"
            "### 写路径函数源码（压缩保活，可纳入需要修改的函数）\n"
            "#### 写路径函数源码: VVoid Foo::HandleRemoveRouteMessage()\n"
            "- 代码片段:\n"
            "VVoid Foo::HandleRemoveRouteMessage() { m_pclCurRoute = V_NULL; }\n"
            "# 崩溃分析任务\n"
        )
        # 控制契约放进 json fence，避免 cpp 花括号干扰 extract_json_object。
        control = (
            "```json\n"
            '{"agent_can_fetch_more": false, "context_requests": []}\n'
            "```\n"
        )
        round0 = (
            "分析：存在共享变量竞态。\n"
            "## 6. 修复建议\n"
            "### 6.1 直接修复（代码级）\n"
            "```cpp\n"
            "CVArray<RenderStep> Foo::GetRenderSteps() {\n"
            "  m_clRouteInfoMutex.Lock(V_INFINITY_WAIT);\n"
            "  CVArray<RenderStep> routes;\n"
            "  if (m_pclCurRoute) {\n"
            "    routes = m_pclCurRoute->GetRenderSteps();\n"
            "  }\n"
            "  m_clRouteInfoMutex.Unlock();\n"
            "  return routes;\n"
            "}\n"
            "```\n"
            + control
        )
        round1 = (
            "分析：竞态需同时改写路径。\n"
            "## 修复代码\n"
            "```cpp\n"
            "CVArray<RenderStep> Foo::GetRenderSteps() {\n"
            "  m_clRouteInfoMutex.Lock(V_INFINITY_WAIT);\n"
            "  CVArray<RenderStep> routes;\n"
            "  if (m_pclCurRoute) {\n"
            "    routes = m_pclCurRoute->GetRenderSteps();\n"
            "  }\n"
            "  m_clRouteInfoMutex.Unlock();\n"
            "  return routes;\n"
            "}\n"
            "```\n"
            "```cpp\n"
            "VVoid Foo::HandleRemoveRouteMessage() {\n"
            "  m_clRouteInfoMutex.Lock(V_INFINITY_WAIT);\n"
            "  m_pclCurRoute = V_NULL;\n"
            "  m_clRouteInfoMutex.Unlock();\n"
            "}\n"
            "```\n"
            + control
        )
        texts = [round0, round1]
        calls = {"n": 0}

        def call_llm(prompt: str, *, round_index: int):
            idx = calls["n"]
            calls["n"] += 1
            return SimpleNamespace(content=texts[idx]), prompt

        @contextmanager
        def around_resolve(round_index):
            yield

        from services.context_engine import ContextResolverRegistry

        engine = ContextEngine(ContextEngineConfig(max_chars=50000), ContextResolverRegistry([]))
        hooks = CallableContextLoopHooks(
            call_llm=call_llm,
            on_evidence=lambda *_a, **_k: None,
            around_resolve=around_resolve,
        )
        result = run_agent_context_loop(
            config=ContextLoopConfig(max_rounds=5, max_requests=8, agent_loop_mode="context_loop"),
            hooks=hooks,
            initial_prompt=stable,
            context_engine=engine,
        )
        self.assertEqual(calls["n"], 2)
        self.assertIn("HandleRemoveRouteMessage", result.analysis_text)
        self.assertEqual(result.termination_reason, "model_final")
        self.assertEqual(len(result.rounds), 2)

    def test_should_revise_helper(self) -> None:
        """综合判定：有保活写体 + 竞态结论 + 缺写侧补丁。"""
        prompt = (
            "### 写路径函数源码（压缩保活，可纳入需要修改的函数）\n"
            "#### 写路径函数源码: VVoid Foo::Writer()\n"
            "# 崩溃分析任务\n"
        )
        analysis = "根因是竞态。\n## 6. 修复建议\n```cpp\nVVoid Foo::Reader(){ return; }\n```\n"
        missing = should_revise_for_incomplete_write_fixes(prompt=prompt, analysis=analysis)
        self.assertTrue(missing)
        self.assertIn("Writer", missing[0])


if __name__ == "__main__":
    unittest.main()
