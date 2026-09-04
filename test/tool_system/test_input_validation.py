import unittest
from typing import Any, Dict

from tool_system import (
    BaseTool,
    BaseWorkflow,
    ConfigDrivenExecutor,
    Priority,
    SystemConfig,
    ToolDefinition,
    ToolAndWorkflowRegistry,
    ToolConfig,
    WorkflowConfig,
    WorkflowContext,
    WorkflowDefinition,
    RuntimeBudget,
    RunTrace,
)
from tool_system.agent_runtime import AgentRuntime


class ValidationTool(BaseTool):
    executed = False

    @property
    def definition(self):
        return ToolDefinition(
            name="validation_tool",
            description="test",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            category="test",
        )

    def validate_input(self, input_data):
        return bool(input_data.get("required")), "缺少 required"

    def execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        self.executed = True
        return {"status": "success"}

    def execute_stream(self, input_data):
        self.executed = True
        yield "success"


class ValidationWorkflow(BaseWorkflow):
    solved = False

    @property
    def definition(self):
        return WorkflowDefinition(
            name="validation_workflow",
            description="test",
            problem_type="expected",
            required_tools=[],
            metadata={"problem_type": "expected"},
        )

    def solve(self, problem, context):
        self.solved = True
        return {"status": "success"}


class InputValidationTest(unittest.TestCase):
    def setUp(self):
        self.registry = ToolAndWorkflowRegistry()
        self.tool = ValidationTool()
        self.workflow = ValidationWorkflow()
        self.registry.register("validation_tool", self.tool, Priority.CUSTOM, is_tool=True)
        self.registry.register("validation_workflow", self.workflow, Priority.CUSTOM, is_tool=False)

    def test_runtime_accepts_all_llm_backends_and_records_selected_backend(self):
        class Executor:
            last_run_trace = None
            def execute_workflow(self, workflow, problem):
                return {"status": "success"}

        for engine in ("direct", "langchain", "langgraph"):
            result = AgentRuntime(Executor(), engine=engine).run("x", {})
            self.assertEqual(result["metadata"]["runtime_engine"], engine)

    def test_runtime_rejects_unknown_backend(self):
        with self.assertRaisesRegex(ValueError, "engine must be one of"):
            AgentRuntime(object(), engine="sequential")

    def test_workflow_context_rejects_invalid_input_before_execute(self):
        context = WorkflowContext(None, self.registry, {})

        with self.assertRaisesRegex(ValueError, "输入验证失败"):
            context.execute_tool("validation_tool", {})

        self.assertFalse(self.tool.executed)
        self.assertEqual(len(context.execution_events), 1)
        self.assertEqual(context.execution_events[0]["status"], "failed")

    def test_executor_direct_tool_entrypoint_validates_input(self):
        config = SystemConfig(
            tools=[ToolConfig(name="validation_tool")],
            workflows=[],
        )
        executor = ConfigDrivenExecutor(self.registry, config, llm_adapter=None)

        with self.assertRaisesRegex(ValueError, "input validation failed"):
            executor.execute_tool("validation_tool", {})

        self.assertFalse(self.tool.executed)

    def test_executor_stream_entrypoint_validates_input(self):
        config = SystemConfig(
            tools=[ToolConfig(name="validation_tool")],
            workflows=[],
        )
        executor = ConfigDrivenExecutor(self.registry, config, llm_adapter=None)

        with self.assertRaisesRegex(ValueError, "input validation failed"):
            next(executor.execute_tool_stream("validation_tool", {}))

        self.assertFalse(self.tool.executed)

    def test_executor_validates_workflow_problem_before_solve(self):
        config = SystemConfig(
            tools=[],
            workflows=[WorkflowConfig(name="validation_workflow")],
        )
        executor = ConfigDrivenExecutor(self.registry, config, llm_adapter=None)

        with self.assertRaisesRegex(ValueError, "input validation failed"):
            executor.execute_workflow(
                "validation_workflow", {"problem_type": "unexpected"}
            )

        self.assertFalse(self.workflow.solved)

    def test_workflow_context_records_trace_and_tool_policy(self):
        context = WorkflowContext(None, self.registry, {})
        result = context.execute_tool("validation_tool", {"required": True})

        self.assertEqual(result["status"], "success")
        self.assertEqual(context.trace.run_id, context.execution_events[0]["run_id"])
        self.assertEqual(context.execution_events[0]["event"], "tool.success")
        self.assertTrue(context.execution_events[0]["input_hash"])
        self.assertEqual(context.get_tool_definition("validation_tool").risk, "read_only")

    def test_runtime_budget_rejects_calls_before_execution(self):
        trace = RunTrace(budget=RuntimeBudget(max_tool_calls=1))
        context = WorkflowContext(None, self.registry, {}, trace=trace)
        context.execute_tool("validation_tool", {"required": True})

        with self.assertRaisesRegex(RuntimeError, "runtime budget exceeded"):
            context.execute_tool("validation_tool", {"required": True})
        self.assertTrue(self.tool.executed)
        self.assertEqual(trace.budget.tool_calls, 2)


if __name__ == "__main__":
    unittest.main()
