#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Workflow 接口定义 - 问题类型解决方案
"""

from __future__ import annotations

import datetime
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .tool import ToolDefinition
    from .llm_adapter import BaseLLMAdapter, LLMResponse
    from .registry import ToolAndWorkflowRegistry


@dataclass
class WorkflowDefinition:
    """工作流定义"""
    name: str                          # 工作流唯一标识
    description: str                   # 描述（供用户理解用途）
    problem_type: str                  # 问题类型: ios_crash/android_anr/memory_leak/crash_analysis
    required_tools: List[str]          # 需要的工具列表
    version: str = "1.0.0"             # 版本
    metadata: Dict[str, Any] = field(default_factory=dict)  # 额外元信息


class BaseWorkflow(ABC):
    """工作流基类 - 问题类型解决方案"""

    @property
    @abstractmethod
    def definition(self) -> WorkflowDefinition:
        """工作流定义"""
        pass

    @abstractmethod
    def solve(self, problem: Dict[str, Any], context: "WorkflowContext") -> Dict[str, Any]:
        """
        解决问题

        Args:
            problem: 问题描述（crash log, error info 等）
            context: 工作流执行上下文

        Returns:
            解决方案结果
        """
        pass

    def validate_problem(self, problem: Dict[str, Any]) -> tuple[bool, Optional[str]]:
        """
        验证问题输入

        Args:
            problem: 问题数据

        Returns:
            (是否有效, 错误消息)
        """
        # 默认验证 problem_type 是否匹配
        if "problem_type" in self.definition.metadata:
            expected = self.definition.metadata["problem_type"]
            actual = problem.get("problem_type")
            if actual and actual != expected:
                return False, f"problem_type 不匹配: 期望 {expected}, 实际 {actual}"
        return True, None

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.definition.name}>"


class WorkflowContext:
    """
    工作流执行上下文
    提供了 Workflow 执行过程中需要的能力：LLM 调用、工具执行、配置等
    """

    def __init__(self,
                 llm_adapter: Any,
                 tool_registry: Any,
                 config: Dict[str, Any]):
        """
        初始化上下文

        Args:
            llm_adapter: LLM 适配器
            tool_registry: 工具注册表
            config: 配置信息
        """
        self.llm = llm_adapter
        self.tools = tool_registry
        self.config = config
        self.execution_events: List[Dict[str, Any]] = []

    def _record_execution_event(
        self,
        *,
        kind: str,
        name: str,
        started_at: str,
        started_perf: float,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        event: Dict[str, Any] = {
            "kind": kind,
            "name": name,
            "status": status,
            "started_at": started_at,
            "finished_at": datetime.datetime.now().astimezone().isoformat(),
            "duration_ms": max(0, int(round((time.perf_counter() - started_perf) * 1000))),
        }
        if error:
            event["error"] = error
        self.execution_events.append(event)

    def call_llm(self,
                 prompt: str,
                 messages: Optional[List[Dict[str, str]]] = None,
                 **kwargs) -> Any:
        """
        统一 LLM 调用

        Args:
            prompt: 提示词（会转换为 messages）
            messages: 可选的完整消息列表
            **kwargs: 其他参数

        Returns:
            LLM 响应
        """
        if messages is None:
            messages = [{"role": "user", "content": prompt}]
        started_at = datetime.datetime.now().astimezone().isoformat()
        started_perf = time.perf_counter()
        try:
            response = self.llm.chat(messages, **kwargs)
        except Exception as exc:
            self._record_execution_event(
                kind="llm",
                name="llm_analysis",
                started_at=started_at,
                started_perf=started_perf,
                status="failed",
                error=str(exc),
            )
            raise
        self._record_execution_event(
            kind="llm",
            name="llm_analysis",
            started_at=started_at,
            started_perf=started_perf,
            status="success",
        )
        return response

    def call_llm_stream(self,
                        prompt: str,
                        messages: Optional[List[Dict[str, str]]] = None,
                        **kwargs):
        """
        流式 LLM 调用

        Args:
            prompt: 提示词
            messages: 可选的完整消息列表
            **kwargs: 其他参数
        """
        if messages is None:
            messages = [{"role": "user", "content": prompt}]
        return self.llm.stream(messages, **kwargs)

    def execute_tool(self, tool_name: str, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行工具

        Args:
            tool_name: 工具名称
            input_data: 输入数据

        Returns:
            工具执行结果
        """
        tool = self.tools.get_tool(tool_name)
        if tool is None:
            raise ValueError(f"工具 '{tool_name}' 不存在")

        # 实例化（如需）
        if isinstance(tool, type):
            tool = tool()

        started_at = datetime.datetime.now().astimezone().isoformat()
        started_perf = time.perf_counter()
        try:
            result = tool.execute(input_data)
        except Exception as exc:
            self._record_execution_event(
                kind="tool",
                name=tool_name,
                started_at=started_at,
                started_perf=started_perf,
                status="failed",
                error=str(exc),
            )
            raise
        self._record_execution_event(
            kind="tool",
            name=tool_name,
            started_at=started_at,
            started_perf=started_perf,
            status="success",
        )
        return result

    def get_tool_definition(self, tool_name: str) -> Optional[Any]:
        """获取工具定义"""
        tool = self.tools.get_tool(tool_name)
        if tool and hasattr(tool, 'definition'):
            return tool.definition
        return None
