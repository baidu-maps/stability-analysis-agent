#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""进程 Maps 解析与 VA 查找（仅作内存索引，不把全文写入 01）。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

# Harmony / Android /proc-style maps:
#   6590d64000-659183d000 r-xp 00363000 /data/.../lib.so
#   70f1234000-70f1235000 r-xp 00000000 00:00 0 /system/lib64/libc.so
_MAP_LINE_RE = re.compile(
    r"^([0-9a-fA-F]+)-([0-9a-fA-F]+)\s+"
    r"([rwxps-]{3,4})\s+"
    r"([0-9a-fA-F]+)"
    r"(?:\s+[0-9a-fA-F]+:[0-9a-fA-F]+\s+\d+)?"  # optional major:minor inode
    r"\s*(.*)$"
)

_MAPS_SECTION_RE = re.compile(
    r"(?ims)^(?:Maps|memory map)[^\n]*:\s*\n(.*?)(?=^"
    r"(?:OpenFiles:|Open Files:|HiLog:|-----|=======|logcat:|Logcat:)"
    r"|\Z)"
)

# Apple Binary Images (subset): 0x100000000 - 0x1000fffff libmylib.dylib ...
_BINARY_IMAGES_RE = re.compile(
    r"(?ims)^Binary Images:\s*\n(.*?)(?=^=======|\Z)"
)
_BINARY_IMAGE_LINE_RE = re.compile(
    r"^\s*(0x[0-9a-fA-F]+)\s*-\s*(0x[0-9a-fA-F]+)\s+\S*\s+(.+?)(?:\s+<|\s+\(|$)"
)


@dataclass(frozen=True)
class MapEntry:
    start: int
    end: int
    perms: str
    file_offset: int
    path: str

    @property
    def basename(self) -> str:
        if not self.path:
            return ""
        if self.path.startswith("["):
            return self.path
        return os.path.basename(self.path.rstrip())

    @property
    def executable(self) -> bool:
        return "x" in (self.perms or "").lower()

    def contains(self, va: int) -> bool:
        return self.start <= va < self.end

    def file_offset_for_va(self, va: int) -> int:
        return (va - self.start) + self.file_offset


def extract_maps_blob(content: str) -> str:
    if not content:
        return ""
    m = _MAPS_SECTION_RE.search(content)
    if m:
        return m.group(1) or ""
    m = _BINARY_IMAGES_RE.search(content)
    if m:
        return m.group(1) or ""
    return ""


def parse_memory_maps(content: str) -> List[MapEntry]:
    """解析 Maps / memory map / Binary Images 为区间列表。"""
    blob = extract_maps_blob(content)
    if not blob.strip():
        return []

    entries: List[MapEntry] = []
    # Prefer Linux/Harmony/Android map lines
    for line in blob.splitlines():
        line = line.strip()
        if not line or line.startswith("---"):
            continue
        mm = _MAP_LINE_RE.match(line)
        if mm:
            start_s, end_s, perms, off_s, path = mm.groups()
            try:
                start = int(start_s, 16)
                end = int(end_s, 16)
                file_off = int(off_s, 16)
            except ValueError:
                continue
            path = (path or "").strip()
            # Android may leave trailing fields; strip device:inode if glued oddly
            if path and not path.startswith("/") and not path.startswith("["):
                # keep as-is (anon name etc.)
                pass
            entries.append(
                MapEntry(
                    start=start,
                    end=end,
                    perms=perms,
                    file_offset=file_off,
                    path=path,
                )
            )
            continue
        bm = _BINARY_IMAGE_LINE_RE.match(line)
        if bm:
            start_s, end_s, path = bm.groups()
            try:
                start = int(start_s, 16)
                end = int(end_s, 16)
            except ValueError:
                continue
            path = (path or "").strip()
            entries.append(
                MapEntry(
                    start=start,
                    end=end,
                    perms="r-x",
                    file_offset=0,
                    path=path,
                )
            )
    return entries


def lookup_va(entries: List[MapEntry], va: int) -> Optional[MapEntry]:
    if va < 0 or not entries:
        return None
    # Prefer executable mapping when overlapping (rare)
    hits = [e for e in entries if e.contains(va)]
    if not hits:
        return None
    for e in hits:
        if e.executable:
            return e
    return hits[0]


def classify_mapped_kind(entry: Optional[MapEntry], *, special_name: str = "") -> str:
    """返回 kind: code / stack / heap_or_anon / mapped_data / unmapped。"""
    if entry is None:
        return "unmapped"
    path_l = (entry.path or "").lower()
    name = special_name.lower()
    if name in ("sp", "rsp", "esp") or "[stack" in path_l:
        return "stack"
    if entry.executable:
        return "code"
    if path_l.startswith("[") or "anon" in path_l or "heap" in path_l:
        return "heap_or_anon"
    if entry.path:
        return "mapped_data"
    return "heap_or_anon"


def module_load_base(entry: MapEntry) -> int:
    """单段近似装载基址：mapping_start - file_offset（可能与 backtrace 相对 pc 差一页）。"""
    return max(0, entry.start - entry.file_offset)


def so_load_base(entries: List[MapEntry], hit: MapEntry) -> int:
    """与 Harmony/Android backtrace 相对 pc 对齐的 so 装载基址。

    优先取同一 path 下 ``file_offset==0`` 的最低映射 start（首个 PT_LOAD / r--）。
    若无 offset=0 段（maps 残缺只剩 r-xp），回退 ``start - file_offset``。
    """
    if not hit.path or hit.path.startswith("["):
        return max(0, hit.start - hit.file_offset)
    segs = [e for e in entries if e.path == hit.path]
    if not segs and hit.basename:
        segs = [
            e
            for e in entries
            if e.basename == hit.basename and e.path and not e.path.startswith("[")
        ]
    if not segs:
        return max(0, hit.start - hit.file_offset)
    zero_off = [e for e in segs if e.file_offset == 0]
    if zero_off:
        return min(e.start for e in zero_off)
    first = min(segs, key=lambda e: e.start)
    return max(0, first.start - first.file_offset)


def so_relative_offset(entries: List[MapEntry], hit: MapEntry, va: int) -> int:
    """addr2line / 栈帧同款：VA 相对 so 装载基址的偏移。"""
    return va - so_load_base(entries, hit)
