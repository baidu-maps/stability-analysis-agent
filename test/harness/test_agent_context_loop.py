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
from tool_system.runtime import RunTrace


class AgentContextLoopTests(unittest.TestCase):
    def _hooks(self, *, parse_result=None, resolve_result=None, llm_texts=None):
        texts = list(llm_texts or ["round-0"])
        calls = {"llm": 0, "resolve": 0}

        def call_llm(prompt, *, round_index):
            calls["llm"] += 1
            idx = min(calls["llm"] - 1, len(texts) - 1)
            return SimpleNamespace(content=texts[idx]), f"prompt-{round_index}"

        def parse_requests(analysis_text):
            if parse_result is not None:
                payload = dict(parse_result)
                payload.setdefault("raw_payload", dict(parse_result))
                payload.setdefault("next_action", dict(parse_result.get("next_action") or {}))
                payload.setdefault("requested_more", bool(parse_result.get("agent_can_fetch_more")))
                payload.setdefault("has_control_contract", isinstance(parse_result.get("agent_can_fetch_more"), bool))
                return payload
            return {
                "context_requests": [{"symbol": "foo"}],
                "agent_can_fetch_more": True,
                "raw_payload": {"agent_can_fetch_more": True},
                "next_action": {},
            }

        def resolve_request(request):
            calls["resolve"] += 1
            values = resolve_result or [{"success": True, "symbol": "foo"}]
            return {**values[0], "request": request}

        @contextmanager
        def around_resolve(round_index):
            yield

        hooks = CallableContextLoopHooks(
            call_llm=call_llm,
            on_evidence=lambda resolved, round_index: None,
            around_resolve=around_resolve,
        )
        registry = ContextResolverRegistry(
            CallableContextResolver(request_type, resolve_request)
            for request_type in ("function", "field", "references", "callers")
        )
        engine = ContextEngine(
            ContextEngineConfig(),
            registry,
            decision_parser=parse_requests,
        )
        return hooks, calls, engine

    def test_single_round_when_not_context_loop(self):
        hooks, _, engine = self._hooks(llm_texts=['{"summary_zh":"ok","root_cause":"null","confidence":0.8}'])
        out = run_agent_context_loop(
            config=ContextLoopConfig(max_rounds=3, agent_loop_mode="single"),
            hooks=hooks,
            initial_prompt="start",
            context_engine=engine,
        )
        self.assertEqual(out.analysis_text, '{"summary_zh":"ok","root_cause":"null","confidence":0.8}')
        self.assertEqual(len(out.rounds), 1)
        self.assertIsNotNone(out.structured_analysis)
        self.assertEqual(out.structured_analysis.get("root_cause"), "null")

    def test_structured_analysis_degrades_without_json(self):
        trace = RunTrace("analysis-degraded")
        hooks, _, engine = self._hooks(llm_texts=["plain prose only"])
        out = run_agent_context_loop(
            config=ContextLoopConfig(max_rounds=1, agent_loop_mode="single"),
            hooks=hooks,
            initial_prompt="start",
            trace=trace,
            context_engine=engine,
        )
        self.assertIsNone(out.structured_analysis)
        self.assertIn("schema_violation", [e.get("event") for e in trace.events])

    def test_propose_fix_terminates_early(self):
        hooks, calls, engine = self._hooks(
            parse_result={
                "context_requests": [],
                "agent_can_fetch_more": False,
                "next_action": {"kind": "propose_fix", "reason": "ready"},
            },
            llm_texts=['{"agent_can_fetch_more": false, "next_action": {"kind": "propose_fix"}}'],
        )
        out = run_agent_context_loop(
            config=ContextLoopConfig(max_rounds=5, agent_loop_mode="context_loop"),
            hooks=hooks,
            initial_prompt="start",
            context_engine=engine,
        )
        self.assertEqual(out.termination_reason, "ready_to_fix")
        self.assertEqual(calls["resolve"], 0)

        hooks, _, engine = self._hooks(
            parse_result={
                "context_requests": [],
                "invalid_context_requests": [{"bad": True}],
                "degraded": True,
            },
            llm_texts=["needs-more", "unused"],
        )
        trace = RunTrace("ctx-loop-degraded")
        out = run_agent_context_loop(
            config=ContextLoopConfig(max_rounds=3, agent_loop_mode="context_loop"),
            hooks=hooks,
            initial_prompt="start",
            trace=trace,
            context_engine=engine,
        )
        self.assertEqual(out.analysis_text, "unused")
        self.assertEqual(len(out.rounds), 2)
        self.assertEqual(out.termination_reason, "invalid_schema")
        events = [item.get("event") for item in trace.events]
        self.assertIn("agent.schema_degraded", events)

    def test_invalid_schema_final_with_control_contract_becomes_model_final(self):
        """Final analysis after a schema miss should not block repair via invalid_schema."""
        hooks, _, engine = self._hooks(llm_texts=["need more", "final"])
        texts = [
            "need more",
            '{"agent_can_fetch_more": false, "context_requests": [], "analysis": "final report"}',
        ]
        parse_by_round = [
            {
                "context_requests": [],
                "invalid_context_requests": [{"error": "unsupported request type: file"}],
                "agent_can_fetch_more": True,
                "requested_more": True,
                "has_control_contract": True,
                "raw_payload": {"agent_can_fetch_more": True},
                "degraded": False,
                "next_action": {},
            },
            {
                "context_requests": [],
                "invalid_context_requests": [],
                "agent_can_fetch_more": False,
                "requested_more": False,
                "has_control_contract": True,
                "raw_payload": {"agent_can_fetch_more": False},
                "degraded": False,
                "next_action": {},
            },
        ]
        calls = {"llm": 0}

        def call_llm(prompt, *, round_index):
            calls["llm"] += 1
            idx = min(calls["llm"] - 1, len(texts) - 1)
            return SimpleNamespace(content=texts[idx]), f"prompt-{round_index}"

        def parse_requests(analysis_text):
            idx = max(0, min(calls["llm"] - 1, len(parse_by_round) - 1))
            return dict(parse_by_round[idx])

        hooks.call_llm = call_llm
        engine.decision_parser = parse_requests
        out = run_agent_context_loop(
            config=ContextLoopConfig(max_rounds=3, agent_loop_mode="context_loop"),
            hooks=hooks,
            initial_prompt="start",
            context_engine=engine,
        )
        self.assertEqual(out.termination_reason, "model_final")
        self.assertIn("final report", out.analysis_text)

    def test_mixed_valid_and_invalid_requests_resolves_valid_items(self):
        hooks, calls, engine = self._hooks(
            parse_result={
                "context_requests": [{"type": "function", "symbol": "foo"}],
                "invalid_context_requests": [{"error": "unsupported request type"}],
                "agent_can_fetch_more": True,
                "raw_payload": {"agent_can_fetch_more": True},
                "degraded": True,
            },
            llm_texts=["requests", "final"],
        )
        out = run_agent_context_loop(
            config=ContextLoopConfig(max_rounds=2, agent_loop_mode="context_loop"),
            hooks=hooks,
            initial_prompt="start",
            context_engine=engine,
        )
        self.assertEqual(calls["resolve"], 1)
        self.assertEqual(out.analysis_text, "final")
        self.assertEqual(out.termination_reason, "max_rounds")

    def test_max_rounds_one_uses_final_only_prompt(self):
        hooks, _, engine = self._hooks(llm_texts=["final"])
        prompts = []
        original = hooks.call_llm

        def capture(prompt, *, round_index):
            prompts.append(prompt)
            return original(prompt, round_index=round_index)

        hooks.call_llm = capture
        out = run_agent_context_loop(
            config=ContextLoopConfig(max_rounds=1, agent_loop_mode="context_loop"),
            hooks=hooks,
            initial_prompt="# 崩溃分析任务\n分析",
            context_engine=engine,
        )
        self.assertEqual(len(out.rounds), 1)
        self.assertIn("本轮必须输出最终分析", prompts[0])
        self.assertEqual(out.termination_reason, "max_rounds")

    def test_round_checkpoint_callback_runs_once_per_round(self):
        hooks, _, engine = self._hooks(
            llm_texts=["request", "final"],
            parse_result={
                "context_requests": [{"type": "function", "symbol": "foo"}],
                "agent_can_fetch_more": True,
                "raw_payload": {"agent_can_fetch_more": True},
            },
        )
        completed = []
        hooks.on_round_complete = lambda round_index, payload: completed.append(round_index)
        out = run_agent_context_loop(
            config=ContextLoopConfig(max_rounds=2, agent_loop_mode="context_loop"),
            hooks=hooks,
            initial_prompt="start",
            context_engine=engine,
        )
        self.assertEqual(completed, [0, 1])
        self.assertEqual(len(out.rounds), 2)

    def test_context_engine_injects_only_round_delta(self):
        registry = ContextResolverRegistry([
            CallableContextResolver(
                "function",
                lambda request: {"request": request, "success": True, "snippet": ["void added() {}"]},
            )
        ])
        engine = ContextEngine(
            ContextEngineConfig(max_chars=8000),
            registry,
            format_resolution=lambda item: "void added() {}",
        )
        resolved = engine.resolve_requests(
            [{"type": "function", "symbol": "added"}], round_index=0,
        )
        delta = engine.evidence_delta(resolved, round_index=1)
        prompt = engine.build_prompt(
            "INITIAL_UNIQUE\n# 崩溃分析任务\n分析",
            evidence_delta=delta,
        )
        self.assertEqual(prompt.count("INITIAL_UNIQUE"), 1)
        self.assertEqual(prompt.count("void added() {}"), 1)
        self.assertIn("## 已处理的上下文请求", prompt)

    def test_context_engine_budgets_sections_without_damaging_control_contract(self):
        registry = ContextResolverRegistry([
            CallableContextResolver(
                "function",
                lambda request: {"request": request, "success": True},
            )
        ])
        engine = ContextEngine(ContextEngineConfig(max_chars=4000), registry)
        prompt = engine.build_prompt(
            "STABLE_MARKER\n" + ("stable context " * 1000) + "\n# 崩溃分析任务\n分析",
            evidence_delta=[
                {"kind": "source_code", "content": "DELTA_MARKER\n" + ("delta " * 1000)},
                {"kind": "request_ledger", "content": "LEDGER_MARKER\n" + ("ledger " * 1000)},
            ],
            is_final_round=True,
        )
        self.assertLessEqual(len(prompt), 4000)
        self.assertNotIn("delta truncated", prompt)
        self.assertNotIn("pinned section truncated", prompt)
        self.assertIn("## 本轮任务", prompt)
        self.assertIn("本轮必须输出最终分析", prompt)
        self.assertIn('{"agent_can_fetch_more": false, "context_requests": []}', prompt)

    def test_compress_stable_context_pins_sync_member_section(self):
        head = "HEAD_FUNCS\n" + ("fn body line\n" * 400)
        skel = (
            "### 崩溃所属类骨架（DemoEngineControl）\n"
            "- 文件: demo_engine_control.h\n"
            "- 疑似同步原语成员（优先复用，禁止假设新的 mutex/lock 成员名）："
            "m_clRouteInfoMutex, m_clCurPosInfoMutex\n"
            "- 同步/共享指针相关声明摘录（来自类骨架，优先复用这些成员）：\n"
            "```cpp\n"
            "\tCNMutex\t\t\t\t\t\t\tm_clRouteInfoMutex;\t\t\t/**< 路线信息保护 */\n"
            "\tDemoRoute* \t\t\t\t\t\tm_pCurrentItem;\t\t\t\t/**< 当前路线 (仅是引用) */\n"
            "```\n"
            "```cpp\n"
            + ("    void MethodPad();\n" * 200)
            + "};\n```\n"
        )
        keep = (
            "### 关键同步与共享成员（DemoEngineControl，压缩保活）\n"
            "- 疑似同步原语成员（禁止臆造新 mutex/lock）：m_clRouteInfoMutex\n"
            "- 声明摘录：\n"
            "```cpp\n"
            "CNMutex m_clRouteInfoMutex;\n"
            "DemoRoute* m_pCurrentItem;\n"
            "```\n"
        )
        write_keep = (
            "### 写路径函数源码（压缩保活，可纳入需要修改的函数）\n"
            "#### 写路径函数源码: void HandleClearItem()\n"
            "- 代码片段:\n"
            "void HandleClearItem() {\n"
            "  m_pCurrentItem = nullptr;\n"
            "}\n"
        )
        tail = "# 崩溃分析任务\n分析规则尾部\n"
        text = head + skel + keep + write_keep + tail
        compressed = ContextEngine._compress_stable_context(text, 3500)
        self.assertLessEqual(len(compressed), 3500)
        self.assertNotIn("stable context compressed by budget", compressed)
        self.assertNotIn("pinned section truncated", compressed)
        self.assertNotIn("fn body line", compressed)
        self.assertIn("未放入，因为超出容量", compressed)
        self.assertIn("m_clRouteInfoMutex", compressed)
        self.assertIn("关键同步与共享成员", compressed)
        self.assertIn("写路径函数源码", compressed)
        self.assertIn("HandleClearItem", compressed)
        self.assertIn("m_pCurrentItem = nullptr", compressed)
        self.assertIn("# 崩溃分析任务", compressed)
        # 保活段应紧挨任务节之前
        task_at = compressed.find("# 崩溃分析任务")
        sync_at = compressed.find("关键同步与共享成员")
        self.assertGreaterEqual(sync_at, 0)
        self.assertLess(sync_at, task_at)

    def test_compress_stable_context_prefers_write_path_for_container_copy(self):
        """容器拷贝竞态：展开成员写路径须压过旁路/同步噪音被保活。"""
        head = (
            "## 崩溃摘要\n"
            "- 错误类型: SIGABRT\n"
            "- 箭头展开容器成员: m_renderSteps\n"
            "- 已定位展开成员的写/就地修改路径（可作为 Copy 竞态主证据）。\n"
        )
        filler = "### 其它证据\n" + ("evidence pad line\n" * 300)
        crash_line_keep = (
            "### 崩溃行关键源码（压缩保活，优先分析崩溃行实参求值与被调）\n"
            "#### 函数源码: DemoArray<RenderStep> Reader()\n"
            "routes = ptr->GetSteps();\n"
            + ("crash pad line\n" * 250)
        )
        sync_keep = (
            "### 关键同步与共享成员（Owner，压缩保活）\n"
            "- 疑似同步原语成员：m_clRouteInfoMutex\n"
        )
        write_keep = (
            "### 写路径函数源码（压缩保活，可纳入需要修改的函数）\n"
            "#### 写路径函数源码: void DemoRoute::Build()\n"
            "- 代码片段:\n"
            "void DemoRoute::Build() {\n"
            "  m_renderSteps.Add(step);\n"
            "}\n"
        )
        tail = "# 崩溃分析任务\n输出契约\n"
        text = head + filler + crash_line_keep + sync_keep + write_keep + tail
        compressed = ContextEngine._compress_stable_context(text, 1600)
        self.assertLessEqual(len(compressed), 1600)
        self.assertNotIn("stable context compressed by budget", compressed)
        self.assertNotIn("evidence pad line", compressed)
        self.assertNotIn("crash pad line", compressed)
        self.assertIn("未放入，因为超出容量", compressed)
        self.assertIn("写路径函数源码", compressed)
        self.assertIn("DemoRoute::Build", compressed)
        self.assertIn("m_renderSteps.Add", compressed)
        self.assertIn("# 崩溃分析任务", compressed)
        write_at = compressed.find("DemoRoute::Build")
        task_at = compressed.find("# 崩溃分析任务")
        self.assertGreaterEqual(write_at, 0)
        self.assertLess(write_at, task_at)

    def test_compress_stable_context_pins_crash_line_over_write_path(self):
        """heap/delete 场景：崩溃行 arg_eval 保活应优先于大块写路径。"""
        head = (
            "## 崩溃摘要\n"
            "- 错误类型: SIGABRT\n"
            "- signal: heap allocator abort / operator_delete\n"
        )
        crash_line_keep = (
            "### 崩溃行关键源码（压缩保活，优先分析崩溃行实参求值与被调）\n"
            "- 说明: 优先保留参数求值被调\n"
            "#### 函数源码: const char* GetShaderValue(int a, int b)\n"
            "- 来源标签: 参数求值被调, 崩溃行被调\n"
            "- 代码片段:\n"
            "const char* GetShaderValue(int a, int b) {\n"
            "  g_strShaderVert = str1 + str2;\n"
            "  return g_strShaderVert.c_str();\n"
            "}\n"
        )
        write_keep = (
            "### 写路径函数源码（压缩保活，可纳入需要修改的函数）\n"
            "#### 写路径函数源码: void GLRenderShader::getAttributeInfo()\n"
            "- 代码片段:\n"
            + ("  m_vecAttrDesc[nAttrLoc + i] = desc;\n" * 200)
        )
        tail = "# 崩溃分析任务\n输出契约\n"
        text = head + crash_line_keep + write_keep + tail
        compressed = ContextEngine._compress_stable_context(text, 4500)
        self.assertLessEqual(len(compressed), 4500)
        self.assertNotIn("stable context compressed by budget", compressed)
        self.assertNotIn("m_vecAttrDesc[nAttrLoc + i] = desc;", compressed)
        self.assertIn("未放入，因为超出容量", compressed)
        self.assertIn("GetShaderValue", compressed)
        self.assertIn("g_strShaderVert", compressed)
        self.assertIn("崩溃行关键源码", compressed)
        self.assertIn("# 崩溃分析任务", compressed)
        # 写路径可被挤掉或截断，但不能反过来挤掉 GetShaderValue
        crash_at = compressed.find("GetShaderValue")
        task_at = compressed.find("# 崩溃分析任务")
        self.assertGreaterEqual(crash_at, 0)
        self.assertLess(crash_at, task_at)

    def test_compress_stable_context_pins_bypass_after_crash_line(self):
        head = (
            "## 崩溃摘要\n"
            "- signal: heap allocator abort / operator_delete\n"
        )
        crash_line = (
            "### 崩溃行关键源码（压缩保活）\n"
            "#### 函数源码: const char* GetShaderValue()\n"
            "- 来源标签: 参数求值被调\n"
            "g_strShaderVert = x; return g_strShaderVert.c_str();\n"
        )
        bypass = (
            "### 同文件旁路候选源码（压缩保活，次于崩溃行；供旁路/次要假设）\n"
            "#### 函数源码: void GLRenderShader::getAttributeInfo()\n"
            "m_vecAttrDesc[nAttrLoc + 3] = desc;\n"
        )
        write_keep = (
            "### 写路径函数源码（压缩保活）\n"
            "#### 写路径函数源码: void noise()\n"
            + ("noise line\n" * 80)
        )
        tail = "# 崩溃分析任务\n规则\n"
        text = head + crash_line + bypass + write_keep + tail
        compressed = ContextEngine._compress_stable_context(text, 5000)
        self.assertIn("GetShaderValue", compressed)
        self.assertIn("g_strShaderVert", compressed)
        self.assertIn("getAttributeInfo", compressed)
        self.assertIn("m_vecAttrDesc", compressed)
        self.assertLess(
            compressed.find("GetShaderValue"),
            compressed.find("getAttributeInfo"),
        )

    def test_truncate_crash_line_pin_keeps_lifetime_hits(self):
        head = (
            "### 崩溃行关键源码（压缩保活）\n"
            "#### 函数源码: const char* GetShaderValue()\n"
            "- 来源标签: 参数求值被调\n"
            "- 代码片段:\n"
            "const char* GetShaderValue() {\n"
        )
        mid = "".join(f'  case {i}: return "s{i}";\n' for i in range(400))
        hit = (
            '  g_strShaderVert = "anim";\n'
            "  return g_strShaderVert.c_str();\n"
            "}\n"
        )
        block = head + mid + hit + "\n# 崩溃分析任务\n输出契约\n"
        out = ContextEngine._compress_stable_context(block, 2500)
        self.assertLessEqual(len(out), 2500)
        self.assertIn("# 崩溃分析任务", out)
        self.assertIn("未放入，因为超出容量", out)
        self.assertNotIn("case 200", out)
        self.assertNotIn("pinned section truncated", out)

    def test_context_engine_ledger_records_rejected_and_duplicate_attempts(self):
        registry = ContextResolverRegistry([
            CallableContextResolver(
                "function",
                lambda request: {"request": request, "success": True},
            )
        ])
        engine = ContextEngine(ContextEngineConfig(), registry)
        request = {"type": "function", "symbol": "Foo::bar"}
        engine.resolve_requests([request], round_index=0)
        engine.resolve_requests([request], round_index=1)
        engine.record_invalid_requests(
            [{"type": "shell", "symbol": "bad", "error": "unsupported request type"}],
            round_index=1,
        )
        entries = engine.session.request_ledger
        function_entry = next(item for item in entries if item["symbol"] == "Foo::bar")
        rejected_entry = next(item for item in entries if item["symbol"] == "bad")
        self.assertEqual([item["status"] for item in function_entry["attempts"]], ["success", "duplicate"])
        self.assertEqual(rejected_entry["status"], "rejected")

    def test_duplicate_failed_request_remains_lookup_exhausted(self):
        registry = ContextResolverRegistry([
            CallableContextResolver(
                "function",
                lambda request: {"request": request, "success": False, "error": "not found"},
            )
        ])
        engine = ContextEngine(ContextEngineConfig(), registry)
        request = {"type": "function", "symbol": "Missing::method"}
        engine.resolve_requests([request], round_index=0)
        duplicate = engine.resolve_requests([request], round_index=1)[0]
        self.assertTrue(duplicate["skipped"])
        self.assertTrue(duplicate["lookup_exhausted"])
        self.assertTrue(engine.all_requests_blocked([duplicate]))

    def test_llm_error_checkpoints_exactly_one_terminal_round(self):
        hooks, _, engine = self._hooks()
        completed = []

        def fail_llm(prompt, *, round_index):
            raise RuntimeError("provider unavailable")

        hooks.call_llm = fail_llm
        hooks.on_round_complete = lambda round_index, payload: completed.append(payload)
        with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
            run_agent_context_loop(
                config=ContextLoopConfig(max_rounds=2, agent_loop_mode="context_loop"),
                hooks=hooks,
                initial_prompt="start",
                context_engine=engine,
            )
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["termination_reason"], "llm_error")
        self.assertEqual(engine.session.termination_reason, "llm_error")

    def test_context_loop_resolves_and_continues(self):
        hooks, calls, engine = self._hooks(
            llm_texts=["fetch-more", "final-analysis"],
            parse_result={
                "context_requests": [{"symbol": "foo"}],
                "agent_can_fetch_more": True,
            },
        )
        trace = RunTrace("ctx-loop-resolve")
        out = run_agent_context_loop(
            config=ContextLoopConfig(max_rounds=3, agent_loop_mode="context_loop"),
            hooks=hooks,
            initial_prompt="start",
            trace=trace,
            context_engine=engine,
        )
        self.assertEqual(out.analysis_text, "final-analysis")
        self.assertGreaterEqual(calls["resolve"], 1)
        events = [item.get("event") for item in trace.events]
        self.assertIn("agent.context_resolved", events)

    def test_context_loop_stops_when_no_request_is_located(self):
        """本轮上下文一个都没定位到时，不再消耗后续模型轮次。"""
        hooks, calls, engine = self._hooks(
            llm_texts=["need-more", "should-not-run"],
            parse_result={
                "context_requests": [{"type": "function", "symbol": "Missing::fn"}],
                "agent_can_fetch_more": True,
            },
            resolve_result=[{"success": False, "error": "未定位到函数定义: Missing::fn"}],
        )
        out = run_agent_context_loop(
            config=ContextLoopConfig(max_rounds=5, agent_loop_mode="context_loop"),
            hooks=hooks,
            initial_prompt="start",
            context_engine=engine,
        )
        self.assertEqual(calls["llm"], 1)
        self.assertEqual(out.analysis_text, "need-more")
        self.assertEqual(out.termination_reason, "all_requests_blocked")

    def test_prior_source_stays_when_new_block_exceeds_capacity(self):
        """先前取到的完整函数继续放入；新的超长函数整段不发，也不标成已看见。"""
        engine = ContextEngine(ContextEngineConfig(max_chars=12000), ContextResolverRegistry([]))
        small = {
            "success": True,
            "snippet": "void Loop(void) {\n  SMALL_LOOP_BODY;\n}\n",
            "request": {"type": "function", "symbol": "CVTaskQueueThread::Loop"},
        }
        huge = {
            "success": True,
            "snippet": "void Init() {\n" + ("INIT_PAD\n" * 4000) + "}\n",
            "request": {"type": "function", "symbol": "CVMapControl::Init"},
        }
        engine.ledger.record(small["request"], small, 0)
        engine.ledger.record(huge["request"], huge, 1)
        engine.evidence_delta([small], round_index=0)
        prompt = engine.build_prompt(
            "## 崩溃摘要\nshort\n\n# 崩溃分析任务\n任务\n",
            evidence_delta=engine.evidence_delta([huge], round_index=1),
        )
        self.assertLessEqual(len(prompt), 12000)
        self.assertIn("SMALL_LOOP_BODY", prompt)
        self.assertNotIn("INIT_PAD", prompt)
        self.assertNotIn("delta truncated", prompt)
        self.assertIn("未放入，因为超出容量", prompt)
        self.assertIn("CVTaskQueueThread::Loop", prompt)
        self.assertIn("已放入提示", prompt)
        self.assertNotIn("`function: CVMapControl::Init`：已放入提示", prompt)
        self.assertIn("`function: CVMapControl::Init`：未放入，因为超出容量", prompt)

    def test_context_loop_stops_when_mandatory_prompt_exceeds_capacity(self):
        """契约和崩溃摘要放不下时停止，不再把残缺摘要发给模型。"""
        hooks, calls, seeded = self._hooks(llm_texts=["should-not-run"])
        engine = ContextEngine(
            ContextEngineConfig(max_chars=4000),
            seeded.resolvers,
            decision_parser=seeded.decision_parser,
        )
        summary = "## 崩溃摘要\n" + ("摘要必须完整保留\n" * 800)
        out = run_agent_context_loop(
            config=ContextLoopConfig(max_rounds=3, agent_loop_mode="context_loop"),
            hooks=hooks,
            initial_prompt=summary + "\n# 崩溃分析任务\n任务\n",
            context_engine=engine,
        )
        self.assertEqual(calls["llm"], 0)
        self.assertEqual(out.termination_reason, "prompt_capacity_exceeded")
        self.assertIn("提示容量不足", out.rounds[-1]["prompt"])
        self.assertNotIn("摘要必须完整保留\n摘要必须完整保留", out.rounds[-1]["prompt"])


if __name__ == "__main__":
    unittest.main()
