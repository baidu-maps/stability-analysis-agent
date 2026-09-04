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

from .runtime import RunTrace, RuntimeBudget, value_hash
from .tool_gateway import ToolExecutionGateway
from services.action_security import ActionSecurityAnalyzer
from services.evidence_store import EvidenceContextManager, EvidenceStore
from services.observations import ObservationStore

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
                 config: Dict[str, Any],
                 trace: Optional[RunTrace] = None,
                 policy: Any = None):
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
        self.policy = policy
        budget_config = config.get("runtime_budget", {}) if isinstance(config, dict) else {}
        self.trace = trace or RunTrace(
            budget=RuntimeBudget(
                max_llm_calls=int(budget_config.get("max_llm_calls", 0) or 0),
                max_tool_calls=int(budget_config.get("max_tool_calls", 0) or 0),
                max_total_seconds=float(budget_config.get("max_total_seconds", 0) or 0),
                max_total_tokens=int(budget_config.get("max_total_tokens", 0) or 0),
                max_estimated_cost=float(budget_config.get("max_estimated_cost", 0) or 0),
            )
        )
        self.execution_events: List[Dict[str, Any]] = self.trace.events
        self.evidence = EvidenceStore()
        self.observations = ObservationStore()
        evidence_budget = config.get("evidence_max_chars", 24000) if isinstance(config, dict) else 24000
        self.context_manager = EvidenceContextManager(self.evidence, max_chars=int(evidence_budget or 24000))
        self.gateway = ToolExecutionGateway(policy, self.trace, ActionSecurityAnalyzer())

    def evidence_package(self, *, max_chars: Optional[int] = None,
                         max_tokens: Optional[int] = None,
                         min_round: Optional[int] = None) -> Dict[str, Any]:
        """Return the bounded evidence package exposed to reports/adapters."""
        return self.context_manager.package(
            max_chars=max_chars, max_tokens=max_tokens, min_round=min_round,
        )

    def observation_snapshot(self) -> Dict[str, Any]:
        """Return structured observations suitable for reports or a next turn."""
        return self.observations.snapshot()

    def select_prompt(self, prompt: str) -> str:
        """Return the only prompt text allowed to reach an LLM adapter."""
        token_budget = 0
        if isinstance(self.config, dict):
            token_budget = int(self.config.get("evidence_max_tokens", 0) or 0)
        return str(self.context_manager.select_prompt(prompt, max_tokens=token_budget)["content"])

    def assemble_context_loop_prompt(
        self,
        base_prompt: str,
        *,
        evidence_package: Optional[Dict[str, Any]] = None,
        is_final_round: bool = False,
        early_final_reason: Optional[str] = None,
    ) -> str:
        """Single entry for context-loop prompt assembly; budget via select_prompt()."""
        return str(
            self.context_manager.assemble_context_loop_prompt(
                base_prompt,
                evidence_package=evidence_package,
                is_final_round=is_final_round,
                early_final_reason=early_final_reason,
            )["content"]
        )

    def _record_execution_event(
        self,
        *,
        kind: str,
        name: str,
        started_at: str,
        started_perf: float,
        status: str,
        error: Optional[str] = None,
        **data: Any,
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
        event.update(data)
        self.trace.emit(
            f"{kind}.{status}", kind=kind, name=name, status=status,
            started_at=started_at, finished_at=event["finished_at"],
            duration_ms=event["duration_ms"], error=error,
            **{key: value for key, value in data.items() if key not in {"error"}},
            provider=getattr(self.llm, "provider", None) if kind == "llm" else None,
            model=getattr(self.llm, "model", None) if kind == "llm" else None,
        )

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
            self.trace.budget.consume("llm")
            response = self.llm.chat(messages, **kwargs)
        except Exception as exc:
            self._record_execution_event(
                kind="llm",
                name="llm_analysis",
                started_at=started_at,
                started_perf=started_perf,
                status="failed",
                error=str(exc),
                input_hash=value_hash(messages),
            )
            raise
        self._record_execution_event(
            kind="llm",
            name="llm_analysis",
            started_at=started_at,
            started_perf=started_perf,
            status="success",
            input_hash=value_hash(messages),
            output_hash=value_hash(getattr(response, "content", "")),
            token_usage=getattr(response, "usage", None),
            estimated_cost=(getattr(response, "metadata", None) or {}).get("estimated_cost"),
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
        started_at = datetime.datetime.now().astimezone().isoformat()
        started_perf = time.perf_counter()
        self.trace.budget.consume("llm")
        stream = self.llm.stream(messages, **kwargs)
        self.trace.emit("llm.stream_started", kind="llm", name="llm_analysis",
                        status="running", input_hash=value_hash(messages))

        def _events():
            chunks: List[str] = []
            try:
                for chunk in stream:
                    chunks.append(str(chunk))
                    yield chunk
            except Exception as exc:
                self.trace.emit("llm.stream_failed", kind="llm", name="llm_analysis",
                                status="failed", error=str(exc), input_hash=value_hash(messages),
                                duration_ms=max(0, int((time.perf_counter() - started_perf) * 1000)),
                                started_at=started_at)
                raise
            else:
                self.trace.emit("llm.stream_finished", kind="llm", name="llm_analysis",
                                status="success", input_hash=value_hash(messages),
                                output_hash=value_hash("".join(chunks)),
                                duration_ms=max(0, int((time.perf_counter() - started_perf) * 1000)),
                                started_at=started_at)

        return _events()

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

        try:
            payload = dict(input_data or {})
            payload.setdefault("_observation_store", self.observations)
            return self.gateway.execute(tool_name, tool, payload)
        except ValueError as exc:
            if "input validation failed" in str(exc):
                raise ValueError(f"工具 '{tool_name}' 输入验证失败: {str(exc).split(': ', 1)[-1]}") from exc
            raise

    def get_tool_definition(self, tool_name: str) -> Optional[Any]:
        """获取工具定义"""
        tool = self.tools.get_tool(tool_name)
        if tool and hasattr(tool, 'definition'):
            return tool.definition
        return None
