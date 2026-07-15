#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
示例：自定义 Workflow（可删除 / 复用）

演示如何编写一个自定义 Workflow 并把它注册到全局 Registry：

- ``definition`` 描述 Workflow 的元数据与 ``required_tools``。
- ``solve`` 通过 ``WorkflowContext`` 调用已注册的 Tool / LLM 来完成分析。
- ``priority=Priority.CUSTOM`` 允许覆盖内置 workflow（同名时）。

实际开发时可在本目录新建一个 ``my_*.py`` 文件，复制本模板修改即可。
"""

from __future__ import annotations

from typing import Any, Dict

from tool_system import (
    BaseWorkflow,
    Priority,
    WorkflowContext,
    WorkflowDefinition,
    register_workflow,
)


@register_workflow(priority=Priority.EXTENSION)
class ExampleCustomWorkflow(BaseWorkflow):
    """示例自定义 Workflow：先调用 ``crash_log_parser``，再返回结构化结果。"""

    @property
    def definition(self) -> WorkflowDefinition:
        return WorkflowDefinition(
            name="example_custom_workflow",
            description="示例自定义工作流（可作为插件开发的起点）",
            problem_type="custom_analysis",
            required_tools=["crash_log_parser"],
            version="0.1.0",
        )

    def solve(self, problem: Dict[str, Any], context: WorkflowContext) -> Dict[str, Any]:
        # 1. 调用已注册的 Tool
        log_content = problem.get("crash_log") or problem.get("log_content") or ""
        parse_result = context.execute_tool(
            "crash_log_parser",
            {"log_content": log_content},
        )

        # 2. 如需 LLM，可通过 context.call_llm(...) / call_llm_stream(...)

        return {
            "status": "success",
            "analysis": "[ExampleCustomWorkflow] analysis complete",
            "parse_result": parse_result,
        }
