#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
工具和工作流注册中心 - 支持覆盖替换
"""

from __future__ import annotations

import logging
from enum import IntEnum
from typing import Any, Dict, List, Optional, Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class Priority(IntEnum):
    """优先级（数值越大优先级越高）"""
    BUILTIN = 100       # 内置实现
    EXTENSION = 200     # 扩展实现（默认更高）
    CUSTOM = 300        # 自定义实现（最高）


@dataclass
class Registration:
    """注册项"""
    name: str
    cls_or_instance: Any
    priority: Priority
    is_override: bool = False  # 是否覆盖了已有实现
    module: Optional[str] = None  # 来源模块


class ToolAndWorkflowRegistry:
    """工具和工作流注册中心 - 支持覆盖替换"""

    def __init__(self):
        self._tools: Dict[str, Registration] = {}
        self._workflows: Dict[str, Registration] = {}
        self._override_history: List[Dict] = []  # 记录覆盖历史

    # ==================== 通用方法 ====================

    def register(self,
                 name: str,
                 cls_or_instance: Any,
                 priority: Priority = Priority.EXTENSION,
                 force_override: bool = False,
                 is_tool: bool = True,
                 module: Optional[str] = None) -> bool:
        """
        注册工具/工作流，支持覆盖

        Args:
            name: 名称
            cls_or_instance: 类或实例
            priority: 优先级
            force_override: 是否强制覆盖
            is_tool: True 为工具，False 为工作流
            module: 来源模块

        Returns:
            是否注册成功
        """
        registry = self._tools if is_tool else self._workflows
        is_tool_str = "工具" if is_tool else "工作流"

        existing = registry.get(name)

        if existing and not force_override:
            if priority <= existing.priority:
                # 优先级不够，拒绝注册
                logger.warning(f"跳过注册 {is_tool_str} '{name}': 已有更高优先级实现 (已有: {existing.priority}, 新: {priority})")
                return False

        # 注册/覆盖
        registry[name] = Registration(
            name=name,
            cls_or_instance=cls_or_instance,
            priority=priority,
            is_override=bool(existing),
            module=module
        )

        if existing:
            self._override_history.append({
                "name": name,
                "type": "tool" if is_tool else "workflow",
                "old_priority": existing.priority,
                "new_priority": priority,
                "old_class": existing.cls_or_instance.__name__ if hasattr(existing.cls_or_instance, '__name__') else str(existing.cls_or_instance),
                "new_class": cls_or_instance.__name__ if hasattr(cls_or_instance, '__name__') else str(cls_or_instance)
            })
            logger.info(f"🔄 覆盖 {is_tool_str}: '{name}' ({existing.priority} -> {priority})")
        else:
            logger.info(f"✅ 注册 {is_tool_str}: '{name}' (优先级: {priority})")

        return True

    def get(self, name: str) -> Optional[Any]:
        """获取工具或工作流（自动判断类型）"""
        if name in self._tools:
            return self._tools[name].cls_or_instance
        if name in self._workflows:
            return self._workflows[name].cls_or_instance
        return None

    def get_tool(self, name: str) -> Optional[Any]:
        """获取工具"""
        reg = self._tools.get(name)
        return reg.cls_or_instance if reg else None

    def get_workflow(self, name: str) -> Optional[Any]:
        """获取工作流"""
        reg = self._workflows.get(name)
        return reg.cls_or_instance if reg else None

    def list_all_tools(self) -> List[str]:
        """列出所有工具名称"""
        return list(self._tools.keys())

    def list_all_workflows(self) -> List[str]:
        """列出所有工作流名称"""
        return list(self._workflows.keys())

    def list_overrides(self) -> List[Dict]:
        """列出所有被覆盖的实现"""
        return self._override_history.copy()

    def get_priority(self, name: str) -> Optional[Priority]:
        """获取某个实现的优先级"""
        reg = self._tools.get(name) or self._workflows.get(name)
        return reg.priority if reg else None

    def is_override(self, name: str) -> bool:
        """是否覆盖了原有实现"""
        reg = self._tools.get(name) or self._workflows.get(name)
        return reg.is_override if reg else False

    # ==================== 便捷装饰器 ====================

    def register_tool(self, priority: Priority = Priority.EXTENSION,
                      force_override: bool = False,
                      module: Optional[str] = None):
        """工具注册装饰器"""
        def decorator(cls):
            self.register(cls.__name__, cls, priority, force_override, is_tool=True, module=module)
            return cls
        return decorator

    def register_workflow(self, priority: Priority = Priority.EXTENSION,
                          force_override: bool = False,
                          module: Optional[str] = None):
        """工作流注册装饰器"""
        def decorator(cls):
            self.register(cls.__name__, cls, priority, force_override, is_tool=False, module=module)
            return cls
        return decorator

    def register_tool_instance(self, instance: Any,
                               priority: Priority = Priority.EXTENSION,
                               force_override: bool = False):
        """注册工具实例"""
        name = getattr(instance.definition, 'name', instance.__class__.__name__)
        self.register(name, instance, priority, force_override, is_tool=True)
        return instance

    def register_workflow_instance(self, instance: Any,
                                   priority: Priority = Priority.EXTENSION,
                                   force_override: bool = False):
        """注册工作流实例"""
        name = getattr(instance.definition, 'name', instance.__class__.__name__)
        self.register(name, instance, priority, force_override, is_tool=False)
        return instance

    # ==================== 查找方法 ====================

    def get_workflow_by_problem_type(self, problem_type: str) -> List[Any]:
        """根据问题类型查找工作流"""
        result = []
        for reg in self._workflows.values():
            workflow = reg.cls_or_instance
            if hasattr(workflow, 'definition'):
                if workflow.definition.problem_type == problem_type:
                    result.append(workflow)
        return result

    def get_tools_by_category(self, category: str) -> List[Any]:
        """根据分类查找工具"""
        result = []
        for reg in self._tools.values():
            tool = reg.cls_or_instance
            if hasattr(tool, 'definition'):
                if tool.definition.category == category:
                    result.append(tool)
        return result

    def clear(self):
        """清空注册表"""
        self._tools.clear()
        self._workflows.clear()
        self._override_history.clear()


# 全局注册表实例
_registry: Optional[ToolAndWorkflowRegistry] = None


def get_registry() -> ToolAndWorkflowRegistry:
    """获取全局注册表"""
    global _registry
    if _registry is None:
        _registry = ToolAndWorkflowRegistry()
    return _registry


def set_registry(registry: ToolAndWorkflowRegistry):
    """设置全局注册表"""
    global _registry
    _registry = registry


# 便捷装饰器（使用全局注册表）
def register_tool(priority: Priority = Priority.EXTENSION,
                  force_override: bool = False,
                  module: Optional[str] = None):
    """工具注册装饰器（全局）"""
    return get_registry().register_tool(priority, force_override, module)


def register_workflow(priority: Priority = Priority.EXTENSION,
                      force_override: bool = False,
                      module: Optional[str] = None):
    """工作流注册装饰器（全局）"""
    return get_registry().register_workflow(priority, force_override, module)
