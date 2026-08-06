#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
workflows/ — 内置 Workflow 实现

公开 API：
  - BaseCrashAnalysisWorkflow, iOSCrashAnalyzeWorkflow, AndroidCrashAnalyzeWorkflow, GenericCrashAnalyzeWorkflow
  - register_all_workflows()
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = [
    "BaseCrashAnalysisWorkflow",
    "iOSCrashAnalyzeWorkflow",
    "AndroidCrashAnalyzeWorkflow",
    "GenericCrashAnalyzeWorkflow",
    "AnrFreezeAnalysisWorkflow",
    "register_all_workflows",
]

_LAZY_CLASS_NAMES = frozenset(__all__) - {"register_all_workflows"}


def register_all_workflows(registry=None):
    """注册所有内置工作流到注册表（延迟导入，避免启动时加载 RAG/ML 依赖）。"""
    from tool_system import get_registry, Priority

    from .crash_analysis_workflow import (
        AndroidCrashAnalyzeWorkflow,
        GenericCrashAnalyzeWorkflow,
        iOSCrashAnalyzeWorkflow,
    )
    from .anr_freeze_workflow import AnrFreezeAnalysisWorkflow

    if registry is None:
        registry = get_registry()

    registry.register(
        "ios_crash_analyze",
        iOSCrashAnalyzeWorkflow(),
        priority=Priority.BUILTIN,
        force_override=False,
        is_tool=False,
        module="workflows",
    )
    registry.register(
        "android_crash_analyze",
        AndroidCrashAnalyzeWorkflow(),
        priority=Priority.BUILTIN,
        force_override=False,
        is_tool=False,
        module="workflows",
    )
    registry.register(
        "crash_analysis",
        GenericCrashAnalyzeWorkflow(),
        priority=Priority.BUILTIN,
        force_override=False,
        is_tool=False,
        module="workflows",
    )
    registry.register(
        "anr_freeze_analysis",
        AnrFreezeAnalysisWorkflow(),
        priority=Priority.BUILTIN,
        force_override=False,
        is_tool=False,
        module="workflows",
    )


def __getattr__(name: str) -> Any:
    if name not in _LAZY_CLASS_NAMES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if name == "AnrFreezeAnalysisWorkflow":
        from .anr_freeze_workflow import AnrFreezeAnalysisWorkflow
        return AnrFreezeAnalysisWorkflow
    from . import crash_analysis_workflow as _mod

    return getattr(_mod, name)


if TYPE_CHECKING:
    from .anr_freeze_workflow import AnrFreezeAnalysisWorkflow
    from .crash_analysis_workflow import (
        AndroidCrashAnalyzeWorkflow,
        BaseCrashAnalysisWorkflow,
        GenericCrashAnalyzeWorkflow,
        iOSCrashAnalyzeWorkflow,
    )
