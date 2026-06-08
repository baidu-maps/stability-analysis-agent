#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按日志格式注册的解析器（渐进式拆分入口）。"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import List, Optional

from tools.crash_parser.core import parse_crash_core
from tools.crash_parser.harmony_crash_diagnosis import (
    is_harmony_crash_diagnosis_json,
    parse_harmony_crash_diagnosis,
)
from tools.crash_parser.platform_json_exports import (
    is_platform_json_export,
    parse_platform_json_export,
)
from tools.crash_parser.format_detect import (
    _detect_apple_ios_freeze_report,
    _detect_apple_ios_truncated_crash,
    _detect_ios_mach_tool_export,
    _detect_ios_pre_parsed_symbolized_crash,
)
from tools.crash_parser.types import CrashAnalysisResult, CrashParseOptions


class BaseCrashParser:
    """解析器基类：每种日志格式一个实现，按优先级注册。"""

    format_id: str = "default"

    def can_handle(self, content: str, os_type: str) -> bool:
        return False

    def parse(
        self,
        content: str,
        os_type: str,
        debug: bool = False,
        options: Optional[CrashParseOptions] = None,
    ) -> CrashAnalysisResult:
        raise NotImplementedError

    def _annotate(self, result: CrashAnalysisResult) -> CrashAnalysisResult:
        if self.format_id == "default":
            return result
        return replace(
            result,
            meta_info=replace(result.meta_info, log_format=self.format_id),
        )


class IosPreParsedCrashParser(BaseCrashParser):
    """已符号化精简 iOS 导出（如去哪儿 * SIGSEGV + 双序号栈）。"""

    format_id = "ios_pre_parsed_symbolized"

    def can_handle(self, content: str, os_type: str) -> bool:
        return os_type == "ios" and _detect_ios_pre_parsed_symbolized_crash(content)

    def parse(
        self,
        content: str,
        os_type: str,
        debug: bool = False,
        options: Optional[CrashParseOptions] = None,
    ) -> CrashAnalysisResult:
        return self._annotate(parse_crash_core(content, os_type=os_type, debug=debug, options=options))


class IosMachExportCrashParser(BaseCrashParser):
    """KZp / 第三方 Mach 导出（Last Exception Backtrace）。"""

    format_id = "ios_mach_tool_export"

    def can_handle(self, content: str, os_type: str) -> bool:
        return os_type == "ios" and _detect_ios_mach_tool_export(content)

    def parse(
        self,
        content: str,
        os_type: str,
        debug: bool = False,
        options: Optional[CrashParseOptions] = None,
    ) -> CrashAnalysisResult:
        return self._annotate(parse_crash_core(content, os_type=os_type, debug=debug, options=options))


class IosAppleCrashParser(BaseCrashParser):
    """标准 Apple .crash（Exception Type + Crashed Thread / Thread N）。"""

    format_id = "ios_apple_crash"

    def can_handle(self, content: str, os_type: str) -> bool:
        if os_type not in ("ios", "macos"):
            return False
        if re.search(r"^\s*Exception Type:\s*", content, re.MULTILINE) and re.search(
            r"^\s*Crashed Thread:\s*\d+\s*$", content, re.MULTILINE
        ):
            return True
        return _detect_apple_ios_truncated_crash(content)

    def parse(
        self,
        content: str,
        os_type: str,
        debug: bool = False,
        options: Optional[CrashParseOptions] = None,
    ) -> CrashAnalysisResult:
        return self._annotate(parse_crash_core(content, os_type=os_type, debug=debug, options=options))


class IosFreezeReportParser(BaseCrashParser):
    """iOS 主线程卡顿 / Watchdog 采样。"""

    format_id = "ios_freeze_report"

    def can_handle(self, content: str, os_type: str) -> bool:
        return os_type == "ios" and _detect_apple_ios_freeze_report(content)

    def parse(
        self,
        content: str,
        os_type: str,
        debug: bool = False,
        options: Optional[CrashParseOptions] = None,
    ) -> CrashAnalysisResult:
        return self._annotate(parse_crash_core(content, os_type=os_type, debug=debug, options=options))


