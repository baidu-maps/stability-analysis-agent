#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置驱动的执行器
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Generator

from .config import SystemConfig, ToolConfig, SkillConfig
from .registry import ToolAndSkillRegistry, Priority
from .skill import SkillContext
from .llm.llm_adapter import BaseLLMAdapter, LLMAdapterFactory, LLMResponse

logger = logging.getLogger(__name__)


class ConfigDrivenExecutor:
    """配置驱动的执行器"""

    def __init__(self,
                 registry: ToolAndSkillRegistry,
                 config: SystemConfig,
                 llm_adapter: Optional[BaseLLMAdapter] = None):
        """
        初始化执行器

        Args:
            registry: 工具和技能注册表
            config: 系统配置
            llm_adapter: LLM 适配器（可选，不提供则根据配置创建）
        """
        self.registry = registry
        self.config = config

        # 初始化 LLM 适配器
        if llm_adapter is None:
            if config.llm:
                self.llm_adapter = LLMAdapterFactory.create(config.llm.to_dict())
            else:
                logger.warning("No LLM config, creating dummy adapter")
                self.llm_adapter = None
        else:
            self.llm_adapter = llm_adapter

        # 创建 SkillContext
        self._skill_context = SkillContext(
            llm_adapter=self.llm_adapter,
            tool_registry=registry,
            config=config.metadata
        )

        # 实例化缓存
        self._tool_instances: Dict[str, Any] = {}
        self._skill_instances: Dict[str, Any] = {}

        # 执行初始化
        self._initialize()

    def _initialize(self):
        """初始化实例"""
        logger.info("Initializing ConfigDrivenExecutor...")

        # 实例化启用的工具
        for tool_cfg in self.config.get_enabled_tools():
            try:
                tool = self._get_implementation(tool_cfg.name, is_tool=True)
                if tool:
                    # 如果是类，需要实例化
                    if isinstance(tool, type):
                        tool = tool(**tool_cfg.params)
                    self._tool_instances[tool_cfg.name] = tool
                    logger.info(f"Initialized tool: {tool_cfg.name}")
            except Exception as e:
                logger.error(f"Failed to initialize tool {tool_cfg.name}: {e}")

        # 实例化启用的技能
        for skill_cfg in self.config.get_enabled_skills():
            try:
                skill = self._get_implementation(skill_cfg.name, is_tool=False)
                if skill:
                    if isinstance(skill, type):
                        skill = skill(**skill_cfg.params)
                    self._skill_instances[skill_cfg.name] = skill
                    logger.info(f"Initialized skill: {skill_cfg.name}")
            except Exception as e:
                logger.error(f"Failed to initialize skill {skill_cfg.name}: {e}")

    def _get_implementation(self, name: str, is_tool: bool) -> Any:
        """根据配置获取具体实现"""
        collection = self.config.tools if is_tool else self.config.skills
        cfg = next((c for c in collection if c.name == name), None)

        if cfg and cfg.implementation:
            # 使用配置指定的实现
            impl = self.registry.get(cfg.implementation)
            if impl:
                logger.info(f"Using configured implementation for '{name}': {cfg.implementation}")
                return impl
            logger.warning(f"Configured implementation '{cfg.implementation}' not found, falling back to default")

        # 使用默认/最高优先级实现
        return self.registry.get(name)

    # ==================== Tool 执行 ====================

    def execute_tool(self, name: str, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行工具

        Args:
            name: 工具名称
            input_data: 输入数据

        Returns:
            工具执行结果
        """
        tool = self._tool_instances.get(name)
        if tool is None:
            # 尝试动态获取
            tool = self._get_implementation(name, is_tool=True)
            if tool is None:
                raise ValueError(f"Tool '{name}' not found")
            if isinstance(tool, type):
                tool = tool()
                self._tool_instances[name] = tool

        return tool.execute(input_data)

    def execute_tool_stream(self, name: str, input_data: Dict[str, Any]) -> Generator[str, None, None]:
        """执行工具（流式版本，如果有）"""
        tool = self._tool_instances.get(name)
        if tool is None:
            raise ValueError(f"Tool '{name}' not initialized")

        # 如果工具支持流式执行
        if hasattr(tool, "execute_stream"):
            for chunk in tool.execute_stream(input_data):
                yield chunk
        else:
            # 否则返回普通结果
            result = tool.execute(input_data)
            yield str(result)

    # ==================== Skill 执行 ====================

    def execute_skill(self, name: str, problem: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行技能

        Args:
            name: 技能名称
            problem: 问题数据

        Returns:
            技能执行结果
        """
        skill = self._skill_instances.get(name)
        if skill is None:
            # 尝试动态获取
            skill = self._get_implementation(name, is_tool=False)
            if skill is None:
                raise ValueError(f"Skill '{name}' not found")
            if isinstance(skill, type):
                skill = skill()
                self._skill_instances[name] = skill

        # 创建新的 SkillContext（包含当前 LLM 适配器）
        context = SkillContext(
            llm_adapter=self.llm_adapter,
            tool_registry=self.registry,
            config=self.config.metadata
        )

        return skill.solve(problem, context)

    def execute_skill_stream(self, name: str, problem: Dict[str, Any]) -> Generator[str, None, None]:
        """执行技能（流式版本）"""
        skill = self._skill_instances.get(name)
        if skill is None:
            raise ValueError(f"Skill '{name}' not initialized")

        # 如果技能支持流式执行
        if hasattr(skill, "solve_stream"):
            context = SkillContext(
                llm_adapter=self.llm_adapter,
                tool_registry=self.registry,
                config=self.config.metadata
            )
            for chunk in skill.solve_stream(problem, context):
                yield chunk
        else:
            result = self.execute_skill(name, problem)
            yield str(result)

    # ==================== LLM 调用 ====================

    def call_llm(self, prompt: str, **kwargs) -> LLMResponse:
        """直接调用 LLM"""
        if self.llm_adapter is None:
            raise RuntimeError("LLM adapter not initialized")

        messages = [{"role": "user", "content": prompt}]
        return self.llm_adapter.chat(messages, **kwargs)

    def call_llm_stream(self, prompt: str, **kwargs) -> Generator[str, None, None]:
        """流式调用 LLM"""
        if self.llm_adapter is None:
            raise RuntimeError("LLM adapter not initialized")

        messages = [{"role": "user", "content": prompt}]
        return self.llm_adapter.stream(messages, **kwargs)

    # ==================== 信息查询 ====================

    def list_active(self) -> Dict[str, List[str]]:
        """列出当前活跃的工具和技能"""
        return {
            "tools": list(self._tool_instances.keys()),
            "skills": list(self._skill_instances.keys())
        }

    def get_tool_definition(self, name: str) -> Optional[Any]:
        """获取工具定义"""
        tool = self._tool_instances.get(name) or self._get_implementation(name, is_tool=True)
        if tool and hasattr(tool, "definition"):
            return tool.definition
        return None

    def get_skill_definition(self, name: str) -> Optional[Any]:
        """获取技能定义"""
        skill = self._skill_instances.get(name) or self._get_implementation(name, is_tool=False)
        if skill and hasattr(skill, "definition"):
            return skill.definition
        return None

    def validate(self) -> List[str]:
        """验证配置是否完整有效"""
        errors = []

        # 检查 LLM
        if self.llm_adapter is None:
            errors.append("LLM adapter not initialized")

        # 检查工具
        for tool_cfg in self.config.get_enabled_tools():
            impl = self._get_implementation(tool_cfg.name, is_tool=True)
            if impl is None:
                errors.append(f"Tool '{tool_cfg.name}' not found in registry")

        # 检查技能
        for skill_cfg in self.config.get_enabled_skills():
            impl = self._get_implementation(skill_cfg.name, is_tool=False)
            if impl is None:
                errors.append(f"Skill '{skill_cfg.name}' not found in registry")

        return errors


# ==================== 工厂函数 ====================

def create_executor(config: Optional[SystemConfig] = None,
                   registry: Optional[ToolAndSkillRegistry] = None,
                   llm_adapter: Optional[BaseLLMAdapter] = None) -> ConfigDrivenExecutor:
    """
    创建配置驱动的执行器

    Args:
        config: 系统配置（默认从文件加载）
        registry: 工具和技能注册表（默认创建新的）
        llm_adapter: LLM 适配器（默认根据配置创建）

    Returns:
        配置驱动的执行器
    """
    if config is None:
        config = SystemConfig.from_dict({})
        logger.info("Using empty config")

    if registry is None:
        from .builtins import register_all_tools
        registry = ToolAndSkillRegistry()
        register_all_tools(registry)

    return ConfigDrivenExecutor(registry, config, llm_adapter)


def create_executor_from_config_file(config_path: str,
                                      registry: Optional[ToolAndSkillRegistry] = None) -> ConfigDrivenExecutor:
    """从配置文件创建执行器"""
    config = SystemConfig.from_file(config_path)
    return create_executor(config, registry)