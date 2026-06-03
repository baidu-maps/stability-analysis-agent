#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""崩溃解析共享数据结构与线程辅助。"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
    # 识别到的日志格式（见 crash_parser.parsers 中 format_id）
    log_format: Optional[str] = None

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
