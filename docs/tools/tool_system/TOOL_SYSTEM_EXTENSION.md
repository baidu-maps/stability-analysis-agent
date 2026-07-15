# Tool System 扩展指南

本文档介绍如何扩展 Tool System。

## 扩展方式

1. **新增 Tool** - 添加新的基础能力
2. **新增 Workflow** - 添加新的问题解决方案
3. **替换内置** - 覆盖已有的 Tool/Workflow 实现
4. **自定义 LLM** - 添加新的 LLM 适配器
5. **本地插件** - 通过 `extensions/` 目录或 Python 入口点把外部实现接入

## 仓库级示例：`extensions/`

仓库自带可复制的官方模板：

```
extensions/
├── __init__.py                      # register_all()：启动时自动发现
├── tools/
│   ├── example_tool.py              # 自定义 Tool 模板
│   └── my_custom_tool.py            # （自己新增）
└── workflows/
    ├── example_workflow.py          # 自定义 Workflow 模板
    └── my_custom_workflow.py        # （自己新增）
```

`extensions/tools/__init__.py` 与 `extensions/workflows/__init__.py` 默认会 import
包内所有非 `_` 开头、非 `example_*` 的 Python 模块，因此新建 `my_*.py` 后无需额外
配置，下一次运行 `sa-agent` 时它就会被自动加载并注册到全局 `tool_system` Registry。

### 示例：新增自定义 Tool

```python
from tool_system import BaseTool, ToolDefinition, register_tool, Priority


@register_tool(priority=Priority.EXTENSION)
class MyCustomTool(BaseTool):
    """自定义工具"""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="my_custom_tool",
            description="自定义工具描述",
            input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
            output_schema={"type": "object", "properties": {"output": {"type": "string"}}},
            category="custom",
            version="0.1.0",
        )

    def execute(self, input_data):
        return {"output": f"处理: {input_data.get('input')}"}
```

完整可运行模板见 [`extensions/tools/example_tool.py`](../../../extensions/tools/example_tool.py)。

### 示例：新增自定义 Workflow

```python
from tool_system import (
    BaseWorkflow, WorkflowDefinition, WorkflowContext,
    register_workflow, Priority,
)


@register_workflow(priority=Priority.EXTENSION)
class MyCustomWorkflow(BaseWorkflow):
    """自定义工作流"""

    @property
    def definition(self) -> WorkflowDefinition:
        return WorkflowDefinition(
            name="my_custom_workflow",
            description="自定义工作流描述",
            problem_type="custom_problem",
            required_tools=["crash_log_parser"],
            version="0.1.0",
        )

    def solve(self, problem, context: WorkflowContext):
        # 调用工具
        result = context.execute_tool("crash_log_parser", {
            "log_content": problem["crash_log"]
        })
        return {"status": "success", "result": result}
```

完整可运行模板见 [`extensions/workflows/example_workflow.py`](../../../extensions/workflows/example_workflow.py)。

## 用户级扩展发现

除了仓库级 `extensions/`，以下位置也会被 `extensions.register_all()` 扫描：

| 路径 | 用途 |
|------|------|
| `~/.config/stability-analysis-agent/extensions/` | 默认用户级扩展目录（环境变量 `STABILITY_AGENT_USER_EXT_DIR` 可覆盖） |
| `<cwd>/.stability-analysis-agent/extensions/` | 跟着项目走的工作区级扩展 |
| `STABILITY_AGENT_EXT_DIRS`（PATH 列表分隔符）| 追加额外的扩展目录 |
| `stability_analysis_agent.tools` / `stability_analysis_agent.workflows` Python 入口点 | 通过 `pip install` 安装的第三方插件包 |

示例：把内部符号表服务包装成一个 Tool。

```python
# ~/.config/stability-analysis-agent/extensions/internal_symbol_service.py
from tool_system import BaseTool, ToolDefinition, register_tool, Priority


@register_tool(priority=Priority.EXTENSION, force_override=False)
class InternalSymbolServiceTool(BaseTool):
    @property
    def definition(self):
        return ToolDefinition(
            name="internal_symbol_service",
            description="对接内部符号表服务",
            input_schema={
                "type": "object",
                "properties": {
                    "binary_id": {"type": "string"},
                    "addresses": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["binary_id", "addresses"],
            },
            output_schema={"type": "object", "properties": {"frames": {"type": "array"}}},
            category="resolver",
            version="0.1.0",
        )

    def execute(self, input_data):
        # 通过 HTTP 或 SDK 调用公司内部服务
        return {"frames": [...]}
```

下次运行 `sa-agent` 时它会被自动加载。

## 通过 Python 入口点发布扩展包

在自己的项目 `pyproject.toml` 中声明：

```toml
[project.entry-points."stability_analysis_agent.tools"]
my_custom_tool = "my_pkg.my_tool:MyCustomTool"

[project.entry-points."stability_analysis_agent.workflows"]
my_custom_workflow = "my_pkg.my_workflow:MyCustomWorkflow"
```

执行 `pip install ./my_pkg` 后，用户运行 `sa-agent` 时你的 Tool / Workflow 会自动出现在
全局 Registry 中，并按声明的 `priority` 参与覆盖决策。

## 示例：替换内置

```python
from tool_system import Priority

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

## 设置菜单相关动作

设置菜单中已暴露两个新能力：

- **检查更新（升级 sa-agent 到最新版）**：调用 `cli.upgrade` 自动查询 PyPI、安装方式探测与升级。
- **查看本地缓存 / 清理本地缓存（cli_reports）**：调用 `cli.report_paths`，只读概览或按"全部 / 仅最近 N 份"清理报告目录。

## 相关文档

- [doc/workflows/EXTENSION.md](../workflows/EXTENSION.md) - Workflow 扩展指南
- [doc/workflows/IMPLEMENTATION.md](../workflows/IMPLEMENTATION.md) - 内置实现