class AndroidHarmonyTidCrashParser(BaseCrashParser):
    """HarmonyOS / Android 多 Tid 块 dump（Fault thread info / Tid:）。"""

    format_id = "android_harmony_tid"

    def can_handle(self, content: str, os_type: str) -> bool:
        return os_type in ("android", "harmonyos") and "Tid:" in content

    def parse(
        self,
        content: str,
        os_type: str,
        debug: bool = False,
        options: Optional[CrashParseOptions] = None,
    ) -> CrashAnalysisResult:
        return self._annotate(parse_crash_core(content, os_type=os_type, debug=debug, options=options))


class HarmonyCrashDiagnosisJsonParser(BaseCrashParser):
    """Harmony ``crashDiagnosis: { ... }`` 单行 JSON（body.attributed_stack.stack_frames）。"""

    format_id = "harmony_crash_diagnosis_json"

    def can_handle(self, content: str, os_type: str) -> bool:
        return is_harmony_crash_diagnosis_json(content)

    def parse(
        self,
        content: str,
        os_type: str,
        debug: bool = False,
        options: Optional[CrashParseOptions] = None,
    ) -> CrashAnalysisResult:
        return self._annotate(
            parse_harmony_crash_diagnosis(content, debug, options=options)
        )


class PlatformJsonExportParser(BaseCrashParser):
    """Sentry / Crashlytics / Bugsnag / 通用 JSON 栈导出。"""

    format_id = "platform_json_export"

    def can_handle(self, content: str, os_type: str) -> bool:
        return is_platform_json_export(content)

    def parse(
        self,
        content: str,
        os_type: str,
        debug: bool = False,
        options: Optional[CrashParseOptions] = None,
    ) -> CrashAnalysisResult:
        # parse_platform_json_export 会写入更精确的 adapter log_format。
        return parse_platform_json_export(content, debug, options=options)


class HarmonyStacktraceCrashParser(BaseCrashParser):
    """OpenHarmony 单 Stacktrace: 块（无 Tid:）。"""

    format_id = "harmony_stacktrace"

    def can_handle(self, content: str, os_type: str) -> bool:
        return (
            os_type == "harmonyos"
            and "Stacktrace:" in content
            and "Tid:" not in content
        )

    def parse(
        self,
        content: str,
        os_type: str,
        debug: bool = False,
        options: Optional[CrashParseOptions] = None,
    ) -> CrashAnalysisResult:
        return self._annotate(parse_crash_core(content, os_type=os_type, debug=debug, options=options))


class AndroidLogcatCrashParser(BaseCrashParser):
    """Android logcat / debuggerd Fatal signal 片段（无 Tid:）。"""

    format_id = "android_logcat"

    def can_handle(self, content: str, os_type: str) -> bool:
        if os_type != "android" or "Tid:" in content:
            return False
        cl = content.lower()
        if "fatal signal" in cl:
            return True
        if re.search(r"^\s*Cmdline:\s*com\.", content, re.MULTILINE | re.IGNORECASE):
            return True
        if "*** *** *** *** *** *** *** *** *** *** *** *** *** *** *** ***" in content:
            return True
        return False

    def parse(
        self,
        content: str,
        os_type: str,
        debug: bool = False,
        options: Optional[CrashParseOptions] = None,
    ) -> CrashAnalysisResult:
        return self._annotate(parse_crash_core(content, os_type=os_type, debug=debug, options=options))


class DefaultCrashParser(BaseCrashParser):
    """兜底解析器：Harmony/Android/Linux 及未识别格式。"""

    format_id = "default"

    def can_handle(self, content: str, os_type: str) -> bool:
        return True

    def parse(
        self,
        content: str,
        os_type: str,
        debug: bool = False,
        options: Optional[CrashParseOptions] = None,
    ) -> CrashAnalysisResult:
        return parse_crash_core(content, os_type=os_type, debug=debug, options=options)


PARSERS: List[BaseCrashParser] = [
    IosPreParsedCrashParser(),
    IosMachExportCrashParser(),
    IosAppleCrashParser(),
    IosFreezeReportParser(),
    HarmonyCrashDiagnosisJsonParser(),
    PlatformJsonExportParser(),
    AndroidHarmonyTidCrashParser(),
    HarmonyStacktraceCrashParser(),
    AndroidLogcatCrashParser(),
    DefaultCrashParser(),
]


def select_crash_parser(content: str, os_type: str) -> BaseCrashParser:
    for parser in PARSERS:
        if parser.can_handle(content, os_type=os_type):
            return parser
    return PARSERS[-1]
