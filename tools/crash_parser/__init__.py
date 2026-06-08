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
- harmony_crash_diagnosis: Harmony crashDiagnosis JSON 单行导出
"""

from tools.crash_parser.core import parse_crash_core
from tools.crash_parser.harmony_crash_diagnosis import (
    is_harmony_crash_diagnosis_json,
    parse_harmony_crash_diagnosis,
)
from tools.crash_parser.platform_json_exports import (
    is_platform_json_export,
    parse_platform_json_export,
)
from tools.crash_parser.format_detect import detect_os_type
from tools.crash_parser.parsers import (
    BaseCrashParser,
    DefaultCrashParser,
    AndroidHarmonyTidCrashParser,
    AndroidLogcatCrashParser,
    HarmonyCrashDiagnosisJsonParser,
    HarmonyStacktraceCrashParser,
    IosAppleCrashParser,
    IosFreezeReportParser,
    IosMachExportCrashParser,
    IosPreParsedCrashParser,
    PARSERS,
    PlatformJsonExportParser,
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
    "HarmonyCrashDiagnosisJsonParser",
    "IosAppleCrashParser",
    "IosFreezeReportParser",
    "IosMachExportCrashParser",
    "IosPreParsedCrashParser",
    "MetaInfo",
    "PARSERS",
    "PlatformJsonExportParser",
    "StackFrame",
    "ThreadStack",
    "crash_parse_options_from_cli_args",
    "detect_os_type",
    "is_harmony_crash_diagnosis_json",
    "is_platform_json_export",
    "parse_crash_core",
    "parse_harmony_crash_diagnosis",
    "parse_platform_json_export",
    "select_crash_parser",
]
