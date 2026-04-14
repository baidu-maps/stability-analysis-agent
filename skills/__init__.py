#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
skills/ — 内置 Skill 实现（替换原 tool_system/skill_builtins.py）

公开 API：
  - BaseCrashAnalysisSkill, iOSCrashAnalyzeSkill, AndroidCrashAnalyzeSkill, GenericCrashAnalyzeSkill
  - register_all_skills()
"""

from .crash_analysis_skill import (
    BaseCrashAnalysisSkill,
    iOSCrashAnalyzeSkill,
    AndroidCrashAnalyzeSkill,
    GenericCrashAnalyzeSkill,
)

__all__ = [
    "BaseCrashAnalysisSkill",
    "iOSCrashAnalyzeSkill",
    "AndroidCrashAnalyzeSkill",
    "GenericCrashAnalyzeSkill",
    "register_all_skills",
]


def register_all_skills(registry=None):
    """注册所有内置技能到注册表（延迟导入避免循环依赖）。"""
    from tool_system import get_registry, Priority

    if registry is None:
        registry = get_registry()

    registry.register(
        "ios_crash_analyze",
        iOSCrashAnalyzeSkill(),
        priority=Priority.BUILTIN,
        force_override=False,
        is_tool=False,
        module="skills",
    )
    registry.register(
        "android_crash_analyze",
        AndroidCrashAnalyzeSkill(),
        priority=Priority.BUILTIN,
        force_override=False,
        is_tool=False,
        module="skills",
    )
    registry.register(
        "crash_analysis",
        GenericCrashAnalyzeSkill(),
        priority=Priority.BUILTIN,
        force_override=False,
        is_tool=False,
        module="skills",
    )
