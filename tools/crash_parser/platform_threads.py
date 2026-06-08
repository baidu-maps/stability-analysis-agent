#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按平台划分线程栈块（从 core 渐进拆出）。"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import List, Optional, Tuple

from tools.crash_parser.android import _parse_android_thread_banner
from tools.crash_parser.ios_scoping import (
    _ios_apple_thread_block_count,
    _ios_crashed_thread_block,
    _ios_extract_crashed_thread_index,
    _ios_thread_count_from_banner,
)
from tools.crash_parser.stack_extract import _extract_frames_for_crash_segment, extract_stack_frames
from tools.crash_parser.types import CrashParseOptions, ThreadStack, _thread_layer_summary

logger = logging.getLogger(__name__)

ParseThreadsResult = Tuple[List[ThreadStack], Optional[int], int, int]


def parse_threads_android_harmony_tid(
    content: str,
    os_type: str,
    debug: bool,
    opts: CrashParseOptions,
) -> Optional[ParseThreadsResult]:
    """Harmony/Android 多 Tid 块划分；非 Tid 格式返回 None。"""
    if os_type not in ("harmonyos", "android") or "Tid:" not in content:
        return None

    lines = content.splitlines()
    tid_indices: List[Tuple[int, str, str]] = []
    for idx, line in enumerate(lines):
        m = re.search(r"^Tid:(\d+),\s*Name:([^\n]+)$", line.strip())
        if m:
            tid_indices.append((idx, m.group(1).strip(), m.group(2).strip()))

    crash_seg_req = max(1, int(opts.crash_segment_index))
    crash_backtrace_count = 1
    threads: List[ThreadStack] = []

    if not tid_indices:
        stack_frames, crash_backtrace_count, _, _ = _extract_frames_for_crash_segment(
            content, debug, crash_seg_req, global_line_start=1,
        )
        threads.append(
            ThreadStack(
                tid=None,
                name=None,
                is_crash_thread=True,
                is_main_thread=None,
                frames=stack_frames,
                thread_index=None,
                **_thread_layer_summary(stack_frames),
            )
        )
        return threads, 1, crash_backtrace_count, sum(len(t.frames) for t in threads)

    thread_count_total_val = len(tid_indices)
    max_threads = opts.max_threads
    max_primary_frames = opts.max_primary_frames
    max_background_frames = opts.max_background_frames

    def _append_thread(
        thread_tid: str,
        thread_name: str,
        *,
        is_crash_thread: bool,
        is_main_thread: Optional[bool],
        block_start: int,
        block_end: int,
    ) -> None:
        if max_threads > 0 and len(threads) >= max_threads:
            return
        block = "\n".join(lines[block_start:block_end])
        frames = extract_stack_frames(block, debug, base_raw_log_line=block_start + 1)
        if not frames:
            return
        if is_crash_thread and max_primary_frames > 0 and len(frames) > max_primary_frames:
            frames = frames[:max_primary_frames]
        if not is_crash_thread and max_background_frames > 0 and len(frames) > max_background_frames:
            frames = frames[:max_background_frames]
        threads.append(
            ThreadStack(
                tid=thread_tid,
                name=thread_name,
                thread_index=None,
                is_crash_thread=is_crash_thread,
                is_main_thread=is_main_thread,
                frames=frames,
                **_thread_layer_summary(frames),
            )
        )

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
                item
                for item in tid_indices
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
            _append_thread(
                tid, name,
                is_crash_thread=True,
                is_main_thread=True,
                block_start=start_idx,
                block_end=primary_end_idx,
            )
            structured_split_used = bool(threads)

    background_entries = []
    if other_thread_header_idx is not None:
        background_entries = [item for item in tid_indices if item[0] > other_thread_header_idx]

    if structured_split_used:
        for i, (start_idx, tid, name) in enumerate(background_entries):
            next_start = (
                background_entries[i + 1][0] if i + 1 < len(background_entries) else len(lines)
            )
            _append_thread(
                tid, name,
                is_crash_thread=False,
                is_main_thread=False,
                block_start=start_idx,
                block_end=next_start,
            )

    if not threads:
        for i, (start_idx, tid, name) in enumerate(tid_indices):
            if max_threads > 0 and len(threads) >= max_threads:
                break
            end_idx = tid_indices[i + 1][0] if i + 1 < len(tid_indices) else len(lines)
            is_crash = i == 0
            _append_thread(
                tid, name,
                is_crash_thread=is_crash,
                is_main_thread=True if is_crash else False,
                block_start=start_idx,
                block_end=end_idx,
            )

    if crash_seg_req > 1:
        logger.warning(
            "Tid 分块模式下不支持按 backtrace: 分段选择，已忽略 crash_segment_index=%s",
            crash_seg_req,
        )

    total_frames = sum(len(t.frames) for t in threads)
    return threads, thread_count_total_val, crash_backtrace_count, total_frames


def parse_threads_ios(
    content: str,
    debug: bool,
    opts: CrashParseOptions,
) -> ParseThreadsResult:
    """Apple iOS：只解析 Crashed Thread（无该行时取 Thread 0）。"""
    crash_seg_req = max(1, int(opts.crash_segment_index))
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
    threads = [
        ThreadStack(
            tid=None,
            name=None,
            thread_index=ios_thread_index,
            is_crash_thread=True,
            is_main_thread=None,
            frames=stack_frames,
            **_thread_layer_summary(stack_frames),
        )
    ]
    return threads, thread_count_total_val, crash_backtrace_count, len(stack_frames)


def parse_threads_single_block(
    content: str,
    os_type: str,
    debug: bool,
    opts: CrashParseOptions,
) -> ParseThreadsResult:
    """其他平台或 Android/Harmony 无 Tid 的单栈解析。"""
    crash_seg_req = max(1, int(opts.crash_segment_index))
    scope_content = content
    global_line_start = 1

    if os_type in ("harmonyos", "android") and "Stacktrace:" in content and "Tid:" not in content:
        lines_sc = content.splitlines()
        stacktrace_idx = next(
            (i for i, L in enumerate(lines_sc) if L.strip() == "Stacktrace:"), None
        )
        hilog_idx = next((i for i, L in enumerate(lines_sc) if L.strip() == "HiLog:"), None)
        if stacktrace_idx is not None:
            end = hilog_idx if hilog_idx is not None else len(lines_sc)
            scope_content = "\n".join(lines_sc[:end])

    stack_frames, crash_backtrace_count, _, _ = _extract_frames_for_crash_segment(
        scope_content, debug, crash_seg_req, global_line_start=global_line_start,
    )
    threads = [
        ThreadStack(
            tid=None,
            name=None,
            is_crash_thread=True,
            is_main_thread=None,
            frames=stack_frames,
            thread_index=None,
            **_thread_layer_summary(stack_frames),
        )
    ]
    return threads, 1, crash_backtrace_count, len(stack_frames)


def apply_android_thread_metadata(content: str, threads: List[ThreadStack]) -> List[ThreadStack]:
    """Android traces / ANR 首行线程 banner 或 debuggerd pid/tid 行。"""
    if not threads or "Tid:" in content:
        return threads

    first_line = content.splitlines()[0] if content.strip() else ""
    banner = _parse_android_thread_banner(first_line)
    if banner:
        nm, tid_s = banner
        threads[0] = replace(threads[0], tid=tid_s, name=nm)
        return threads

    if threads[0].tid is None:
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
    return threads
