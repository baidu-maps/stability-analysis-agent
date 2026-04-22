#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tool System 核心模块
"""

from .tool import BaseTool, ToolDefinition
from .workflow import BaseWorkflow, WorkflowDefinition, WorkflowContext
from .registry import (
    ToolAndWorkflowRegistry,
    Priority,
    get_registry,
    set_registry,
    register_tool,
    register_workflow
)
from .config import SystemConfig, ToolConfig, WorkflowConfig, LLMConfig, create_default_config
from .executor import ConfigDrivenExecutor, create_executor, create_executor_from_config_file
from .llm.llm_adapter import (
    BaseLLMAdapter,
    DirectLLMAdapter,
    LangChainLLMAdapter,
    LangGraphLLMAdapter,
    LLMAdapterFactory,
    LLMResponse
)

__all__ = [
    # Tool
    "BaseTool",
    "ToolDefinition",
    # Workflow
    "BaseWorkflow",
    "WorkflowDefinition",
    "WorkflowContext",
    # Registry
    "ToolAndWorkflowRegistry",
    "Priority",
    "get_registry",
    "set_registry",
    "register_tool",
    "register_workflow",
    # Config
    "SystemConfig",
    "ToolConfig",
    "WorkflowConfig",
    "LLMConfig",
    "create_default_config",
    # Executor
    "ConfigDrivenExecutor",
    "create_executor",
    "create_executor_from_config_file",
    # LLM
    "BaseLLMAdapter",
    "DirectLLMAdapter",
    "LangChainLLMAdapter",
    "LangGraphLLMAdapter",
    "LLMAdapterFactory",
    "LLMResponse",
]

def register_all_tools_and_workflows(registry=None):
    """注册所有内置工具和工作流（延迟导入避免循环依赖）。"""
    from tools import register_all_tools
    from workflows import register_all_workflows

    if registry is None:
        registry = get_registry()
    register_all_tools(registry)
    register_all_workflows(registry)

# 自动注册内置
# register_all_tools_and_workflows()
