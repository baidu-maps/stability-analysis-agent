#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提取 tombstone Abort message，并识别分配器堆损坏 abort。"""

from __future__ import annotations

import re
from typing import Optional

_ABORT_MESSAGE_RE = re.compile(
    r"(?im)^\s*(?:Abort message|LastFatalMessage)\s*:\s*(.+?)\s*$",
)

_HEAP_ABORT_RE = re.compile(
    r"scudo|"
    r"jemalloc|"
    r"invalid chunk state|"
    r"double[-_ ]?free|"
    r"heap[-_ ]?(?:corrupt|error)|"
    r"debug[-_ ]?malloc|"
    r"fdsan|"
    r"use[-_ ]after[-_ ]free when deallocat|"
    r"corrupted (?:double[-_ ]?linked )?list|"
    r"malloc(?:_error)?(?:_break|):",
    re.I,
)

_MAIN_THREAD_NAMES = frozenset(
    {
        "main",
        "mainthread",
        "ui",
        "uithread",
        ".main",
    }
)


def extract_abort_message(content: str) -> str:
    """从崩溃日志提取 Abort message / LastFatalMessage 原文。"""
    if not content:
        return ""
    match = _ABORT_MESSAGE_RE.search(content)
    if not match:
        return ""
    return str(match.group(1) or "").strip().strip("'\"")


def is_heap_allocator_abort(*texts: Optional[str]) -> bool:
    """是否为 Scudo/jemalloc 等分配器在释放/校验时主动 abort。"""
    blob = "\n".join(str(item or "") for item in texts)
    if not blob.strip():
        return False
    return bool(_HEAP_ABORT_RE.search(blob))


def thread_type_from_name(name: Optional[str]) -> str:
    """根据线程名判断 main / background。"""
    raw = str(name or "").strip()
    if not raw:
        return "main"
    lowered = raw.lower().replace(" ", "")
    if lowered in _MAIN_THREAD_NAMES or lowered.endswith(".main"):
        return "main"
    return "background"
