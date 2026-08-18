#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置驱动的执行器
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Generator

from .config import SystemConfig, ToolConfig, WorkflowConfig
from .registry import ToolAndWorkflowRegistry, Priority
from .workflow import WorkflowContext
from .llm.llm_adapter import BaseLLMAdapter, LLMAdapterFactory, LLMResponse

logger = logging.getLogger(__name__)


class ConfigDrivenExecutor:
    """配置驱动的执行器"""

    def __init__(self,
                 registry: ToolAndWorkflowRegistry,
                 config: SystemConfig,
                 llm_adapter: Optional[BaseLLMAdapter] = None):
        """
        初始化执行器

        Args:
            registry: 工具和工作流注册表
            config: 系统配置
            llm_adapter: LLM 适配器（可选，不提供则根据配置创建）
        """
        self.registry = registry
        self.config = config
        self.last_execution_events: List[Dict[str, Any]] = []

        # 初始化 LLM 适配器
        if llm_adapter is None:
            if config.llm:
                self.llm_adapter = LLMAdapterFactory.create(config.llm.to_dict())
            else:
                logger.warning("No LLM config, creating dummy adapter")
                self.llm_adapter = None
        else:
            self.llm_adapter = llm_adapter

        # 创建 WorkflowContext
        self._workflow_context = WorkflowContext(
            llm_adapter=self.llm_adapter,
            tool_registry=registry,
            config=config.metadata
        )

        # 实例化缓存
        self._tool_instances: Dict[str, Any] = {}
        self._workflow_instances: Dict[str, Any] = {}

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

        # 实例化启用的工作流
        for workflow_cfg in self.config.get_enabled_workflows():
            try:
                workflow = self._get_implementation(workflow_cfg.name, is_tool=False)
                if workflow:
                    if isinstance(workflow, type):
                        workflow = workflow(**workflow_cfg.params)
                    self._workflow_instances[workflow_cfg.name] = workflow
                    logger.info(f"Initialized workflow: {workflow_cfg.name}")
            except Exception as e:
                logger.error(f"Failed to initialize workflow {workflow_cfg.name}: {e}")

    def _get_implementation(self, name: str, is_tool: bool) -> Any:
        """根据配置获取具体实现"""
        collection = self.config.tools if is_tool else self.config.workflows
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

        return self._execute_tool_with_validation(name, tool, input_data)

    @staticmethod
    def _validate_tool_input(name: str, tool: Any,
                             input_data: Dict[str, Any]) -> None:
        """Validate tool input before allowing a direct tool invocation."""
        validate_input = getattr(tool, "validate_input", None)
        if callable(validate_input):
            valid, error_msg = validate_input(input_data)
            if not valid:
                raise ValueError(f"Tool '{name}' input validation failed: {error_msg}")

    @classmethod
    def _execute_tool_with_validation(cls, name: str, tool: Any,
                                      input_data: Dict[str, Any]) -> Dict[str, Any]:
        cls._validate_tool_input(name, tool, input_data)
        return tool.execute(input_data)

    def execute_tool_stream(self, name: str, input_data: Dict[str, Any]) -> Generator[str, None, None]:
        """执行工具（流式版本，如果有）"""
        tool = self._tool_instances.get(name)
        if tool is None:
            raise ValueError(f"Tool '{name}' not initialized")

        # 如果工具支持流式执行
        if hasattr(tool, "execute_stream"):
            self._validate_tool_input(name, tool, input_data)
            for chunk in tool.execute_stream(input_data):
                yield chunk
        else:
            # 否则返回普通结果
            result = self._execute_tool_with_validation(name, tool, input_data)
            yield str(result)

    # ==================== Workflow 执行 ====================

    def execute_workflow(self, name: str, problem: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行工作流

        Args:
            name: 工作流名称
            problem: 问题数据

        Returns:
            工作流执行结果
        """
        workflow = self._workflow_instances.get(name)
        if workflow is None:
            # 尝试动态获取
            workflow = self._get_implementation(name, is_tool=False)
            if workflow is None:
                raise ValueError(f"Workflow '{name}' not found")
            if isinstance(workflow, type):
                workflow = workflow()
                self._workflow_instances[name] = workflow

        # 创建新的 WorkflowContext（包含当前 LLM 适配器）
        context = WorkflowContext(
            llm_adapter=self.llm_adapter,
            tool_registry=self.registry,
            config=self.config.metadata
        )

        valid, error_msg = workflow.validate_problem(problem)
        if not valid:
            raise ValueError(f"Workflow '{name}' input validation failed: {error_msg}")

        result = workflow.solve(problem, context)
        self.last_execution_events = list(context.execution_events)
        return result

    def execute_workflow_stream(self, name: str, problem: Dict[str, Any]) -> Generator[str, None, None]:
        """执行工作流（流式版本）"""
        workflow = self._workflow_instances.get(name)
        if workflow is None:
            raise ValueError(f"Workflow '{name}' not initialized")

        # 如果工作流支持流式执行
        if hasattr(workflow, "solve_stream"):
            context = WorkflowContext(
                llm_adapter=self.llm_adapter,
                tool_registry=self.registry,
                config=self.config.metadata
            )
            valid, error_msg = workflow.validate_problem(problem)
            if not valid:
                raise ValueError(f"Workflow '{name}' input validation failed: {error_msg}")
            for chunk in workflow.solve_stream(problem, context):
                yield chunk
        else:
            result = self.execute_workflow(name, problem)
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
        """列出当前活跃的工具和工作流"""
        return {
            "tools": list(self._tool_instances.keys()),
            "workflows": list(self._workflow_instances.keys())
        }

    def get_tool_definition(self, name: str) -> Optional[Any]:
        """获取工具定义"""
        tool = self._tool_instances.get(name) or self._get_implementation(name, is_tool=True)
        if tool and hasattr(tool, "definition"):
            return tool.definition
        return None

    def get_workflow_definition(self, name: str) -> Optional[Any]:
        """获取工作流定义"""
        workflow = self._workflow_instances.get(name) or self._get_implementation(name, is_tool=False)
        if workflow and hasattr(workflow, "definition"):
            return workflow.definition
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

        # 检查工作流
        for workflow_cfg in self.config.get_enabled_workflows():
            impl = self._get_implementation(workflow_cfg.name, is_tool=False)
            if impl is None:
                errors.append(f"Workflow '{workflow_cfg.name}' not found in registry")

        return errors


# ==================== 工厂函数 ====================

def create_executor(config: Optional[SystemConfig] = None,
                   registry: Optional[ToolAndWorkflowRegistry] = None,
                   llm_adapter: Optional[BaseLLMAdapter] = None) -> ConfigDrivenExecutor:
    """
    创建配置驱动的执行器

    Args:
        config: 系统配置（默认从文件加载）
        registry: 工具和工作流注册表（默认创建新的）
        llm_adapter: LLM 适配器（默认根据配置创建）

    Returns:
        配置驱动的执行器
    """
    if config is None:
        config = SystemConfig.from_dict({})
        logger.info("Using empty config")

    if registry is None:
        from .builtins import register_all_tools
        registry = ToolAndWorkflowRegistry()
        register_all_tools(registry)

    return ConfigDrivenExecutor(registry, config, llm_adapter)


def create_executor_from_config_file(config_path: str,
                                      registry: Optional[ToolAndWorkflowRegistry] = None) -> ConfigDrivenExecutor:
    """从配置文件创建执行器"""
    config = SystemConfig.from_file(config_path)
    return create_executor(config, registry)
