#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把日志里已经有的函数名整理成后续分析可用的堆栈。"""

from __future__ import annotations

import os
from typing import Any, Dict, List


def enterprise_skip_symbol_resolve_enabled() -> bool:
    """企业进程将 ``BD_SA_SKIP_SYMBOL_RESOLVE=1`` 时，跳过堆栈地址解析。"""
    return os.environ.get("BD_SA_SKIP_SYMBOL_RESOLVE") == "1"


def resolved_stack_from_log_symbols(parse_result: Dict[str, Any]) -> Dict[str, Any]:
    """把日志解析结果中已有的函数名整理成下游可继续使用的堆栈。"""
    threads_out: List[Dict[str, Any]] = []
    success = 0
    total = 0
    raw_threads = parse_result.get("threads") if isinstance(parse_result, dict) else None
    if not isinstance(raw_threads, list):
        raw_threads = []
    for thread in raw_threads:
        if not isinstance(thread, dict):
            continue
        frames_out: List[Dict[str, Any]] = []
        for frame in thread.get("frames") or []:
            if not isinstance(frame, dict):
                continue
            total += 1
            function = str(frame.get("function") or "").strip()
            if function:
                success += 1
            frames_out.append({
                "address": str(frame.get("address") or ""),
                "function": function or None,
                "module": frame.get("module"),
                "file": frame.get("file"),
                "line": frame.get("line"),
                "resolved_function": function or None,
                "resolved_file": frame.get("file"),
                "resolved_line": frame.get("line"),
            })
        threads_out.append({
            "name": thread.get("name"),
            "tid": thread.get("tid"),
            "is_crash_thread": bool(thread.get("is_crash_thread")),
            "is_main_thread": thread.get("is_main_thread"),
            "thread_index": thread.get("thread_index"),
            "frames": frames_out,
        })
    meta = parse_result.get("meta_info") if isinstance(parse_result, dict) else None
    os_type = str(meta.get("os_type") or "") if isinstance(meta, dict) else ""
    return {
        "resolved_threads": threads_out,
        "success_count": success,
        "total_count": total,
        "errors": [],
        "os_type": os_type,
        "library_path": "",
        "tool_name": "enterprise_ios_log_symbols",
        "resolution_source": "log_symbolicated_passthrough",
        "skipped_symbol_resolve": True,
    }
