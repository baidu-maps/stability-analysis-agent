#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""崩溃日志 OS / 格式检测。"""

from __future__ import annotations

import re

from tools._stack_symbol_utils import looks_like_cpp_qualified_stack
from tools.crash_parser.stack_lines import _IOS_PREPARSED_STACK_RE

_IOS_OS_VERSION_RE = re.compile(r"^\s*OS Version:\s*iOS\b", re.MULTILINE | re.IGNORECASE)
_IOS_HW_MODEL_RE = re.compile(
    r"^\s*Hardware Model:\s*(iPhone|iPad|iPod)\b", re.MULTILINE | re.IGNORECASE
)


def _detect_apple_ios_truncated_crash(content: str) -> bool:
    """
    截断/二次导出的 Apple 崩溃，或 ANR/主线程死锁类报告（可无 Exception/Crashed 行）。
    """
    cl = content.lower()
    if re.search(r"^\s*OS Version:\s*macOS\b", content, re.MULTILINE | re.IGNORECASE) or re.search(
        r"^\s*OS Version:\s*Mac OS X\b", content, re.MULTILINE | re.IGNORECASE
    ):
        return False
    if re.search(r"Hardware Model:\s*Mac", content, re.IGNORECASE):
        return False
    if "binary images:" not in cl:
        return False
    if not (
        re.search(r"UIKitCore|UIKit\.framework", content)
        and re.search(r"libsystem_pthread\.dylib|libobjc\.A\.dylib", content, re.IGNORECASE)
    ):
        return False
    if (
        re.search(r"^\s*Exception Type:\s*", content, re.MULTILINE)
        and re.search(r"^\s*Crashed Thread:\s*\d+", content, re.MULTILINE)
    ):
        return True
    if re.search(r"(?m)^Thread\s+\d+\s+Deadlock:\s*$", content) or "main thread deadlocked" in cl:
        return True
    return False


def _detect_apple_ios_freeze_report(content: str) -> bool:
    """
    iOS 主线程卡顿 / Watchdog 类采样（Freeze Type、卡顿堆栈 等工具导出，常无 OS Version / Binary Images）。
    """
    if re.search(r"Hardware Model:\s*Mac", content, re.IGNORECASE):
        return False
    if re.search(r"^\s*Freeze\s+Type:\s*", content, re.MULTILINE | re.IGNORECASE):
        if re.search(r"UIKitCore|libsystem_kernel\.dylib|GraphicsServices", content, re.IGNORECASE):
            return True
    if "卡顿堆栈" in content and re.search(
        r"UIKitCore|libobjc\.A\.dylib|libsystem_kernel\.dylib", content, re.IGNORECASE
    ):
        return True
    return False


def _detect_ios_mach_tool_export(content: str) -> bool:
    """
    第三方 / KZp 等导出：Crash Type: Mach、Last Exception Backtrace 等，常无 OS Version / Binary Images。
    """
    if re.search(r"Hardware Model:\s*Mac", content, re.IGNORECASE):
        return False
    if not re.search(r"Last Exception Backtrace:", content, re.I):
        return False
    if not (
        re.search(r"Crash Type:\s*Mach", content, re.I)
        or re.search(r"Crash Reason:\s*EXC_", content, re.I)
    ):
        return False
    if not re.search(r"\.dylib\b|UIKitCore|CoreFoundation|Metal", content, re.I):
        return False
    return True


def _detect_ios_pre_parsed_symbolized_crash(content: str) -> bool:
    """
    已符号化的精简 iOS 崩溃导出（如去哪儿 Crash.txt）：
    - 首行 ``* SIGSEGV: 0x... UUID + offset``
    - 栈行 ``帧号 帧号 模块 0x地址 符号``（双序号前缀）
    """
    if re.search(r"Hardware Model:\s*Mac", content, re.IGNORECASE):
        return False
    if not re.search(r"^\*\s*SIG[A-Z0-9_]+:\s*0x[0-9a-fA-F]+", content, re.MULTILINE):
        return False
    stack_hits = sum(
        1 for line in content.splitlines() if _IOS_PREPARSED_STACK_RE.match(line)
    )
    return stack_hits >= 2

def detect_os_type(content: str) -> str:
    """检测操作系统类型"""
    content_lower = content.lower()

    # 项目内自定义：中文「平台: mac」
    if "平台: mac" in content:
        return "macos"

    # Apple iOS：显式 OS Version / Hardware Model（须早于 Harmony：栈符号如 connectToHost 含子串「ohos」会误判）
    if _IOS_OS_VERSION_RE.search(content) or _IOS_HW_MODEL_RE.search(content):
        return "ios"

    # HarmonyOS / OpenHarmony（勿用裸子串 ohos：会命中 connectToHost 等；单独 ohos 用词边界）
    if (
        "harmonyos" in content_lower
        or "openharmony" in content_lower
        or "build info:mro" in content_lower
        or "com.ohos." in content_lower
        or re.search(r"\bohos\b", content_lower)
    ):
        return "harmonyos"

    # Android 相关特征（避免使用过于宽泛的 'art' 单独匹配）
    if any(keyword in content_lower for keyword in ["build fingerprint", "dalvik", "android"]):
        return "android"
    # logcat / debuggerd 短片段常无「android」字样（Fatal signal + Cmdline/pid/tid 或 APK 内嵌 so）
    if re.search(r"\bfatal signal\b", content_lower) and (
        re.search(r"^\s*Cmdline:\s*com\.", content, re.MULTILINE | re.IGNORECASE)
        or re.search(r"\bpid:\s*\d+,\s*tid:\s*\d+,\s*name:", content, re.IGNORECASE)
        or "base.apk!" in content_lower
    ):
        return "android"
    if re.search(r"^\s*Crashed Thread:\s*\d+\s*$", content, re.MULTILINE) and re.search(
        r"^\s*Exception Type:\s*", content, re.MULTILINE
    ):
        if "report version:" in content_lower and (
            "identifier:" in content_lower or "process:" in content_lower
        ):
            if re.search(r"^\s*OS Version:\s*macOS\b", content, re.MULTILINE | re.IGNORECASE):
                return "macos"
            if re.search(r"^\s*OS Version:\s*Mac OS X\b", content, re.MULTILINE | re.IGNORECASE):
                return "macos"
            # 无 macOS 字样、带 Apple 报告头时视为 iOS（避免 libdyld 误判为 macOS）
            return "ios"

    if _detect_apple_ios_truncated_crash(content):
        return "ios"

    if _detect_apple_ios_freeze_report(content):
        return "ios"

    if _detect_ios_mach_tool_export(content):
        return "ios"

    if _detect_ios_pre_parsed_symbolized_crash(content):
        return "ios"

    if looks_like_cpp_qualified_stack(content):
        return "ios"

    # 桌面 macOS（显式 OS 行或编译器特征；不再用裸子串 dyld — iOS 同样有 libdyld.dylib）
    if re.search(r"^\s*OS Version:\s*Mac OS X\b", content, re.MULTILINE) or re.search(
        r"^\s*OS Version:\s*macOS\b", content, re.MULTILINE | re.IGNORECASE
    ):
        return "macos"
    if "apple llvm" in content_lower:
        return "macos"

    if any(keyword in content_lower for keyword in ["gdb", "core dump", "segmentation fault"]) and "#" in content:
        return "linux"
    if (
        any(keyword in content_lower for keyword in ["exception code:", "faulting module:", "windows"])
        and "call stack:" in content_lower
    ):
        return "windows"
    return "unknown"
