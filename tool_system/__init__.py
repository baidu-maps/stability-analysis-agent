#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tool System 核心模块
"""

from .tool import BaseTool, ToolDefinition
from .skill import BaseSkill, SkillDefinition, SkillContext
from .registry import (
    ToolAndSkillRegistry,
    Priority,
    get_registry,
    set_registry,
    register_tool,
    register_skill
)
from .config import SystemConfig, ToolConfig, SkillConfig, LLMConfig, create_default_config
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
    # Skill
    "BaseSkill",
    "SkillDefinition",
    "SkillContext",
    # Registry
    "ToolAndSkillRegistry",
    "Priority",
    "get_registry",
    "set_registry",
    "register_tool",
    "register_skill",
    # Config
    "SystemConfig",
    "ToolConfig",
    "SkillConfig",
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

def register_all_tools_and_skills(registry=None):
    """注册所有内置工具和技能（延迟导入避免循环依赖）。"""
    from tools import register_all_tools
    from skills import register_all_skills

    if registry is None:
        registry = get_registry()
    register_all_tools(registry)
    register_all_skills(registry)

# 自动注册内置
# register_all_tools_and_skills()