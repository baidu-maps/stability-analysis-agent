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
        self.assertIn("STABLE_MARKER", prompt)
        self.assertIn("DELTA_MARKER", prompt)
        self.assertIn("LEDGER_MARKER", prompt)
        self.assertIn("## 本轮任务", prompt)
        self.assertIn("本轮必须输出最终分析", prompt)
        self.assertIn('{"agent_can_fetch_more": false, "context_requests": []}', prompt)

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


if __name__ == "__main__":
    unittest.main()
