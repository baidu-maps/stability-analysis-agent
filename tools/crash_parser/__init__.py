#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
崩溃日志解析子包（由 crash_log_parser_tool 渐进式拆分而来）。

模块职责：
- types: 数据结构
- format_detect: OS / 格式检测
- stack_lines: 各平台栈行解析
- ios_scoping / android: 平台专用辅助
- stack_extract: 从文本提取 StackFrame
- meta: crash_info / meta_info 提取
- core: 组装 CrashAnalysisResult
- parsers: 按格式注册的解析器
"""

from tools.crash_parser.core import parse_crash_core
from tools.crash_parser.format_detect import detect_os_type
from tools.crash_parser.parsers import (
    BaseCrashParser,
    DefaultCrashParser,
    AndroidHarmonyTidCrashParser,
    AndroidLogcatCrashParser,
    HarmonyStacktraceCrashParser,
    IosAppleCrashParser,
    IosFreezeReportParser,
    IosMachExportCrashParser,
    IosPreParsedCrashParser,
    PARSERS,
    select_crash_parser,
)
from tools.crash_parser.types import (
    CrashAnalysisResult,
    CrashInfo,
    CrashParseOptions,
    MetaInfo,
    StackFrame,
    ThreadStack,
    crash_parse_options_from_cli_args,
)

__all__ = [
    "BaseCrashParser",
    "CrashAnalysisResult",
    "CrashInfo",
    "CrashParseOptions",
    "DefaultCrashParser",
    "IosAppleCrashParser",
    "IosFreezeReportParser",
    "IosMachExportCrashParser",
    "IosPreParsedCrashParser",
    "MetaInfo",
    "PARSERS",
    "StackFrame",
    "ThreadStack",
    "crash_parse_options_from_cli_args",
    "detect_os_type",
    "parse_crash_core",
    "select_crash_parser",
]
