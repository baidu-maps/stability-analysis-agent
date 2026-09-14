#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修复代码格式门禁与 context-loop 续轮单测。"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from types import SimpleNamespace

from services.agent_context_loop import CallableContextLoopHooks, ContextLoopConfig, run_agent_context_loop
from services.context_engine import (
    CallableContextResolver,
    ContextEngine,
    ContextEngineConfig,
    ContextResolverRegistry,
)
from services.fix_format_completeness import (
    analysis_has_class_skeleton_fix_fence,
    invented_sync_members_in_fix,
    should_revise_for_fix_format,
)
from tool_system.runtime import RunTrace


class FixFormatCompletenessTests(unittest.TestCase):
    """检测 class 骨架与臆造锁。"""

    def test_detects_class_skeleton_with_ellipsis(self):
        analysis = """
## 6. 修复建议
### 6.1 直接修复（代码级）
```cpp
class WalkMapOperator {
private:
    mutable std::mutex m_pBaseMapControlMutex;
    // ... 其他成员 ...
};
```
"""
        self.assertTrue(analysis_has_class_skeleton_fix_fence(analysis))

    def test_detects_invented_mutex_not_in_prompt(self):
        analysis = """
## 修复代码
```cpp
CMapStatus WalkMapOperator::GetMapStatus()
{
    std::lock_guard<std::mutex> lock(m_pBaseMapControlMutex);
    return m_pBaseMapControl->GetMapStatus();
}
```
"""
        prompt = "member_fields: m_pBaseMapControl"
        invented = invented_sync_members_in_fix(analysis, prompt)
        self.assertIn("m_pBaseMapControlMutex", invented)
        feedback = should_revise_for_fix_format(prompt=prompt, analysis=analysis)
        self.assertIsNotNone(feedback)
        self.assertIn("臆造", feedback or "")

    def test_known_mutex_in_prompt_skips_revision(self):
        analysis = """
## 修复代码
```cpp
CMapStatus Foo::Get()
{
    m_clRouteInfoMutex.Lock();
    return m_ptr->Get();
}
```
"""
        prompt = "疑似同步原语成员：m_clRouteInfoMutex"
        self.assertEqual(invented_sync_members_in_fix(analysis, prompt), [])
        self.assertIsNone(should_revise_for_fix_format(prompt=prompt, analysis=analysis))


class FixFormatRevisionLoopTests(unittest.TestCase):
    """context-loop 在格式不合格时续一轮。"""

    def test_context_loop_revises_on_class_skeleton(self):
        bad = """
## 6. 修复建议
### 6.1 直接修复
```cpp
class WalkMapOperator {
    mutable std::mutex m_pBaseMapControlMutex;
    // ... 其他成员 ...
};
```
{"agent_can_fetch_more": false, "context_requests": [], "next_action": {"kind": "final"}}
"""
        good = (
            "最终分析：需人工新增锁成员。\n"
            '{"agent_can_fetch_more": false, "context_requests": [], '
            '"next_action": {"kind": "final"}}'
        )
        calls = {"llm": 0}

        def call_llm(prompt, *, round_index):
            calls["llm"] += 1
            text = bad if calls["llm"] == 1 else good
            return SimpleNamespace(content=text), f"prompt-{round_index}"

        def parse_requests(analysis_text):
            return {
                "context_requests": [],
                "agent_can_fetch_more": False,
                "raw_payload": {"agent_can_fetch_more": False},
                "next_action": {"kind": "final"},
                "requested_more": False,
                "has_control_contract": True,
            }

        @contextmanager
        def around_resolve(round_index):
            yield

        hooks = CallableContextLoopHooks(
            call_llm=call_llm,
            on_evidence=lambda resolved, round_index: None,
            around_resolve=around_resolve,
        )
        registry = ContextResolverRegistry(
            CallableContextResolver(request_type, lambda request: {"success": True})
            for request_type in ("function", "field", "references", "callers")
        )
        engine = ContextEngine(
            ContextEngineConfig(),
            registry,
            decision_parser=parse_requests,
        )
        trace = RunTrace("fix-format-rev")
        out = run_agent_context_loop(
            config=ContextLoopConfig(max_rounds=3, agent_loop_mode="context_loop"),
            hooks=hooks,
            initial_prompt="member_fields: m_pBaseMapControl only",
            trace=trace,
            context_engine=engine,
        )
        self.assertEqual(calls["llm"], 2)
        self.assertIn("需人工", out.analysis_text)
        events = [item.get("event") for item in trace.events]
        self.assertIn("agent.fix_format_revision", events)


if __name__ == "__main__":
    unittest.main()
