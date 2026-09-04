#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tool 接口定义 - 基础单元能力
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ToolDefinition:
    """工具定义"""
    name: str                          # 工具唯一标识
    description: str                   # 描述（供 LLM 理解能力）
    input_schema: Dict[str, Any]       # 输入参数 schema (JSON Schema)
    output_schema: Dict[str, Any]      # 输出 schema
    category: str                      # 分类: parser/resolver/provider/analysis/llm
    version: str = "1.0.0"             # 版本
    metadata: Dict[str, Any] = field(default_factory=dict)  # 额外元信息
    # Harness metadata. Defaults preserve the behavior of existing extensions.
    risk: str = "read_only"             # read_only/workspace_write/execute/network/destructive
    side_effect: bool = False
    idempotent: bool = True
    timeout_sec: Optional[float] = None
    # subprocess: tool uses subprocess.run(timeout=...) — gateway does not wrap in a
    # blocking thread pool. best_effort: caller timeout only. none: no gateway timeout.
    timeout_enforcement: str = "best_effort"
    requires_approval: bool = False
    cost_class: str = "low"
    allowed_roots: List[str] = field(default_factory=list)


class BaseTool(ABC):
    """工具基类 - 基础单元能力"""

    @property
    @abstractmethod
    def definition(self) -> ToolDefinition:
        """工具定义"""
        pass

    @abstractmethod
    def execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行工具

        Args:
            input_data: 输入数据

        Returns:
            输出数据
        """
        pass

    def validate_input(self, input_data: Dict[str, Any]) -> tuple[bool, Optional[str]]:
        """
        验证输入数据

        Args:
            input_data: 输入数据

        Returns:
            (是否有效, 错误消息)
        """
        # 默认不做验证
        return True, None

    def execute_with_validation(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """带验证的执行"""
        valid, error_msg = self.validate_input(input_data)
        if not valid:
            raise ValueError(f"输入验证失败: {error_msg}")
        return self.execute(input_data)

    def runtime_policy(self) -> Dict[str, Any]:
        """Return execution policy without requiring every plugin to be updated."""
        definition = self.definition
        return {
            "risk": getattr(definition, "risk", "read_only"),
            "side_effect": bool(getattr(definition, "side_effect", False)),
            "idempotent": bool(getattr(definition, "idempotent", True)),
            "timeout_sec": getattr(definition, "timeout_sec", None),
            "timeout_enforcement": getattr(definition, "timeout_enforcement", "best_effort"),
            "requires_approval": bool(getattr(definition, "requires_approval", False)),
            "cost_class": getattr(definition, "cost_class", "low"),
            "allowed_roots": list(getattr(definition, "allowed_roots", []) or []),
        }

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.definition.name}>"
