#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""崩溃日志段落检测与 BuildId 提取（写入 01 的精炼字段，非全文镜像）。"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional

# Android tombstone ``build id:`` block / stack-line BuildId
_BUILD_ID_LINE_RE = re.compile(
    r"(?m)^\s*(/[^\s(]+?\.(?:so|dylib|dll|apk))\s+\(BuildId:\s*([0-9a-fA-F]+)\)\s*$"
)
_BUILD_ID_INLINE_RE = re.compile(
    r"(/[^\s(]+?\.(?:so|dylib))\s*\([^)]*\)?\s*\(BuildId:\s*([0-9a-fA-F]+)\)",
    re.IGNORECASE,
)
_BUILD_ID_SECTION_RE = re.compile(
    r"(?ims)^build id:\s*\n(.*?)(?=^(?:stepped registers:|stack:|memory near |pid:|Registers:|Other thread|Maps:|OpenFiles:|HiLog:|metrics:)|\Z)"
)


def extract_build_ids(content: str) -> Optional[Dict[str, str]]:
    """提取 module_basename → BuildId；无则返回 None。"""
    if not content:
        return None
    found: Dict[str, str] = {}

    section = _BUILD_ID_SECTION_RE.search(content)
    blobs = [section.group(1)] if section else []
    blobs.append(content)

    for blob in blobs:
        for pattern in (_BUILD_ID_LINE_RE, _BUILD_ID_INLINE_RE):
            for path, bid in pattern.findall(blob):
                name = os.path.basename(path.strip())
                if name and bid and name not in found:
                    found[name] = bid.lower()
        if found and section:
            break

    return found or None


def detect_raw_log_sections(content: str) -> List[str]:
    """识别原始日志中存在的高价值段落（跨 Android / Harmony / Apple）。"""
    if not content:
        return []
    lower = content.lower()
    present: List[str] = []

    def _add(name: str, cond: bool) -> None:
        if cond and name not in present:
            present.append(name)

    _add(
        "signal",
        bool(
            re.search(r"(?m)^(?:signal\s+\d+|Reason:\s*Signal:|Exception Type:)", content, re.I)
        ),
    )
    _add(
        "registers",
        bool(re.search(r"(?m)^Registers:", content))
        or bool(re.search(r"(?m)^\s+x0\s+[0-9a-fA-F]{8,}", content))
        or bool(re.search(r"(?m)^\s*x0\s*:\s*[0-9a-fA-F]", content)),
    )
    _add(
        "backtrace",
        "backtrace:" in lower
        or bool(re.search(r"(?m)^Fault thread info:", content))
        or bool(re.search(r"(?m)^#\d+\s+pc\s+", content)),
    )
    _add("build_id", "build id:" in lower or "buildid:" in lower)
    _add("submitter_stack", "submitterstacktrace" in lower.replace(" ", ""))
    _add("stepped_registers", "stepped registers:" in lower)
    _add("stack_dump", bool(re.search(r"(?m)^stack:", content)) or "faultstack:" in lower.replace(" ", ""))
    _add("memory_near", "memory near" in lower)
    _add(
        "other_threads",
        "other thread info:" in lower
        or bool(re.search(r"(?m)^pid:\s*\d+,\s*tid:\s*\d+", content))
        and content.lower().count("backtrace:") > 1,
    )
    _add("maps", bool(re.search(r"(?m)^Maps:", content)) or "memory map" in lower)
    _add("open_files", "openfiles:" in lower.replace(" ", "") or "open files:" in lower)
    _add("hilog", bool(re.search(r"(?m)^HiLog:", content)))
    _add("metrics", bool(re.search(r"(?m)^metrics:", content)))
    return present
