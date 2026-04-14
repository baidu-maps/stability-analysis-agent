#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
崩溃日志提取工具
用于提取崩溃日志中的堆栈地址、崩溃信息和元信息
"""

import re
import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, asdict, replace

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@dataclass
class StackFrame:
    """堆栈帧信息"""
    frame_number: int
    address: str
    function: Optional[str] = None
    file: Optional[str] = None
    line: Optional[int] = None
    raw_log_line: Optional[int] = None  # 该帧在原始崩溃日志文件中的 1-based 行号，便于回溯
    module: Optional[str] = None
    offset: Optional[str] = None
    stack_type: Optional[str] = None  # "read", "freed", "allocated", "thread_created"
    library_type: Optional[str] = None  # "system" / "app" / "sdk" / "unknown"
    layer: Optional[str] = None  # 逻辑层：如 "native" / "arkts" / "objc" 等，用于混合栈区分
    language: Optional[str] = None  # 语言/运行时：如 "cpp" / "java" / "arkts" / "objc" / "swift"
    subsystem: Optional[str] = None  # 子系统：如 "gpu" / "render" / "network" / "db"

@dataclass
class CrashInfo:
    """崩溃信息"""
    thread_type: str  # "main" 或 "background"
    crash_reason: str
    signal: Optional[str] = None
    exception_type: Optional[str] = None
    crash_address: Optional[str] = None
    category: Optional[str] = None  # 崩溃类别：native_crash / js_exception / java_exception / oom / anr / gpu_crash / ability_crash 等
    primary_language: Optional[str] = None  # 本次崩溃主语言/运行时，如 "cpp" / "java" / "arkts" / "objc"

@dataclass
class MetaInfo:
    """元信息"""
    os_type: str  # "android", "ios", "linux", "windows", "macos", "harmonyos"
    os_version: Optional[str] = None
    app_version: Optional[str] = None
    device_model: Optional[str] = None
    timestamp: Optional[str] = None
    platform: Optional[str] = None
    compiler: Optional[str] = None
    process_id: Optional[str] = None
    module_base_addresses: Optional[Dict[str, str]] = None
    arch: Optional[str] = None  # 架构信息，如 "arm64", "x86_64", "armv7", "aarch64" 等
    symbol_path: Optional[str] = None  # 符号文件路径（如dSYM、pdb等）
    ability_name: Optional[str] = None  # Harmony Ability 名称（如 EntryAbility）
    process_name: Optional[str] = None  # 进程名（如 com.example.app）
    anr_suspected: Optional[bool] = None  # 是否疑似 ANR / freeze
    # Harmony/Android 等多 Tid 块：日志中识别到的线程块总数；单栈或其它平台多为 1
    thread_count_total: Optional[int] = None
    # 实际写入 threads 的数量（受 CrashParseOptions.max_threads 等限制）
    thread_count_extracted: Optional[int] = None
    # 是否已按 library_dir 裁剪堆栈帧（与 add2line 库白名单规则一致）
    library_dir_frame_filter_applied: Optional[bool] = None
    # 被裁剪掉的帧数（仅当 library_dir_frame_filter_applied 为 True 时有效）
    frames_removed_by_library_dir_filter: Optional[int] = None

@dataclass
class ThreadStack:
    """按线程分组的堆栈信息"""
    tid: Optional[str]
    name: Optional[str]
    # 线程在日志中的序号（如 Apple .crash 的 Thread N / Crashed Thread: N）
    thread_index: Optional[int]
    role: str  # primary / main / background / system 等
    frames: List[StackFrame]
    stack_layers: List[str]  # 该线程栈中出现的层级，如 ["native", "arkts"]
    has_native_frames: bool
    has_arkts_frames: bool  # 是否含 ArkTS/JS 帧
    has_java_frames: bool  # 是否含 Java 帧
    has_objc_frames: bool  # 是否含 Objective-C 帧
    has_swift_frames: bool  # 是否含 Swift 帧
    languages: List[str] = None  # 线程内涉及的语言集合，如 ["cpp","arkts","java","objc","swift"]


@dataclass
class CrashAnalysisResult:
    """崩溃分析结果（按线程分组）"""
    threads: List[ThreadStack]
    crash_info: CrashInfo
    meta_info: MetaInfo
    raw_content: str
    parse_status: str = "ok"  # 解析状态：ok / partial_log / error
    # 日志中检测到的含 backtrace: 的栈块数量（Tid 分块等模式下为 1，且可能未按 backtrace 分段）
    crash_backtrace_sum_count: int = 1
    # 用户请求解析第几段（1-based，见 CrashParseOptions.crash_segment_index）
    crash_backtrace_index_set: int = 1


@dataclass
class CrashParseOptions:
    """崩溃日志解析（第一步）的可调参数；未传入时使用此处默认值。"""
    max_threads: int = 4
    max_primary_frames: int = 50
    max_background_frames: int = 20
    crash_segment_index: int = 1
    save_raw_content: bool = False
    filter_frames_by_library_dir: bool = True
    library_dir: Optional[str] = None


def crash_parse_options_from_cli_args(args: Any) -> CrashParseOptions:
    """从 argparse.Namespace 构造解析选项（供 tools/cli/main.py 使用）。"""
    lib = getattr(args, "library_dir", None)
    lib_abs: Optional[str] = None
    if lib and os.path.exists(lib):
        lib_abs = os.path.abspath(lib)
    return CrashParseOptions(
        max_threads=max(1, int(getattr(args, "max_threads", 4) or 4)),
        max_primary_frames=max(1, int(getattr(args, "max_primary_frames", 50) or 50)),
        max_background_frames=max(1, int(getattr(args, "max_background_frames", 20) or 20)),
        crash_segment_index=max(1, int(getattr(args, "crash_segment_index", 1) or 1)),
        save_raw_content=bool(getattr(args, "save_raw_content", False)),
        filter_frames_by_library_dir=bool(getattr(args, "filter_frames_by_library_dir", True)),
        library_dir=lib_abs,
    )


def _maybe_filter_threads_by_library_dir(
    threads: List[ThreadStack],
    os_type: str,
    options: CrashParseOptions,
) -> Tuple[List[ThreadStack], int, bool]:
    """
    按库目录白名单裁剪各线程 frames（需 options.library_dir 且 options.filter_frames_by_library_dir）。
    返回 (新 threads, 移除的帧数, 是否执行了过滤逻辑)。
    """
    if not options.filter_frames_by_library_dir:
        return threads, 0, False
    lib_raw = (options.library_dir or "").strip()
    if not lib_raw:
        return threads, 0, False
    lib_path = Path(lib_raw)
    if not lib_path.exists():
        logger.warning("library_dir 路径不存在，跳过按库目录过滤堆栈帧: %s", lib_raw)
        return threads, 0, False

    from ._library_frame_whitelist import find_library_files_in_dir, match_libraries_for_module

    if lib_path.is_file():
        library_files = [lib_path]
    elif lib_path.is_dir():
        library_files = find_library_files_in_dir(lib_raw, os_type)
        if not library_files:
            logger.warning("库目录下未找到库文件，跳过按库目录过滤: %s", lib_raw)
            return threads, 0, False
    else:
        return threads, 0, False

    before = sum(len(t.frames) for t in threads)
    new_threads: List[ThreadStack] = []
    for ts in threads:
        kept: List[StackFrame] = []
        for fr in ts.frames:
            mod = fr.module if isinstance(fr.module, str) else None
            if match_libraries_for_module(mod, library_files):
                kept.append(fr)
        if not kept:
            continue
        for i, fr in enumerate(kept):
            fr.frame_number = i
        new_threads.append(
            ThreadStack(
                tid=ts.tid,
                name=ts.name,
                thread_index=ts.thread_index,
                role=ts.role,
                frames=kept,
                **_thread_layer_summary(kept),
            )
        )
    removed = before - sum(len(t.frames) for t in new_threads)
    if removed > 0:
        logger.info(
            "按库目录裁剪堆栈帧: 移除 %s 帧，保留 %s 帧（目录: %s）",
            removed,
            sum(len(t.frames) for t in new_threads),
            lib_raw,
        )
    return new_threads, removed, True


def _thread_layer_summary(frames: List[StackFrame]) -> Dict[str, Any]:
    """根据帧的 layer / language 计算线程的层级与语言摘要"""
    layers = sorted(set(f.layer for f in frames if getattr(f, "layer", None)))
    languages = sorted(set(f.language for f in frames if getattr(f, "language", None)))
    return {
        "stack_layers": layers,
        "has_native_frames": "native" in layers,
        # ArkTS 既可能通过 layer 标识，也可能仅通过 language 标识
        "has_arkts_frames": ("arkts" in layers) or ("arkts" in languages),
        "has_java_frames": "java" in languages,
        "has_objc_frames": "objc" in languages,
        "has_swift_frames": "swift" in languages,
        "languages": languages,
    }

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


# Apple iOS/macOS .crash：「序号 模块 地址 符号 + 偏移」，且 C++ 可出现「+ 0 *(…)」等尾部
_IOS_STACK_PREFIX_RE = re.compile(r"^\s*\d+\s+(\S+)\s+(0x[0-9a-fA-F]+)\s+(.+)$")


def _try_parse_ios_macos_stack_line(line: str) -> Optional[Tuple[str, str, str, str]]:
    """
    解析 Xcode/iOS 风格栈行。返回 (module, address, function, offset) 或 None。
    支持符号尾部为「+ N」或「+ N *(…)」（如 demangled 模板/内联补充）。
    支持「模块 0x基址 + 仅偏移」（无符号名，rest 仅为「+ N」）。
    """
    m = _IOS_STACK_PREFIX_RE.match(line)
    if not m:
        return None
    module, addr, rest = m.group(1), m.group(2), m.group(3)
    rest_st = rest.strip()
    if re.match(r"^\+\s*\d+\s*$", rest_st):
        mrel = re.match(r"^\+\s*(\d+)\s*$", rest_st)
        if mrel:
            return module, addr, "", mrel.group(1)
    # 自右向左匹配最后一个「 + 数字」段（避免函数体内出现「 + 」误截断）
    mx = re.search(r"\s+\+\s+(\d+)(\s*\*\(.+\))?\s*$", rest)
    if mx:
        func = rest[: mx.start()].strip()
        return module, addr, func, mx.group(1)
    mx2 = re.match(r"^(.+?)\s+\+\s+(\d+)\s*$", rest)
    if mx2:
        return module, addr, mx2.group(1).strip(), mx2.group(2)
    return module, addr, rest.strip(), "0"


# 无指令地址、仅「序号 模块  符号 + 偏移」的符号化栈（导出/裁剪后常见）
_IOS_STACK_SYMBOL_ONLY_RE = re.compile(r"^\s*\d+\s+(\S+)\s+(.+)$")


def _try_parse_ios_symbol_only_stack_line(line: str) -> Optional[Tuple[str, str, str, str]]:
    """
    解析不含 0x PC 的 Apple 风格栈行。address 无则空串。
    """
    m = _IOS_STACK_SYMBOL_ONLY_RE.match(line)
    if not m:
        return None
    # Android tombstone「memory near」行：首列为 8–16 位十六进制地址，易被误当成「帧号 + 模块」
    mhead = re.match(r"^\s*(\d+)\s+", line)
    if mhead and len(mhead.group(1)) >= 8:
        return None
    module, rest = m.group(1), m.group(2).strip()
    # 「13 total frames」等统计行
    if module.lower() == "total" and rest.lower().startswith("frames"):
        return None
    if rest.startswith("0x"):
        return None
    mx = re.search(r"\s+\+\s+(\d+)(\s*\*\(.+\))?\s*$", rest)
    if mx:
        func = rest[: mx.start()].strip()
        return module, "", func, mx.group(1)
    mx2 = re.match(r"^(.+?)\s+\+\s+(\d+)\s*$", rest)
    if mx2:
        return module, "", mx2.group(1).strip(), mx2.group(2)
    return module, "", rest, "0"


def _ios_apple_thread_block_count(content: str) -> int:
    """统计「Thread N:」「Thread N Crashed:」「Thread N Deadlock:」等线程栈块数量。"""
    return len(
        re.findall(
            r"(?m)^Thread\s+\d+\s*(?::|Crashed:|Deadlock:)\s*$",
            content,
            re.IGNORECASE,
        )
    )


def _ios_thread_count_from_banner(content: str) -> Optional[int]:
    """KZp / 等导出：「There are N threads...」行中的线程总数。"""
    m = re.search(r"There are\s+(\d+)\s+threads\b", content, re.I)
    if m:
        return int(m.group(1))
    return None


def _ios_crashed_thread_block(content: str) -> Optional[Tuple[str, int]]:
    """
    截取主问题线程栈：优先 Last Exception Backtrace；其次「Thread marked:」；
    再 Crashed Thread N；否则「Thread N Deadlock:」（ANR/死锁）；最后 Thread 0。支持「Thread N Crashed:」。
    """
    lines = content.splitlines()
    # 1) Last Exception Backtrace（KZp / 第三方导出，常与全量线程栈并存）
    for i, line in enumerate(lines):
        if line.strip().startswith("Last Exception Backtrace:"):
            start_idx = i
            end_idx = len(lines)
            for j in range(i + 1, len(lines)):
                L = lines[j].strip()
                if not L:
                    continue
                if re.match(r"^There are \d+ threads", L, re.I):
                    end_idx = j
                    break
                if re.match(r"^Thread \d+", L):
                    end_idx = j
                    break
            while end_idx > start_idx + 1 and not lines[end_idx - 1].strip():
                end_idx -= 1
            return "\n".join(lines[start_idx:end_idx]), start_idx + 1

    # 2) Thread marked:（与 Last Exception 同栈，无 Last Exception 段时）
    for i, line in enumerate(lines):
        if line.strip() == "Thread marked:":
            start_idx = i
            end_idx = len(lines)
            for j in range(i + 1, len(lines)):
                L = lines[j].strip()
                if not L:
                    continue
                if re.match(r"^Thread \d+", L):
                    end_idx = j
                    break
            while end_idx > start_idx + 1 and not lines[end_idx - 1].strip():
                end_idx -= 1
            return "\n".join(lines[start_idx:end_idx]), start_idx + 1

    m = re.search(r"^\s*Crashed Thread:\s*(\d+)\s*$", content, re.MULTILINE)
    if m:
        crashed_n = int(m.group(1))
    else:
        dm = re.search(r"(?m)^Thread\s+(\d+)\s+Deadlock:\s*$", content)
        crashed_n = int(dm.group(1)) if dm else 0
    thread_plain_re = re.compile(rf"^Thread\s+{crashed_n}\s*:\s*$", re.IGNORECASE)
    thread_crashed_re = re.compile(rf"^Thread\s+{crashed_n}\s+Crashed:\s*$", re.IGNORECASE)
    thread_deadlock_re = re.compile(rf"^Thread\s+{crashed_n}\s+Deadlock:\s*$", re.IGNORECASE)
    start_idx: Optional[int] = None
    for i, line in enumerate(lines):
        ls = line.strip()
        if thread_crashed_re.match(ls) or thread_plain_re.match(ls) or thread_deadlock_re.match(ls):
            start_idx = i
            break
    if start_idx is None:
        return None
    end_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        L = lines[i].strip()
        if L.startswith("Binary Images:"):
            end_idx = i
            break
        if re.match(r"^={3,}\s*$", L):
            end_idx = i
            break
        if re.match(r"^Thread\s+\d+\s+crashed with\b", L, re.IGNORECASE):
            end_idx = i
            break
        if re.match(r"^Thread\s+\d+\s+deadlocked with\b", L, re.IGNORECASE):
            end_idx = i
            break
        if re.match(r"^Thread\s+\d+\s*:\s*$", L) or re.match(
            r"^Thread\s+\d+\s+Crashed:\s*$", L, re.IGNORECASE
        ) or re.match(r"^Thread\s+\d+\s+Deadlock:\s*$", L, re.IGNORECASE):
            end_idx = i
            break
    block = "\n".join(lines[start_idx:end_idx])
    return block, start_idx + 1


def _ios_extract_crashed_thread_index(content: str) -> Optional[int]:
    """
    提取 Apple 报告中的崩溃线程序号（Thread N 中的 N）。
    仅用于输出线程元信息，不影响实际栈块裁剪逻辑。
    """
    m = re.search(r"^\s*Crashed Thread:\s*(\d+)\s*$", content, re.MULTILINE)
    if m:
        return int(m.group(1))
    dm = re.search(r"(?m)^Thread\s+(\d+)\s+Deadlock:\s*$", content)
    if dm:
        return int(dm.group(1))
    return None

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


def _extract_frames_for_crash_segment(
    scope_content: str,
    debug: bool,
    crash_segment_index: int,
    *,
    global_line_start: int = 1,
) -> Tuple[List[StackFrame], int, int, int]:
    """
    在 scope_content 内按 ``backtrace:`` 分段，只解析第 resolved 段。

    返回 (frames, backtrace_segment_count, resolved_segment_index, requested_segment_index)；后两者仅用于内部分段逻辑。
    """
    lines = scope_content.splitlines()
    ranges = _backtrace_segment_ranges(lines)
    count = len(ranges)
    req = max(1, int(crash_segment_index))
    resolved = min(req, count)
    if req > count:
        logger.warning(
            "请求的 crash 段索引 %s 超过检测到的段数 %s，已使用第 %s 段",
            req,
            count,
            resolved,
        )
    s, e = ranges[resolved - 1]
    block = "\n".join(lines[s:e])
    frames = extract_stack_frames(block, debug, base_raw_log_line=global_line_start + s)
    return frames, count, resolved, req


def extract_stack_frames(
    content: str,
    debug: bool = False,
    *,
    base_raw_log_line: int = 1,
) -> List[StackFrame]:
    """提取堆栈帧信息。

    base_raw_log_line: 当前 ``content`` 首行在完整原始日志中的 1-based 行号（按线程切块解析时传入）。
    """
    stack_frames = []

    if debug:
        logger.info("开始提取堆栈帧...")

    def _classify_library(module: Optional[str]) -> str:
        if not module:
            return "unknown"
        name = module.strip()
        base = name
        if ".so." in base:
            base = base.split(".so.")[0] + ".so"
        base_lower = base.lower()

        system_prefixes = (
            "libc.so", "libm.so", "libstdc++", "libdl.so",
            "libunwind.so", "liblog.so", "ld-musl-aarch64.so",
            "libsqlite.so",
        )
        system_exact = {
            "ld-musl-aarch64.so.1",
            "libffrt.so",
            "libeventhandler.z.so",
            "libhicollie.z.so",
            "libipc_core.z.so",
            "libipc_common.z.so",
            "libhdc_register.z.so",
            "libappspawn_ace.z.so",
            "appspawn",
        }
        platform_exact = {
            "libark_jsruntime.so",
            "libace_napi.z.so",
        }

        if base_lower.startswith(system_prefixes) or name in system_exact or base in system_exact:
            return "system"
        if name in platform_exact or base in platform_exact:
            return "system"
        return "app"

    def _extract_module_name(module_path: str) -> str:
        """
        从路径/原始模块字段中提取“纯模块名”，去掉路径、括号内符号信息以及尾部的架构/BuildId 装饰。
        
        目标形态：
        - 输入: "/path/to/libxxx.so (_Zfoo+12) [arm64-v8a::xxxxxxxx]"
        - 输出: "libxxx.so"
        - 输入: "/product/.../Foo.apk!libxxx.so" → ``libxxx.so``（APK 内嵌 so）
        """
        module = module_path.split('/')[-1] if '/' in module_path else module_path
        # 去掉括号中的符号信息，例如 "libxxx.so (_Zfoo+12"
        if '(' in module:
            module = module.split('(')[0]
        # 去掉尾部的 "[arm64-v8a::buildId]" 等装饰，只保留纯文件名
        # 日志示例：
        #   libBaiduMapSDK_map_for_privatenavi_v7_6_1.so [arm64-v8a::1cf7a8708f9a04a37f85cdffa7983068]
        if ' [' in module:
            module = module.split(' [', 1)[0]
        module = module.strip()
        mod_l = module.lower()
        if ".apk!" in mod_l:
            idx = mod_l.find(".apk!")
            module = module[idx + len(".apk!") :].strip()
        return module.strip()

    def _extract_js_module(file_path: str) -> Optional[str]:
        if not file_path:
            return None
        if "|" in file_path:
            for part in file_path.split("|"):
                part = part.strip()
                if part.startswith("@"):
                    return part
        return os.path.basename(file_path) or None

    # macOS 带 #N 前缀的栈
    macos_pattern = re.compile(
        r'^#(\d+)\s+([0-9a-fA-Fx]+)\s+(\d+)\s+([^\s]+)\s+([0-9a-fA-Fx]+)\s+([^\s]+)\s+\+\s+(\d+)$'
    )
    # HarmonyOS / Android ART native「#NN pc」与「native: #NN pc」由模块级 _ART_NATIVE_PC_LINE_RE 统一解析
    js_stack_pattern = re.compile(r'^#(\d+)\s+at\s+(.+?)\s+\((.+?):(\d+):(\d+)\)\s*$')
    # HarmonyOS/OpenHarmony 纯 JS 崩溃：Stacktrace: 下 "    at func (path:line:col)" 无 #NN 前缀
    js_stack_plain_pattern = re.compile(r'^\s*at\s+(.+?)\s+\((.+):(\d+):(\d+)\)\s*$')
    # 匿名/入口栈行: "    at (path:line:col)"
    js_stack_anonymous_pattern = re.compile(r'^\s*at\s+\((.+):(\d+):(\d+)\)\s*$')
    asan_pattern = re.compile(
        r'^#(\d+)\s+([0-9a-fA-Fx]+)\s+\(([^)]+)\+([0-9a-fA-Fx]+)\)(?:\s+\(BuildId:\s+[^)]+\))?$'
    )
    current_stack_type = "read"

    for line_no, line in enumerate(content.splitlines(), start=1):
        raw_log_line = base_raw_log_line + line_no - 1
        stripped = line.strip()
        if not stripped:
            continue

        if _ANDROID_SANITIZER_AUX_STACK_HEADER_RE.search(stripped):
            break

        line_lower = stripped.lower()
        if 'freed by' in line_lower:
            current_stack_type = "freed"
        elif 'previously allocated' in line_lower:
            current_stack_type = "allocated"
        elif 'thread' in line_lower and 'created' in line_lower:
            current_stack_type = "thread_created"
        elif 'read of size' in line_lower or 'write of size' in line_lower:
            current_stack_type = "read"

        # Android ART / HarmonyOS："#NN pc" 或 "native: #NN pc"（含 [anon:dalvik-DEX data] 等含空格的模块名）
        match = _ART_NATIVE_PC_LINE_RE.match(stripped)
        if not match:
            match = _ART_NATIVE_PC_IN_LINE_RE.search(stripped)
        if match:
            try:
                frame_num = int(match.group(1))
                address = match.group(2)
                module, sym = _parse_art_native_pc_tail(match.group(3))
                stack_frames.append(StackFrame(
                    frame_number=frame_num,
                    address=address,
                    function=sym,
                    module=module,
                    library_type=_classify_library(module),
                    layer="native",
                    language="cpp",
                    raw_log_line=raw_log_line,
                ))
            except Exception as e:
                logger.warning(f"解析 ART/HarmonyOS native #pc 堆栈帧时出错: {e}")
            continue

        # macOS 带 #N 前缀的栈
        match = macos_pattern.match(stripped)
        if match:
            try:
                frame_num = int(match.group(1))
                address = match.group(2)
                module = match.group(4)
                function = match.group(6)
                offset = match.group(7)
                if function and function != "===" and not function.isdigit():
                    stack_frames.append(StackFrame(
                        frame_number=frame_num,
                        address=address,
                        function=function,
                        module=module,
                        offset=offset,
                        library_type=_classify_library(module),
                        layer="native",
                        language="cpp",
                        raw_log_line=raw_log_line,
                    ))
            except Exception as e:
                logger.warning(f"解析macOS堆栈帧时出错: {e}")
            continue

        # iOS / macOS .crash 风格栈（没有 #N 前缀）；无 0x 地址时再试纯符号行
        ios_parsed = _try_parse_ios_macos_stack_line(line)
        if not ios_parsed:
            ios_parsed = _try_parse_ios_symbol_only_stack_line(line)
        if ios_parsed:
            try:
                module, address, function, offset = ios_parsed
                # Apple 栈行中有一类形如：
                #   libdispatch.dylib  PC  0x18afca000 + 9072
                # 其中「0x18afca000」是 image base，不是函数名。为避免污染语义，将其归一化为 None。
                if function and isinstance(function, str) and re.fullmatch(r"0x[0-9a-fA-F]+", function.strip()):
                    function = None
                # 仅 ObjC selector（-[Class sel] / +[Class sel]）标 objc；Last Exception 也可能是 C++/系统库
                if function and ("-[" in function or "+[" in function):
                    layer = "objc"
                    language = "objc"
                else:
                    layer = "native"
                    language = "cpp"
                stack_frames.append(StackFrame(
                    frame_number=len(stack_frames),
                    address=address or "",
                    function=function,
                    module=module,
                    offset=offset,
                    library_type=_classify_library(module),
                    layer=layer,
                    language=language,
                    raw_log_line=raw_log_line,
                ))
            except Exception as e:
                logger.warning(f"解析iOS/ObjC栈帧时出错: {e}")
            continue

        # Android Java：at com.pkg.Class.method(File.java:12) / (Native method)
        mjava = _ANDROID_JAVA_AT_RE.match(stripped)
        if mjava:
            loc = mjava.group(2).strip()
            if _looks_like_android_java_location(loc):
                try:
                    func_full = mjava.group(1).strip()
                    jfile, jline = _parse_android_java_location(loc)
                    stack_frames.append(StackFrame(
                        frame_number=len(stack_frames),
                        address="",
                        function=func_full,
                        file=jfile,
                        line=jline,
                        module=os.path.basename(jfile) if jfile else None,
                        library_type="app",
                        layer="java",
                        language="java",
                        raw_log_line=raw_log_line,
                    ))
                except Exception as e:
                    logger.warning(f"解析 Android Java 堆栈帧时出错: {e}")
                continue

        match = js_stack_pattern.match(stripped)
        if match:
            try:
                frame_num = int(match.group(1))
                function = match.group(2).strip()
                file_path = match.group(3).strip()
                line_no = int(match.group(4))
                module = _extract_js_module(file_path)
                stack_frames.append(StackFrame(
                    frame_number=frame_num,
                    address="",
                    function=function,
                    file=file_path,
                    line=line_no,
                    module=module,
                    library_type="app",
                    layer="arkts",
                    language="arkts",
                    raw_log_line=raw_log_line,
                ))
            except Exception as e:
                logger.warning(f"解析ArkTS/JS堆栈帧时出错: {e}")
            continue

        match = js_stack_plain_pattern.match(line)
        if match:
            try:
                function = match.group(1).strip()
                file_path = match.group(2).strip()
                line_no = int(match.group(3))
                module = _extract_js_module(file_path)
                stack_frames.append(StackFrame(
                    frame_number=len(stack_frames),
                    address="",
                    function=function,
                    file=file_path,
                    line=line_no,
                    module=module,
                    library_type="app",
                    layer="arkts",
                    language="arkts",
                    raw_log_line=raw_log_line,
                ))
            except Exception as e:
                logger.warning(f"解析纯JS/ArkTS堆栈帧时出错: {e}")
            continue

        match = js_stack_anonymous_pattern.match(line)
        if match:
            try:
                file_path = match.group(1).strip()
                line_no = int(match.group(2))
                module = _extract_js_module(file_path)
                stack_frames.append(StackFrame(
                    frame_number=len(stack_frames),
                    address="",
                    function="(anonymous)",
                    file=file_path,
                    line=line_no,
                    module=module,
                    library_type="app",
                    layer="arkts",
                    language="arkts",
                    raw_log_line=raw_log_line,
                ))
            except Exception as e:
                logger.warning(f"解析匿名JS堆栈帧时出错: {e}")
            continue

        match = asan_pattern.match(stripped)
        if match:
            try:
                frame_num = int(match.group(1))
                # ASan 行形如：
                #   #0 0x7d1e8310f4  (/.../libapp_BaiduMapBaselib.so+0x4b10f4)
                # 这里更关注 so 内偏移量（+0x4b10f4），用于符号解析与对齐 SDK 发布符号，
                # 因此将 address 取为偏移量，而不是绝对 PC 值。
                absolute_pc = match.group(2)
                module = _extract_module_name(match.group(3))
                offset = match.group(4)
                address = offset
                stack_frames.append(StackFrame(
                    frame_number=frame_num,
                    address=address,
                    module=module,
                    offset=offset,
                    stack_type=current_stack_type,
                    library_type=_classify_library(module),
                    layer="native",
                    language="cpp",
                    raw_log_line=raw_log_line,
                ))
            except Exception as e:
                logger.warning(f"解析AddressSanitizer堆栈帧时出错: {e}")
            continue

    if not stack_frames:
        generic_patterns = [
            r'([0-9a-fA-F]{8,16})\s+([^\s]+)\s+([^\s]+)',
            r'([0-9a-fA-F]{8,16})\s+([^\s]+)',
        ]

        for pattern in generic_patterns:
            matches = re.finditer(pattern, content, re.MULTILINE)
            for i, match in enumerate(matches):
                try:
                    address = match.group(1)
                    function = match.group(2) if len(match.groups()) >= 2 else "unknown"
                    if function and function != "===" and not function.isdigit():
                        match_raw_line = (
                            base_raw_log_line + content.count("\n", 0, match.start())
                        )
                        stack_frames.append(StackFrame(
                            frame_number=i,
                            address=address,
                            function=function,
                            library_type=_classify_library(None),
                            layer="native",
                            raw_log_line=match_raw_line,
                        ))
                except Exception as e:
                    logger.warning(f"解析通用堆栈帧时出错: {e}")
                    continue

    unique_frames: List[StackFrame] = []
    seen_addresses = set()

    for frame in stack_frames:
        if frame.layer is None:
            if getattr(frame, "language", None) == "java":
                frame.layer = "java"
            else:
                frame.layer = "native" if (frame.address and not frame.file) else "arkts"
        if frame.language is None:
            if frame.layer == "native":
                frame.language = "cpp"
            elif frame.layer == "arkts":
                frame.language = "arkts"
            elif frame.layer == "objc":
                frame.language = "objc"
            elif frame.layer == "java":
                frame.language = "java"
        unique_key = "|".join([
            frame.address or "",
            frame.function or "",
            frame.file or "",
            str(frame.line or ""),
            frame.module or "",
            str(frame.raw_log_line or ""),
        ])
        if unique_key not in seen_addresses:
            seen_addresses.add(unique_key)
            frame.frame_number = len(unique_frames)
            unique_frames.append(frame)

    return unique_frames

def extract_crash_info(content: str) -> CrashInfo:
    """提取崩溃信息"""
    header_scope = "\n".join(content.splitlines()[:120])
    content_lower = header_scope.lower()

    thread_type = "main"
    crash_reason = "unknown"
    signal = None
    exception_type = None
    category = None
    primary_language = None

    reason_line_match = re.search(r'^Reason:\s*([^\n\r]+)$', content, re.IGNORECASE | re.MULTILINE)
    reason_text = reason_line_match.group(1).strip() if reason_line_match else ""

    signal_reason_map = {
        "SIGSEGV": "segmentation fault",
        "SIGABRT": "abort",
        "SIGILL": "illegal instruction",
        "SIGBUS": "bus error",
        "SIGFPE": "divide by zero",
        "SIGTRAP": "trap",
        "SIGALRM": "timeout",
    }

    # iOS/macOS: Exception Type: EXC_CRASH (SIGABRT)
    exception_type_line = re.search(r'^Exception Type:\s*([^\n\r]+)$', header_scope, re.IGNORECASE | re.MULTILINE)
    if exception_type_line and "exc_crash" in exception_type_line.group(1).lower():
        # 从圆括号中提取信号名称，如 SIGABRT
        m_sig = re.search(r'\(([A-Z0-9_]+)\)', exception_type_line.group(1))
        if m_sig:
            sig_name = m_sig.group(1)
            signal = sig_name
            if crash_reason == "unknown":
                crash_reason = signal_reason_map.get(sig_name, "native_crash")
            if category is None:
                category = "native_crash"
            primary_language = primary_language or "objc"

    signal_match = re.search(r'Signal:([A-Za-z0-9]+)(?:\(([^)]+)\))?', reason_text, re.IGNORECASE)
    if signal_match:
        signal_name = signal_match.group(1).upper()
        signal_code = signal_match.group(2)
        signal = f"{signal_name} ({signal_code})" if signal_code else signal_name
        crash_reason = signal_reason_map.get(signal_name, crash_reason)
    else:
        # 纯 JS/前端崩溃：Reason 为 Error 类型名（TypeError、ReferenceError 等）
        js_error_types = ("TypeError", "ReferenceError", "SyntaxError", "RangeError", "URIError", "EvalError")
        if reason_text and any(reason_text.startswith(t) for t in js_error_types):
            crash_reason = reason_text.split("(")[0].strip() or reason_text
            error_name_match = re.search(r'^Error name:\s*([^\n\r]+)$', header_scope, re.IGNORECASE | re.MULTILINE)
            error_msg_match = re.search(r'^Error message:\s*([^\n\r]+)$', header_scope, re.IGNORECASE | re.MULTILINE)
            err_name = error_name_match.group(1).strip() if error_name_match else ""
            err_msg = error_msg_match.group(1).strip() if error_msg_match else ""
            if err_name or err_msg:
                exception_type = f"{err_name}: {err_msg}".strip(": ").strip() if (err_name and err_msg) else (err_name or err_msg)
            category = category or "js_exception"
            primary_language = primary_language or "arkts"

    crash_patterns = [
        ('segmentation fault', 'segmentation fault'),
        ('segfault', 'segmentation fault'),
        ('段错误', 'segmentation fault'),
        ('null pointer', 'null pointer dereference'),
        ('空指针', 'null pointer dereference'),
        ('out of memory', 'out of memory'),
        ('内存不足', 'out of memory'),
        ('stack overflow', 'stack overflow'),
        ('栈溢出', 'stack overflow'),
        ('illegal instruction', 'illegal instruction'),
        ('非法指令', 'illegal instruction'),
        ('abort', 'abort'),
        ('终止', 'abort'),
        ('access violation', 'access violation'),
        ('访问违规', 'access violation'),
        ('divide by zero', 'divide by zero'),
        ('除零错误', 'divide by zero'),
        ('assertion failed', 'assertion failed'),
        ('断言失败', 'assertion failed'),
        ('exc_bad_access', 'segmentation fault'),
        ('sigsegv', 'segmentation fault'),
        ('sigill', 'illegal instruction'),
        ('sigabrt', 'abort'),
        ('sigalrm', 'timeout'),
    ]

    for pattern, reason in crash_patterns:
        if crash_reason == "unknown" and pattern in content_lower:
            crash_reason = reason
            break

    if not signal:
        signal_patterns = [
            r'signal\s+(\d+)\s*\(([^)]+)\)',
            r'signal\s+(\d+)',
            r'Signal:([A-Za-z0-9]+)\(([^)]+)\)',
            r'Signal:([A-Za-z0-9]+)',
            r'fault\s+addr\s+([0-9a-fA-F]{8,16})',
            r'Exception\s+Code:\s+([0-9a-fA-F]{8})',
            r'signal\s+(\d+)\s+code\s+(-?\d+)',
        ]
        for pattern in signal_patterns:
            signal_match = re.search(pattern, header_scope, re.IGNORECASE)
            if signal_match:
                groups = signal_match.groups()
                signal = f"{signal_match.group(1)} ({signal_match.group(2)})" if len(groups) >= 2 else signal_match.group(1)
                break

    if not signal:
        if 'segmentation fault' in content_lower or 'null pointer' in content_lower or 'sigsegv' in content_lower:
            signal = "11 (SIGSEGV)"
        elif 'illegal instruction' in content_lower or 'sigill' in content_lower:
            signal = "6 (SIGILL)"
        elif 'abort' in content_lower or 'sigabrt' in content_lower:
            signal = "6 (SIGABRT)"
        elif 'timeout' in content_lower or 'sigalrm' in content_lower:
            signal = "14 (SIGALRM)"
        elif 'access violation' in content_lower:
            signal = "0xC0000005"

    # 基于关键字的通用场景分类（category）补全
    low = content_lower
    if category is None:
        if "out of memory" in low or "outofmemoryerror" in low or "lowmemory" in low:
            category = "oom"
        elif "anr" in low or "appfreeze" in low or "application not responding" in low:
            category = "anr"
        elif "gpu" in low or "vulkan" in low or "gles" in low:
            category = "gpu_crash"
        elif "ability" in low or "entryability" in low:
            category = "ability_crash"
        elif signal:
            category = "native_crash"

    if category is None and _android_heuristic_anr_stack(content):
        category = "anr"
        if crash_reason == "unknown":
            crash_reason = "application not responding (suspected)"
        primary_language = primary_language or "java"

    if category is None and _android_mixed_native_java_jni_sample(content):
        category = "native_crash"
        if crash_reason == "unknown":
            crash_reason = "native crash (jni / stack sample)"
        primary_language = primary_language or "cpp"

    if category is None and _android_native_only_pc_stack_sample(content):
        category = "native_crash"
        if crash_reason == "unknown":
            crash_reason = "native crash (stack sample)"
        primary_language = primary_language or "cpp"

    if exception_type is None:
        exception_patterns = [
            r'exception:\s*([^\n\r]+)',
            r'Exception\s+Type:\s*([^\n\r]+)',
            r'Error\s+Type:\s*([^\n\r]+)',
        ]
        for pattern in exception_patterns:
            exception_match = re.search(pattern, header_scope, re.IGNORECASE)
            if exception_match:
                exception_type = exception_match.group(1).strip()
                break

    crash_address = None
    if reason_text:
        reason_address_match = re.search(r'@0x([0-9a-fA-F]+)', reason_text)
        if reason_address_match:
            crash_address = f"0x{reason_address_match.group(1)}"

    if not crash_address:
        address_patterns = [
            r'崩溃地址:\s*0x([0-9a-fA-F]{8,16})',
            r'崩溃地址:\s*([0-9a-fA-F]{8,16})',
            r'crash address:\s*0x([0-9a-fA-F]{8,16})',
            r'crash address:\s*([0-9a-fA-F]{8,16})',
            r'fault address:\s*0x([0-9a-fA-F]{8,16})',
            r'fault address:\s*([0-9a-fA-F]{8,16})',
            # Android logcat / tombstone：``fault addr 0xa0``
            r'fault\s+addr\s+(0x[0-9a-fA-F]{1,16})\b',
        ]

        for pattern in address_patterns:
            address_match = re.search(pattern, header_scope, re.IGNORECASE)
            if address_match:
                crash_address = address_match.group(1)
                break

    return CrashInfo(
        thread_type=thread_type,
        crash_reason=crash_reason,
        signal=signal,
        exception_type=exception_type,
        crash_address=crash_address,
        category=category,
        primary_language=primary_language,
    )

def extract_meta_info(content: str) -> MetaInfo:
    """提取元信息"""
    os_type = detect_os_type(content)
    header_scope = "\n".join(content.splitlines()[:200])
    
    # 提取平台信息
    platform = None
    platform_match = re.search(r'^平台:\s*([^\n\r]+)$', header_scope, re.MULTILINE)
    if platform_match:
        platform = platform_match.group(1).strip()
    
    # 提取进程名（如 iOS Process: / Android Process:）
    process_name = None
    proc_match = re.search(r'^Process:\s*([^\[\n\r]+)', header_scope, re.IGNORECASE | re.MULTILINE)
    if proc_match:
        process_name = proc_match.group(1).strip()
    if process_name is None:
        cmdline_m = re.search(r'Cmdline:\s*(\S+)', header_scope, re.IGNORECASE)
        if cmdline_m:
            process_name = cmdline_m.group(1).strip()

    # 提取时间戳
    timestamp = None
    timestamp_match = re.search(r'^(?:时间|Timestamp):\s*([^\n\r]+)$', header_scope, re.IGNORECASE | re.MULTILINE)
    if timestamp_match:
        timestamp = timestamp_match.group(1).strip()
    
    # 提取进程ID
    process_id = None
    pid_match = re.search(r'^(?:进程ID|Pid):\s*(\d+)$', header_scope, re.IGNORECASE | re.MULTILINE)
    if pid_match:
        process_id = pid_match.group(1)
    if process_id is None:
        # Android DEBUG dump：``pid: 9568, tid: 9753, name: ...``
        pid_dbg = re.search(
            r'\bpid:\s*(\d+),\s*tid:\s*\d+',
            header_scope,
            re.IGNORECASE,
        )
        if pid_dbg:
            process_id = pid_dbg.group(1)
    
    # 提取编译器信息
    compiler = None
    compiler_match = re.search(r'^(?:编译器|Compiler):\s*([^\n\r]+)$', header_scope, re.IGNORECASE | re.MULTILINE)
    if compiler_match:
        compiler = compiler_match.group(1).strip()
    
    # 提取模块基址信息
    module_base_addresses = {}
    
    # 提取主程序基址
    main_base_match = re.search(r'主程序基址:\s*([0-9a-fA-Fx]+)', content)
    if main_base_match:
        module_base_addresses['main'] = main_base_match.group(1)
    
    # 提取libmylib.dylib基址
    lib_base_match = re.search(r'libmylib\.dylib基址:\s*([0-9a-fA-Fx]+)', content)
    if lib_base_match:
        module_base_addresses['libmylib.dylib'] = lib_base_match.group(1)
    
    # 提取libsystem_pthread.dylib基址
    pthread_base_match = re.search(r'libsystem_pthread\.dylib基址:\s*([0-9a-fA-Fx]+)', content)
    if pthread_base_match:
        module_base_addresses['libsystem_pthread.dylib'] = pthread_base_match.group(1)
    
    # 提取libsystem_platform.dylib基址
    platform_base_match = re.search(r'libsystem_platform\.dylib基址:\s*([0-9a-fA-Fx]+)', content)
    if platform_base_match:
        module_base_addresses['libsystem_platform.dylib'] = platform_base_match.group(1)
    
    # 如果基址信息缺失，尝试从堆栈跟踪中自动提取（macOS格式）
    if not module_base_addresses:
        # macOS堆栈格式: #1 0x1023cb2bc 1   libmylib.dylib  0x00000001023cb2bc _Z14signal_handleriP9__siginfoPv + 468
        # 从堆栈跟踪中提取每个模块的第一个符号地址，使用页对齐算法估算基址
        macos_stack_pattern = r'#\d+\s+([0-9a-fA-Fx]+)\s+\d+\s+([^\s]+)\s+([0-9a-fA-Fx]+)\s+[^\s]+\s+\+\s+\d+'
        matches = re.finditer(macos_stack_pattern, content, re.MULTILINE)
        
        module_first_addresses = {}  # 记录每个模块的第一个地址
        for match in matches:
            actual_addr_str = match.group(1)
            module = match.group(2)
            symbol_addr_str = match.group(3)
            
            try:
                actual_addr = int(actual_addr_str, 16)
                symbol_addr = int(symbol_addr_str, 16)
                
                # 对于每个模块，记录第一个地址
                if module not in module_first_addresses:
                    module_first_addresses[module] = actual_addr
                    
                    # 使用页对齐算法估算基址
                    # macOS 通常使用 64KB 或 1MB 页对齐
                    # 尝试多种对齐方式，选择最小的合理基址
                    candidates = [
                        (actual_addr // 0x10000) * 0x10000,  # 64KB对齐
                        (actual_addr // 0x100000) * 0x100000,  # 1MB对齐
                        (actual_addr // 0x1000) * 0x1000,  # 4KB对齐（备用）
                    ]
                    
                    # 选择最小的合理候选（但至少是4KB对齐）
                    for candidate in candidates:
                        if candidate > 0 and candidate <= actual_addr:
                            module_base_addresses[module] = f"0x{candidate:x}"
                            logger.debug(f"从堆栈跟踪估算模块基址: {module} = 0x{candidate:x} (从地址 0x{actual_addr:x})")
                            break
            except (ValueError, AttributeError):
                continue
    
    # 增强的OS版本提取
    os_version = None
    version_patterns = {
        'android': [
            r'android\s+([0-9.]+)',
            r'API\s+level\s+(\d+)',
            r'SDK\s+([0-9.]+)',
        ],
        'ios': [
            r'^\s*OS Version:\s*iOS\s+([0-9.]+)',
            r'ios\s+([0-9.]+)',
            r'iPhone\s+OS\s+([0-9.]+)',
            r'iPadOS\s+([0-9.]+)',
        ],
        'linux': [
            r'linux\s+([0-9.]+)',
            r'kernel\s+([0-9.]+)',
            r'ubuntu\s+([0-9.]+)',
            r'centos\s+([0-9.]+)',
            r'debian\s+([0-9.]+)',
        ],
        'macos': [
            r'macos\s+([0-9.]+)',
            r'os\s+x\s+([0-9.]+)',
            r'darwin\s+([0-9.]+)',
        ],
        'windows': [
            r'windows\s+([0-9.]+)',
            r'win\s+([0-9.]+)',
            r'nt\s+([0-9.]+)',
        ]
    }
    
    if os_type in version_patterns:
        for pattern in version_patterns[os_type]:
            version_match = re.search(pattern, content, re.IGNORECASE | re.MULTILINE)
            if version_match:
                os_version = version_match.group(1)
                break
    
    # 增强的应用版本提取
    app_version = None
    app_version_patterns = [
        r'^Version:\s*([0-9.]+)$',
        r'^version[:\s]+([0-9.]+)$',
        r'^ver[:\s]+([0-9.]+)$',
        r'^build[:\s]+([0-9.]+)$',
        r'^app[:\s]+([0-9.]+)$',
        # Android tombstone / bugreport：``Package: com.foo v123 (1.2.3)`` 取括号内版本
        r'^\s*Package:\s+\S+\s+v\d+\s+\(([^)]+)\)',
    ]
    
    for pattern in app_version_patterns:
        version_match = re.search(pattern, header_scope, re.IGNORECASE | re.MULTILINE)
        if version_match:
            app_version = version_match.group(1)
            break
    
    # 增强的设备型号提取
    device_model = None
    device_patterns = [
        r'^Device info:\s*([^\n\r]+)$',
        r'^Device:\s*([^\n\r]+)$',
        r'^设备(?:信息|型号)?[:：]\s*([^\n\r]+)$',
        r'^Model:\s*([^\n\r]+)$',
        r'^Hardware:\s*([^\n\r]+)$',
        r'^Hardware Model:\s*([^\n\r]+)$',
    ]
    
    for pattern in device_patterns:
        model_match = re.search(pattern, header_scope, re.IGNORECASE | re.MULTILINE)
        if model_match:
            device_model = model_match.group(1).strip()
            break
    
    # 如果没有检测到OS类型，根据平台信息推断
    if os_type == 'unknown' and platform:
        if platform.lower() == 'mac':
            os_type = 'macos'
        elif platform.lower() in ['android', 'ios', 'linux', 'windows']:
            os_type = platform.lower()
    
    # 如果没有检测到OS类型，根据内容特征推断（勿用裸子串 dyld：iOS Binary Images 含 libdyld.dylib）
    if os_type == "unknown":
        if _IOS_OS_VERSION_RE.search(content) or _IOS_HW_MODEL_RE.search(content):
            os_type = "ios"
        elif _detect_apple_ios_truncated_crash(content):
            os_type = "ios"
        elif _detect_apple_ios_freeze_report(content):
            os_type = "ios"
        elif _detect_ios_mach_tool_export(content):
            os_type = "ios"
        elif re.search(r"^\s*OS Version:\s*macOS\b", content, re.MULTILINE | re.IGNORECASE) or re.search(
            r"^\s*OS Version:\s*Mac OS X\b", content, re.MULTILINE | re.IGNORECASE
        ):
            os_type = "macos"
        elif "apple llvm" in content.lower():
            os_type = "macos"
        elif platform and platform.lower() == "mac":
            os_type = "macos"
    
    # 提取架构信息
    arch = None
    arch_patterns = [
        r'^(?:architecture|arch|架构)[:：]\s*([A-Za-z0-9._-]+)$',
        r'\b(arm64-v8a|armeabi-v7a|armeabi)\b',
        # Tombstone：``ABI: 'arm64'``
        r'^\s*ABI:\s*[\'"]?([a-zA-Z0-9_-]+)[\'"]?\s*$',
    ]
    
    for pattern in arch_patterns:
        arch_match = re.search(pattern, header_scope, re.IGNORECASE | re.MULTILINE)
        if arch_match:
            arch = arch_match.group(1).strip() if arch_match.lastindex else arch_match.group(0)
            break
    
    # 如果没找到，从模块路径或堆栈信息中推断
    if not arch:
        # 从路径中提取架构信息（如 /lib/arm64/, /lib64/, /data/storage/el1/bundle/libs/arm64/）
        path_arch_patterns = [
            r'/lib(?:64)?/(arm64|aarch64|x86_64|armv7|armv8|i386|i686|x86)',
            r'/libs/(arm64|aarch64|x86_64|armv7|armv8|i386|i686|x86)',
        ]
        for pattern in path_arch_patterns:
            arch_match = re.search(pattern, content, re.IGNORECASE)
            if arch_match:
                arch = arch_match.group(1) if arch_match.lastindex else arch_match.group(0)
                # 标准化架构名称
                if arch.lower() in ['aarch64', 'arm64']:
                    arch = 'arm64'
                elif arch.lower() in ['x86_64', 'amd64']:
                    arch = 'x86_64'
                elif arch.lower() in ['armv7', 'armeabi-v7a']:
                    arch = 'armv7'
                break
    
    # 提取符号文件路径
    symbol_path = None
    symbol_patterns = [
        r'symbol[_\s]?path[:\s]+([^\n\r]+)',
        r'dsym[_\s]?path[:\s]+([^\n\r]+)',
        r'\.dSYM[:\s]+([^\n\r]+)',
        r'\.pdb[:\s]+([^\n\r]+)',
        r'symbol[_\s]?file[:\s]+([^\n\r]+)',
    ]
    for pattern in symbol_patterns:
        symbol_match = re.search(pattern, content, re.IGNORECASE)
        if symbol_match:
            symbol_path = symbol_match.group(1).strip()
            break
    
    # 增强编译器信息提取
    if not compiler:
        compiler_patterns = [
            r'^(Apple LLVM version[^\n\r]+)$',
            r'^(?:clang|gcc|g\+\+) version[^\n\r]+$',
            r'^(?:llvm|msvc)[^\n\r]*$',
        ]
        for pattern in compiler_patterns:
            compiler_match = re.search(pattern, header_scope, re.IGNORECASE | re.MULTILINE)
            if compiler_match:
                compiler = compiler_match.group(1) if compiler_match.lastindex else compiler_match.group(0)
                break
    
    # Harmony Ability 名称（简单从文本中抓取包含 EntryAbility 的行）
    ability_name = None
    ability_match = re.search(r'EntryAbility', content)
    if ability_match:
        ability_name = "EntryAbility"

    # ANR / freeze 线索
    anr_suspected = False
    if re.search(r'\bANR\b', content, re.IGNORECASE) or "appfreeze" in content.lower():
        anr_suspected = True
    if _android_heuristic_anr_stack(content):
        anr_suspected = True

    return MetaInfo(
        os_type=os_type,
        os_version=os_version,
        app_version=app_version,
        device_model=device_model,
        timestamp=timestamp,
        platform=platform,
        compiler=compiler,
        process_id=process_id,
        module_base_addresses=module_base_addresses if module_base_addresses else None,
        arch=arch,
        symbol_path=symbol_path,
        ability_name=ability_name,
        process_name=process_name,
        anr_suspected=anr_suspected if anr_suspected else None,
    )

def _parse_crash_core(
    content: str,
    os_type: Optional[str] = None,
    debug: bool = False,
    *,
    options: Optional[CrashParseOptions] = None,
) -> CrashAnalysisResult:
    """核心解析逻辑：按线程分组堆栈 + 提取 crash/meta 信息"""
    opts = options if options is not None else CrashParseOptions()
    if os_type is None:
        os_type = detect_os_type(content)

    crash_seg_req = max(1, int(opts.crash_segment_index))
    crash_backtrace_count = 1
    # 与 threads 列表对应的「总线程块数」语义，见下方各分支
    thread_count_total_val: Optional[int] = None

    threads: List[ThreadStack] = []
    total_frames = 0

    # HarmonyOS / Android 等多线程 dump：尝试按 Tid 块划分
    if os_type in ("harmonyos", "android") and "Tid:" in content:
        lines = content.splitlines()
        tid_indices: List[Tuple[int, str, str]] = []
        for idx, line in enumerate(lines):
            m = re.search(r'^Tid:(\d+),\s*Name:([^\n]+)$', line.strip())
            if m:
                tid_indices.append((idx, m.group(1).strip(), m.group(2).strip()))
        if not tid_indices:
            thread_count_total_val = 1
            stack_frames, crash_backtrace_count, _, _ = _extract_frames_for_crash_segment(
                content, debug, crash_seg_req, global_line_start=1,
            )
            total_frames = len(stack_frames)
            threads.append(ThreadStack(
                tid=None, name=None, role="primary", frames=stack_frames,
                thread_index=None,
                **_thread_layer_summary(stack_frames),
            ))
        else:
            thread_count_total_val = len(tid_indices)
            max_threads = opts.max_threads
            max_primary_frames = opts.max_primary_frames
            max_background_frames = opts.max_background_frames

            def _append_thread(thread_tid: str, thread_name: str, role: str, block_start: int, block_end: int) -> None:
                nonlocal total_frames
                if max_threads > 0 and len(threads) >= max_threads:
                    return
                block = "\n".join(lines[block_start:block_end])
                frames = extract_stack_frames(block, debug, base_raw_log_line=block_start + 1)
                if not frames:
                    return
                if role == "primary" and max_primary_frames > 0 and len(frames) > max_primary_frames:
                    frames = frames[:max_primary_frames]
                if role != "primary" and max_background_frames > 0 and len(frames) > max_background_frames:
                    frames = frames[:max_background_frames]
                threads.append(ThreadStack(
                    tid=thread_tid, name=thread_name, thread_index=None, role=role, frames=frames,
                    **_thread_layer_summary(frames),
                ))
                total_frames += len(frames)

            fault_thread_header_idx = next(
                (idx for idx, line in enumerate(lines) if line.strip() == "Fault thread info:"),
                None,
            )
            submitter_idx = next(
                (idx for idx, line in enumerate(lines) if "SubmitterStacktrace" in line),
                None,
            )
            registers_idx = next(
                (idx for idx, line in enumerate(lines) if line.strip() == "Registers:"),
                None,
            )
            other_thread_header_idx = next(
                (idx for idx, line in enumerate(lines) if line.strip() == "Other thread info:"),
                None,
            )

            structured_split_used = False
            if fault_thread_header_idx is not None:
                primary_entry = next(
                    (
                        item for item in tid_indices
                        if item[0] > fault_thread_header_idx
                        and (other_thread_header_idx is None or item[0] < other_thread_header_idx)
                    ),
                    None,
                )
                if primary_entry:
                    start_idx, tid, name = primary_entry
                    end_candidates = [len(lines)]
                    for marker_idx in (submitter_idx, registers_idx, other_thread_header_idx):
                        if marker_idx is not None and marker_idx > start_idx:
                            end_candidates.append(marker_idx)
                    primary_end_idx = min(end_candidates)
                    _append_thread(tid, name, "primary", start_idx, primary_end_idx)
                    structured_split_used = bool(threads)

            background_entries = []
            if other_thread_header_idx is not None:
                background_entries = [item for item in tid_indices if item[0] > other_thread_header_idx]

            if structured_split_used:
                for i, (start_idx, tid, name) in enumerate(background_entries):
                    next_start = background_entries[i + 1][0] if i + 1 < len(background_entries) else len(lines)
                    _append_thread(tid, name, "background", start_idx, next_start)

            if not threads:
                for i, (start_idx, tid, name) in enumerate(tid_indices):
                    if max_threads > 0 and len(threads) >= max_threads:
                        break
                    end_idx = tid_indices[i + 1][0] if i + 1 < len(tid_indices) else len(lines)
                    role = "primary" if i == 0 else "background"
                    _append_thread(tid, name, role, start_idx, end_idx)
            if crash_seg_req > 1:
                logger.warning(
                    "Tid 分块模式下不支持按 backtrace: 分段选择，已忽略 crash_segment_index=%s",
                    crash_seg_req,
                )
    elif os_type == "ios":
        # Apple iOS：只解析 Crashed Thread（无该行时取 Thread 0），避免多线程栈混成一条
        thread_count_total_val = _ios_apple_thread_block_count(content)
        banner_n = _ios_thread_count_from_banner(content)
        if banner_n is not None:
            thread_count_total_val = banner_n
        scoped = _ios_crashed_thread_block(content)
        if scoped:
            scope_content, global_line_start = scoped
        else:
            scope_content = content
            global_line_start = 1
        ios_thread_index = _ios_extract_crashed_thread_index(content)
        stack_frames, crash_backtrace_count, _, _ = _extract_frames_for_crash_segment(
            scope_content, debug, crash_seg_req, global_line_start=global_line_start,
        )
        total_frames = len(stack_frames)
        threads.append(ThreadStack(
            tid=None, name=None, thread_index=ios_thread_index, role="primary", frames=stack_frames,
            **_thread_layer_summary(stack_frames),
        ))
    else:
        # 其他平台 或 HarmonyOS/Android 无 Tid（含纯 JS/前端崩溃）：单线程
        thread_count_total_val = 1
        scope_content = content
        global_line_start = 1
        if os_type in ("harmonyos", "android") and "Stacktrace:" in content and "Tid:" not in content:
            lines_sc = content.splitlines()
            stacktrace_idx = next((i for i, L in enumerate(lines_sc) if L.strip() == "Stacktrace:"), None)
            hilog_idx = next((i for i, L in enumerate(lines_sc) if L.strip() == "HiLog:"), None)
            if stacktrace_idx is not None:
                end = hilog_idx if hilog_idx is not None else len(lines_sc)
                scope_content = "\n".join(lines_sc[:end])
        stack_frames, crash_backtrace_count, _, _ = _extract_frames_for_crash_segment(
            scope_content, debug, crash_seg_req, global_line_start=global_line_start,
        )
        total_frames = len(stack_frames)
        threads.append(ThreadStack(
            tid=None, name=None, role="primary", frames=stack_frames,
            thread_index=None,
            **_thread_layer_summary(stack_frames),
        ))

    # Android traces / ANR 片段：首行 "main" prio=5 tid=1 Native（与 Tid: 分块格式并存时以 Tid: 为准）
    if os_type == "android" and threads and "Tid:" not in content:
        first_line = content.splitlines()[0] if content.strip() else ""
        banner = _parse_android_thread_banner(first_line)
        if banner:
            nm, tid_s = banner
            threads[0] = replace(threads[0], tid=tid_s, name=nm)
        elif threads and threads[0].tid is None:
            m_dbg = re.search(
                r"pid:\s*\d+,\s*tid:\s*(\d+),\s*name:\s*([^\s>]+)\s*>>>",
                content,
                re.IGNORECASE,
            )
            if m_dbg:
                threads[0] = replace(
                    threads[0],
                    tid=m_dbg.group(1).strip(),
                    name=m_dbg.group(2).strip(),
                )

    threads, removed_by_lib_filter, lib_filter_applied = _maybe_filter_threads_by_library_dir(
        threads, os_type, opts,
    )
    total_frames = sum(len(t.frames) for t in threads)

    crash_info = extract_crash_info(content)
    meta_info = extract_meta_info(content)
    meta_info = replace(
        meta_info,
        thread_count_total=thread_count_total_val,
        thread_count_extracted=len(threads),
        library_dir_frame_filter_applied=lib_filter_applied if lib_filter_applied else None,
        frames_removed_by_library_dir_filter=(
            removed_by_lib_filter if lib_filter_applied else None
        ),
    )

    # 依据可用信息粗略判断日志完整性，给出 parse_status
    parse_status = "ok"
    content_lower = content.lower()
    header_markers = [
        "generated by",
        "device info:",
        "build info:",
        "fingerprint:",
        "reason:",
        "exception type:",
        "binary images:",
        "deadlock",
        "crashdoctor",
        "freeze type:",
        "freeze interval:",
        "卡顿",
        # Android logcat / debuggerd
        "fatal signal",
        "cmdline:",
    ]
    has_header_hint = any(m in content_lower for m in header_markers)
    if _android_stack_sample_hint(content):
        has_header_hint = True

    has_threads = bool(threads)
    has_frames = total_frames > 0

    # 情况 1：只有堆栈片段，没有明显头部/元信息，视为 partial_log
    if has_frames and has_threads and not has_header_hint:
        parse_status = "partial_log"

    # 情况 2：完全没有帧，视为 error
    if not has_frames:
        parse_status = "error"

    raw_content = content if opts.save_raw_content else ""

    return CrashAnalysisResult(
        threads=threads,
        crash_info=crash_info,
        meta_info=meta_info,
        raw_content=raw_content,
        parse_status=parse_status,
        crash_backtrace_sum_count=crash_backtrace_count,
        crash_backtrace_index_set=crash_seg_req,
    )


class BaseCrashParser:
    """解析器基类：用于后续按场景/平台拆分实现"""

    def can_handle(self, content: str, os_type: str) -> bool:
        return True

    def parse(
        self,
        content: str,
        os_type: str,
        debug: bool = False,
        options: Optional[CrashParseOptions] = None,
    ) -> CrashAnalysisResult:
        raise NotImplementedError


class DefaultCrashParser(BaseCrashParser):
    """默认解析器：使用现有统一逻辑"""

    def parse(
        self,
        content: str,
        os_type: str,
        debug: bool = False,
        options: Optional[CrashParseOptions] = None,
    ) -> CrashAnalysisResult:
        return _parse_crash_core(
            content, os_type=os_type, debug=debug, options=options,
        )


PARSERS: List[BaseCrashParser] = [
    DefaultCrashParser(),
]


def crash_log_parser(
    content: str,
    debug: bool = False,
    options: Optional[CrashParseOptions] = None,
) -> str:
    """
    崩溃日志提取工具（带解析器注册表）

    options: 解析参数；未传入时使用 CrashParseOptions 默认值（含 crash_segment_index=1）。
    """
    try:
        logger.info("开始解析崩溃日志...")
        # 部分抓包工具会在文本中插入 NUL，会导致 splitlines 与编辑器行号不一致
        content = content.replace("\x00", "")
        os_type = detect_os_type(content)

        opts = options if options is not None else CrashParseOptions()
        opts = replace(opts, crash_segment_index=max(1, int(opts.crash_segment_index)))

        parser: BaseCrashParser = PARSERS[0]
        for p in PARSERS:
            if p.can_handle(content, os_type=os_type):
                parser = p
                break

        result = parser.parse(
            content,
            os_type=os_type,
            debug=debug,
            options=opts,
        )

        # 先转成 dict，再做一次结构化清洗：
        # - 去掉所有值为 False 的 has_xxx_frames 标记，减少噪音
        # - 保留其它字段完整输出
        def _strip_false_flags(obj: Any) -> Any:
            if isinstance(obj, dict):
                new_obj: Dict[str, Any] = {}
                for k, v in obj.items():
                    # 统一通过 has_xxx_frames 这一类字段判断是否包含某类帧：
                    # 为 False 时直接从输出中移除，True 则保留
                    if k.startswith("has_") and v is False:
                        continue
                    new_obj[k] = _strip_false_flags(v)
                return new_obj
            if isinstance(obj, list):
                return [_strip_false_flags(i) for i in obj]
            return obj

        raw_dict = asdict(result)
        cleaned_dict = _strip_false_flags(raw_dict)

        json_result = json.dumps(cleaned_dict, ensure_ascii=False, indent=2)
        logger.info("崩溃日志提取完成")
        return json_result

    except Exception as e:
        logger.error(f"解析崩溃日志时出错: {e}")
        error_result = {
            "error": str(e),
            "raw_content": content
        }
        return json.dumps(error_result, ensure_ascii=False, indent=2)

# 测试代码
if __name__ == "__main__":
    import sys
    
    # 如果从stdin读取输入
    if not sys.stdin.isatty():
        content = sys.stdin.read()
        result = crash_log_parser(content)
        print(result)
    else:
        # 示例崩溃日志
        sample_log = """
        === Stability Analysis Agent Demo - Crash Test ===
        PID: 12345
        Starting crash demonstration...
        CrashClass::dangerousMethod() called
        zsh: segmentation fault
        """
        
        result = crash_log_parser(sample_log)
        print("解析结果:")
        print(result)


# ==================== CrashLogParserTool (BaseTool wrapper) ====================

import logging as _logging
from typing import Any as _Any, Dict as _Dict, Optional as _Optional

from tool_system.tool import BaseTool, ToolDefinition
from tool_system.registry import Priority

_tool_logger = _logging.getLogger(__name__)


class CrashLogParserTool(BaseTool):
    """崩溃日志解析工具 — 内置 Tool 实现，自包含所有解析逻辑。"""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="crash_log_parser",
            description="解析崩溃日志，提取堆栈地址、异常类型、崩溃原因等信息。支持 iOS/Android/鸿蒙等平台。",
            input_schema={
                "type": "object",
                "properties": {
                    "log_content": {"type": "string", "description": "崩溃日志内容"},
                    "debug": {"type": "boolean", "description": "调试模式", "default": False},
                },
                "required": ["log_content"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "thread_type": {"type": "string"},
                    "crash_reason": {"type": "string"},
                    "signal": {"type": "string"},
                    "exception_type": {"type": "string"},
                    "stack_frames": {"type": "array"},
                },
            },
            category="parser",
            version="1.0.0",
        )

    def execute(self, input_data: _Dict[str, _Any]) -> _Dict[str, _Any]:
        import json as _json

        log_content = input_data.get("log_content", "")
        debug = input_data.get("debug", False)

        options = None
        if "options" in input_data:
            opts_dict = input_data["options"]
            options = CrashParseOptions(**opts_dict)

        if options is None:
            options = CrashParseOptions()

        result = crash_log_parser(log_content, debug=debug, options=options)
        try:
            parsed = _json.loads(result)
        except Exception:
            parsed = {"raw_result": result}
        return parsed

    def validate_input(self, input_data: _Dict[str, _Any]) -> "tuple[bool, _Optional[str]]":
        if "log_content" not in input_data:
            return False, "缺少 required 字段: log_content"
        return True, None
