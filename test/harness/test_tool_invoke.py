"""Tests for unified tool invocation."""
from __future__ import annotations

import unittest

from tool_system.runtime import RunTrace
from services.tool_invoke import invoke_tool, snippet_extractor_executor


class TestToolInvoke(unittest.TestCase):
    def test_invoke_tool_records_trace_on_fallback(self):
        trace = RunTrace(run_id="test_run")
        calls: list[tuple[str, dict]] = []

        def executor(name: str, payload: dict) -> dict:
            calls.append((name, payload))
            return {"snippet": ["void f() {}"], "is_complete_function": True}

        out = invoke_tool(
            "snippet_extractor",
            {"file_path": "/tmp/a.cpp", "line_number": 1},
            trace=trace,
            tool_executor=executor,
        )
        self.assertTrue(out.get("is_complete_function"))
        self.assertEqual(calls[0][0], "snippet_extractor")
        self.assertFalse(any(e.get("event") == "tool.success" for e in trace.events))

    def test_snippet_extractor_executor_uses_context(self):
        calls: list[str] = []

        class FakeContext:
            def execute_tool(self, name: str, payload: dict) -> dict:
                calls.append(name)
                return {"snippet": []}

        exec_fn = snippet_extractor_executor(context=FakeContext())
        exec_fn("snippet_extractor", {})
        self.assertEqual(calls, ["snippet_extractor"])


if __name__ == "__main__":
    unittest.main()
