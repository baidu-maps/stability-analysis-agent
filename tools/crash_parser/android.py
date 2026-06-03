#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Android / ART 栈辅助与启发式检测。"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from tools.crash_parser.format_detect import detect_os_type

# Android logcat / 部分 tombstone：以含 "backtrace:" 的行作为一次 native 栈块起点
_BACKTRACE_HEADER_RE = re.compile(r"\bbacktrace\s*:", re.I)


def _backtrace_segment_ranges(lines: List[str]) -> List[Tuple[int, int]]:
    """按 ``backtrace:`` 标题行分段，返回每段在 lines 中的 [start, end) 下标。"""
    starts: List[int] = []
    for i, line in enumerate(lines):
        if _BACKTRACE_HEADER_RE.search(line):
            starts.append(i)
    if not starts:
        return [(0, len(lines))]
    ranges: List[Tuple[int, int]] = []
    for j, s in enumerate(starts):
        e = starts[j + 1] if j + 1 < len(starts) else len(lines)
        ranges.append((s, e))
    return ranges


# Android ART / tombstone / ANR traces: "native: #00 pc ..." 或 "#00 pc ..."；模块可为路径或 "[anon:dalvik-DEX data]"
_ART_NATIVE_PC_LINE_RE = re.compile(
    r"^(?:native:\s*)?#(\d+)\s+pc\s+([0-9a-fA-Fx]+)\s+(.+)$"
)
# logcat / Android Studio：行首为时间戳与 tag，``#NN pc`` 在行末，须用 search 匹配
_ART_NATIVE_PC_IN_LINE_RE = re.compile(
    r"#(\d+)\s+pc\s+([0-9a-fA-Fx]+)\s+(.+)$"
)
# GWP-ASan 等：主 ``backtrace:`` 之后另有分配/释放辅助栈，勿与主崩溃栈合并
_ANDROID_SANITIZER_AUX_STACK_HEADER_RE = re.compile(
    r"\b(deallocated|allocated)\s+by\s+thread\b",
    re.I,
)
# Android Java: "  at com.foo.Bar.method(File.java:12)" / (Native method)
_ANDROID_JAVA_AT_RE = re.compile(r"^\s*at\s+([^(]+)\(([^)]+)\)\s*$")
# ANR / 线程 dump 首行: "main" prio=5 tid=1 Native
_ANDROID_THREAD_BANNER_RE = re.compile(r'^\s*"([^"]+)"\s+prio=\d+\s+tid=(\d+)\s+', re.I)


def _parse_android_thread_banner(line: str) -> Optional[Tuple[str, str]]:
    """从 ``\"main\" prio=5 tid=1 Native`` 类行解析线程名与 tid。"""
    m = _ANDROID_THREAD_BANNER_RE.match(line)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None


def _normalize_art_native_module(raw_mod: str) -> str:
    """与 extract_stack_frames 内 _extract_module_name 对齐的模块名归一化（供行尾解析复用）。"""
    module = raw_mod.strip()
    if "/" in module:
        module = module.split("/")[-1]
    if "(" in module:
        module = module.split("(")[0]
    if " [" in module:
        module = module.split(" [", 1)[0]
    module = module.strip()
    # Android：``Base.apk!libfoo.so`` 表示 APK 内嵌 so，与符号/库目录匹配时取 ``libfoo.so`` 即可
    mod_l = module.lower()
    if ".apk!" in mod_l:
        idx = mod_l.find(".apk!")
        module = module[idx + len(".apk!") :].strip()
    return module.strip()


def _art_native_outermost_paren_pair(tail: str) -> Optional[Tuple[int, int]]:
    """
    若 ``tail`` 以 ``)`` 结尾，从末尾 ``)`` 向左括号匹配，返回最外层 ``(`` 与 ``)`` 下标（含 C++ 符号内嵌 ``()``）。
    """
    if not tail.endswith(")"):
        return None
    close_i = len(tail) - 1
    depth = 0
    for i in range(close_i, -1, -1):
        if tail[i] == ")":
            depth += 1
        elif tail[i] == "(":
            depth -= 1
            if depth == 0:
                return i, close_i
    return None


