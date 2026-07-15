#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
示例：自定义 Tool（可删除 / 复用）

演示如何编写一个自定义 Tool 并把它注册到全局 Registry：

- 使用 ``@register_tool(priority=Priority.CUSTOM)`` 让内置实现仍然生效，
  仅在用户主动启用时覆盖；需要真正覆盖时同时声明 ``force_override=True``。
- ``definition`` 描述 Tool 的元数据（JSON Schema），LangGraph Agent 会读取它
  来进行工具选择。
- ``execute`` 是核心逻辑；返回结构化 ``Dict`` 即可。

实际开发时可在本目录新建一个 ``my_*.py`` 文件，复制本模板修改即可。
"""

from __future__ import annotations

from typing import Any, Dict

from tool_system import BaseTool, Priority, ToolDefinition, register_tool


@register_tool(priority=Priority.EXTENSION)
class ExampleCustomTool(BaseTool):
    """示例自定义 Tool：回显输入并附加工具名标记。"""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="example_custom_tool",
            description="示例自定义工具（可作为插件开发的起点）",
            input_schema={
                "type": "object",
                "properties": {
                    "input_text": {
                        "type": "string",
                        "description": "需要处理的原始文本",
                    },
                },
                "required": ["input_text"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "success / error"},
                    "result": {"type": "string", "description": "处理后的结果"},
                },
                "required": ["status", "result"],
            },
            category="custom",
            version="0.1.0",
        )

    def execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        text = str(input_data.get("input_text", "") or "")
        return {
            "status": "success",
            "result": f"[ExampleCustomTool] processed: {text}",
        }
