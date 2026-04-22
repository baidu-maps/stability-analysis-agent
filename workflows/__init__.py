#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
workflows/ — 内置 Workflow 实现

公开 API：
  - BaseCrashAnalysisWorkflow, iOSCrashAnalyzeWorkflow, AndroidCrashAnalyzeWorkflow, GenericCrashAnalyzeWorkflow
  - register_all_workflows()
"""

from .crash_analysis_workflow import (
    BaseCrashAnalysisWorkflow,
    iOSCrashAnalyzeWorkflow,
    AndroidCrashAnalyzeWorkflow,
    GenericCrashAnalyzeWorkflow,
)

__all__ = [
    "BaseCrashAnalysisWorkflow",
    "iOSCrashAnalyzeWorkflow",
    "AndroidCrashAnalyzeWorkflow",
    "GenericCrashAnalyzeWorkflow",
    "register_all_workflows",
]


def register_all_workflows(registry=None):
    """注册所有内置工作流到注册表（延迟导入避免循环依赖）。"""
    from tool_system import get_registry, Priority

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
