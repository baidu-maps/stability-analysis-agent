from __future__ import annotations

import time
import unittest
from pathlib import Path

from services.policy import PolicyEngine
from tool_system.runtime import RunTrace
from tool_system.tool import BaseTool, ToolDefinition
from tool_system.tool_gateway import ToolExecutionGateway


class SlowTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="slow_tool",
            description="sleep",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            category="test",
            timeout_sec=0.05,
            timeout_enforcement="best_effort",
        )

    def execute(self, input_data):
        time.sleep(0.2)
        return {"ok": True}


class SubprocessTimeoutTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="subprocess_timeout_tool",
            description="subprocess timeout",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            category="test",
            timeout_sec=0.05,
            timeout_enforcement="subprocess",
        )

    def execute(self, input_data):
        import subprocess

        timeout = float(input_data.get("timeout_sec") or input_data.get("_gateway_timeout_sec") or 0.05)
        try:
            subprocess.run(
                ["sleep", "1"],
                capture_output=True,
                text=True,
                timeout=max(0.01, timeout),
            )
        except subprocess.TimeoutExpired:
            return {"status": "timeout"}
        return {"status": "completed"}


class RootedTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="rooted_tool",
            description="root scoped",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            category="test",
            allowed_roots=[str(Path("/allowed").resolve())],
        )

    def execute(self, input_data):
        return {"ok": True}


class ToolGatewayTests(unittest.TestCase):
    def test_timeout_is_enforced(self):
        trace = RunTrace("timeout-test")
        gateway = ToolExecutionGateway(trace=trace)
        with self.assertRaises(TimeoutError):
            gateway.execute("slow_tool", SlowTool(), {})
        self.assertTrue(any(evt.get("timed_out") for evt in trace.events if evt.get("event") == "tool.failed"))

    def test_tool_allowed_roots_are_enforced(self):
        policy = PolicyEngine()
        gateway = ToolExecutionGateway(policy=policy, trace=RunTrace("root-test"))
        with self.assertRaises(PermissionError):
            gateway.execute(
                "rooted_tool",
                RootedTool(),
                {"workspace": "/outside/path", "file_path": "/outside/path/a.cpp"},
            )

    def test_subprocess_timeout_uses_process_kill(self):
        trace = RunTrace("subprocess-timeout")
        gateway = ToolExecutionGateway(trace=trace)
        started = time.perf_counter()
        result = gateway.execute("subprocess_timeout_tool", SubprocessTimeoutTool(), {})
        elapsed = time.perf_counter() - started
        self.assertEqual(result.get("status"), "timeout")
        self.assertLess(elapsed, 0.5)
        success = next(evt for evt in trace.events if evt.get("event") == "tool.success")
        self.assertEqual(success.get("timeout_enforcement"), "subprocess")

    def test_cost_class_is_emitted_on_success(self):
        trace = RunTrace("cost-test")

        class CheapTool(BaseTool):
            @property
            def definition(self) -> ToolDefinition:
                return ToolDefinition(
                    name="cheap_tool",
                    description="cheap",
                    input_schema={"type": "object"},
                    output_schema={"type": "object"},
                    category="test",
                    cost_class="medium",
                )

            def execute(self, input_data):
                return {"ok": True}

        gateway = ToolExecutionGateway(trace=trace)
        gateway.execute("cheap_tool", CheapTool(), {})
        success = next(evt for evt in trace.events if evt.get("event") == "tool.success")
        self.assertEqual(success.get("cost_class"), "medium")


if __name__ == "__main__":
    unittest.main()
