import unittest
from unittest.mock import patch

from tool_system.agent_runtime import AgentRuntime
from tool_system.llm.llm_adapter import LLMAdapterFactory, ReplayLLMAdapter


class EngineHarnessTests(unittest.TestCase):
    def test_all_backends_use_factory_and_same_runtime_contract(self):
        config = {"provider": "openai", "model": "test", "api_key": "x", "base_url": "http://127.0.0.1"}
        with patch.object(LLMAdapterFactory, "create", side_effect=lambda value: value) as factory:
            for engine in ("direct", "langchain", "langgraph"):
                adapter = LLMAdapterFactory.create({**config, "engine": engine})
                self.assertEqual(adapter["engine"], engine)
        self.assertEqual(factory.call_count, 3)

    def test_unknown_engine_is_rejected_without_fallback(self):
        with self.assertRaises(ValueError):
            AgentRuntime(object(), engine="sequential")

    def test_fake_adapter_runs_same_lifecycle_for_all_engines(self):
        traces = []
        class Executor:
            def create_run_trace(self, *, engine=None, problem=None):
                from tool_system.runtime import RunTrace
                self.last_run_trace = RunTrace(engine=engine)
                traces.append(self.last_run_trace)
            def execute_workflow(self, workflow, problem):
                self.last_run_trace.emit("llm.finished", kind="llm", name="fake", output_hash="fixed")
                return {"status": "success", "metadata": {}}
        for engine in ("direct", "langchain", "langgraph"):
            AgentRuntime(Executor(), engine=engine).run("crash_analysis", {})
        self.assertEqual([trace.engine for trace in traces], ["direct", "langchain", "langgraph"])
        stage_sequences = [
            [event["name"] for event in trace.events if event["event"] == "stage.transition"]
            for trace in traces
        ]
        self.assertEqual(stage_sequences, [stage_sequences[0]] * 3)
        self.assertEqual(stage_sequences[0], ["observe", "analyze", "plan", "decide"])

    def test_offline_replay_is_network_free_for_every_engine(self):
        for engine in ("direct", "langchain", "langgraph"):
            adapter = LLMAdapterFactory.create({
                "engine": engine,
                "provider": "offline_replay",
                "responses": [{"content": engine, "usage": {"input_tokens": 2, "output_tokens": 1}}],
            })
            self.assertIsInstance(adapter, ReplayLLMAdapter)
            response = adapter.chat([{"role": "user", "content": "ignored"}])
            self.assertEqual(response.content, engine)
            with self.assertRaisesRegex(RuntimeError, "responses exhausted"):
                adapter.chat([])


if __name__ == "__main__":
    unittest.main()
