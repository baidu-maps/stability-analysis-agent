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
)


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


if __name__ == "__main__":
    unittest.main()