def _parse_art_native_pc_tail(tail: str) -> Tuple[str, Optional[str]]:
    """
    解析 ART native 行在 pc 地址之后的部分，例如
    ``/apex/.../libc.so (syscall+28)`` 或 ``[anon:dalvik-DEX data] (Class.method+16)``。
    Tombstone 常见：``...so (offset 0x...) (sym+off) (BuildId: ...)``，须全局剥掉 offset/BuildId 再取符号。
    返回 (module, symbol_or_none)。
    """
    tail = tail.strip()
    tail = re.sub(r"\s+\(BuildId:\s*[^)]+\)", "", tail, flags=re.I)
    tail = re.sub(r"\s+\(offset\s+0x[0-9a-fA-F]+\)", "", tail, flags=re.I)
    tail = tail.strip()
    pair = _art_native_outermost_paren_pair(tail)
    if pair is not None:
        open_i, close_i = pair
        mod_raw = tail[:open_i].rstrip()
        sym = tail[open_i + 1 : close_i].strip()
        sym_l = sym.lower()
        if sym_l in ("deleted", "deferred") and (
            "memfd:" in mod_raw or mod_raw.startswith("/memfd")
        ):
            return _normalize_art_native_module(mod_raw), None
        return _normalize_art_native_module(mod_raw), sym
    return _normalize_art_native_module(tail), None


def _parse_android_java_location(loc: str) -> Tuple[Optional[str], Optional[int]]:
    """Java 栈 ``(Foo.java:12)`` / ``(Native method)`` / ``(D8$$SyntheticClass:0)``。"""
    loc = loc.strip()
    if loc.lower() == "native method":
        return None, None
    m = re.match(r"^(.+):(\d+)$", loc)
    if m:
        return m.group(1).strip(), int(m.group(2))
    return loc, None


def _looks_like_android_java_location(loc: str) -> bool:
    """区分 Android Java/Kotlin 栈与 ArkTS ``file:line:col``（避免误吞）。"""
    loc = loc.strip()
    if loc.lower() == "native method":
        return True
    if "SyntheticClass" in loc or loc.startswith("D8$$"):
        return True
    head = loc.split(":")[0]
    if re.search(r"\.(java|kt|kts|scala|groovy|smali)$", head, re.I):
        return True
    return False


def _android_heuristic_anr_stack(content: str) -> bool:
    """
    无显式 ``ANR`` 字样时：Android + ART native 行 + 行首 ``at`` Java 栈，且非典型 fatal/tombstone 头，
    则视为疑似 ANR / 主线程阻塞采样。

    与仅含 ``#NN pc``（无 ``native:``）的 JNI/GL 崩溃截断片段区分：需至少满足其一——
    行首带 ``native:`` 的 ART 栈，或 ``\"main\" prio=… tid=…`` 线程头。
    """
    if detect_os_type(content) != "android":
        return False
    if not re.search(r"(?:native:\s*)?#\d+\s+pc\s+[0-9a-fA-Fx]+", content):
        return False
    if not re.search(r"^\s*at\s+\S", content, re.MULTILINE):
        return False
    has_art_native_prefix = bool(re.search(r"(?m)^\s*native:\s*#", content))
    first_line = content.splitlines()[0] if content.strip() else ""
    has_thread_banner = bool(_parse_android_thread_banner(first_line))
    if not has_art_native_prefix and not has_thread_banner:
        return False
    cl = content.lower()
    if "fatal signal" in cl:
        return False
    if re.search(r"^\s*signal\s+\d+\s+\(", content, re.MULTILINE | re.IGNORECASE):
        return False
    if "*** *** *** *** *** *** *** *** *** *** *** *** *** *** *** ***" in content:
        return False
    return True


def _android_mixed_native_java_jni_sample(content: str) -> bool:
    """
    ``#NN pc`` + ``at`` Java、无 ``native:``/线程头时，多为 JNI/渲染线程等 native 崩溃截断，
    与 ANR 采样区分。
    """
    if detect_os_type(content) != "android":
        return False
    if not re.search(r"#\d+\s+pc\s+[0-9a-fA-Fx]+", content):
        return False
    if not re.search(r"^\s*at\s+\S", content, re.MULTILINE):
        return False
    if _android_heuristic_anr_stack(content):
        return False
    if "fatal signal" in content.lower():
        return False
    return True


def _android_native_only_pc_stack_sample(content: str) -> bool:
    """
    仅 ``#NN pc`` 的 Android native 栈（可无 Java 帧，常见 ``(no managed stack frames)`` 工作线程），
    与 ANR / JNI 混合栈区分。
    """
    if detect_os_type(content) != "android":
        return False
    if not re.search(r"#\d+\s+pc\s+[0-9a-fA-Fx]+", content):
        return False
    if _android_heuristic_anr_stack(content):
        return False
    if _android_mixed_native_java_jni_sample(content):
        return False
    if "fatal signal" in content.lower():
        return False
    return True


def _android_stack_sample_hint(content: str) -> bool:
    """parse_status：ANR、JNI 混合栈或纯 native #pc 片段均视为有结构线索。"""
    return (
        _android_heuristic_anr_stack(content)
        or _android_mixed_native_java_jni_sample(content)
        or _android_native_only_pc_stack_sample(content)
    )
