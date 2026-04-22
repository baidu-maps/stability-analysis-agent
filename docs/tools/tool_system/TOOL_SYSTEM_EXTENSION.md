# Tool System 扩展指南

本文档介绍如何扩展 Tool System。

## 扩展方式

1. **新增 Tool** - 添加新的基础能力
2. **新增 Workflow** - 添加新的问题解决方案
3. **替换内置** - 覆盖已有的 Tool/Workflow 实现
4. **自定义 LLM** - 添加新的 LLM 适配器

## 示例：新增自定义 Tool

```python
from tools.core.tool_system import BaseTool, ToolDefinition, register_tool, Priority

@register_tool(priority=Priority.CUSTOM)
class MyCustomTool(BaseTool):
    """自定义工具"""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="my_custom_tool",
            description="自定义工具描述",
            input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
            output_schema={"type": "object", "properties": {"output": {"type": "string"}}},
            category="custom"
        )

    def execute(self, input_data):
        return {"output": f"处理: {input_data.get('input')}"}
```

## 示例：新增自定义 Workflow

```python
from tools.core.tool_system import BaseWorkflow, WorkflowDefinition, register_workflow, Priority, WorkflowContext

@register_workflow(priority=Priority.CUSTOM)
class MyCustomWorkflow(BaseWorkflow):
    """自定义工作流"""

    @property
    def definition(self) -> WorkflowDefinition:
        return WorkflowDefinition(
            name="my_custom_workflow",
            description="自定义工作流描述",
            problem_type="custom_problem",
            required_tools=["crash_log_parser"]
        )

    def solve(self, problem, context: WorkflowContext):
        # 调用工具
        result = context.execute_tool("crash_log_parser", {
            "log_content": problem["crash_log"]
        })

        return {"status": "success", "result": result}
```

## 示例：替换内置

```python
from tools.core.tool_system import Priority

# 替换 crash_log_parser
class MyCustomParser(BaseTool):
    @property
    def definition(self):
        return ToolDefinition(
            name="crash_log_parser",  # 使用相同名称
            description="自定义解析器",
            ...
        )

    ...

# 强制覆盖
registry.register(
    "crash_log_parser",
    MyCustomParser(),
    priority=Priority.CUSTOM,
    force_override=True,
    is_tool=True
)
```

## 相关文档

- [doc/workflows/EXTENSION.md](../workflows/EXTENSION.md) - Workflow 扩展指南
- [doc/workflows/IMPLEMENTATION.md](../workflows/IMPLEMENTATION.md) - 内置实现