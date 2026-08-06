#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从崩溃日志中提取 Maps 信息，产出 02_memory_maps.json（新编号）。"""

from __future__ import annotations

from typing import Any, Dict, List

from tools.crash_parser.memory_maps import MapEntry, parse_memory_maps


def extract_memory_maps(crash_log_content: str) -> Dict[str, Any]:
    """解析崩溃日志中的 Maps 段，返回结构化数据。

    Args:
        crash_log_content: 原始崩溃日志全文

    Returns:
        适合序列化为 JSON 的字典
    """
    entries: List[MapEntry] = parse_memory_maps(crash_log_content or "")

    if not entries:
        return {
            "maps_present": False,
            "entry_count": 0,
            "entries": [],
            "stack_regions": [],
            "summary": {
                "stack_total_bytes": 0,
                "code_modules": [],
            },
        }

    serialized: List[Dict[str, Any]] = []
    stack_regions: List[Dict[str, Any]] = []
    code_modules: List[str] = []

    for e in entries:
        entry_dict: Dict[str, Any] = {
            "start": f"0x{e.start:x}",
            "end": f"0x{e.end:x}",
            "perms": e.perms,
            "file_offset": f"0x{e.file_offset:x}",
            "path": e.path,
            "executable": e.executable,
            "size_bytes": e.end - e.start,
        }
        serialized.append(entry_dict)

        # 识别 stack 区域
        path_lower = (e.path or "").lower()
        if "[stack" in path_lower:
            stack_regions.append({
                "start": f"0x{e.start:x}",
                "end": f"0x{e.end:x}",
                "path": e.path,
                "size_bytes": e.end - e.start,
            })

        # 收集代码模块
        if e.executable and e.path and not e.path.startswith("["):
            basename = e.basename
            if basename and basename not in code_modules:
                code_modules.append(basename)

    stack_total = sum(r["size_bytes"] for r in stack_regions)

    return {
        "maps_present": True,
        "entry_count": len(serialized),
        "entries": serialized,
        "stack_regions": stack_regions,
        "summary": {
            "stack_total_bytes": stack_total,
            "code_modules": code_modules[:50],  # 限制数量避免过大
        },
    }
