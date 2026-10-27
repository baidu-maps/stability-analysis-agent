#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提取 tombstone Abort message，并识别分配器堆损坏 abort。"""

from __future__ import annotations

import re
from typing import Optional

_ABORT_MESSAGE_RE = re.compile(
    r"(?im)^\s*(?:Abort message|LastFatalMessage)\s*:\s*(.+?)\s*$",
)

# Apple translated report: ``Termination Reason: Namespace FRONTBOARD, Code 2343432205,``
_APPLE_TERMINATION_RE = re.compile(
    r"(?im)^Termination Reason:\s*Namespace\s+([A-Za-z0-9_]+)\s*,\s*Code\s+(\d+)\s*,?\s*$",
)
_APPLE_TERM_STOP_RE = re.compile(
    r"^(?:ProcessVisibility|ProcessState|Watchdog|ThermalInfo|Kernel Triage|Thread\s+\d+)\b",
    re.IGNORECASE,
)
_WATCHDOG_EVENT_RE = re.compile(r"(?im)^WatchdogEvent:\s*(\S+)")
_APPLE_WATCHDOG_RE = re.compile(
    r"Failed to terminate gracefully|WatchdogEvent\s*:|Namespace\s+FRONTBOARD",
    re.IGNORECASE,
)
_APPLE_JETSAM_RE = re.compile(
    r"\bjetsam\b|Namespace\s+MEMORYSTATUS|per-process-limit",
    re.IGNORECASE,
)

_DESTROYED_MUTEX_RE = re.compile(
    r"pthread_mutex_destroy called on a destroyed mutex",
    re.I,
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
    """从崩溃日志提取 Abort message / LastFatalMessage，或 Apple Termination Reason。"""
    if not content:
        return ""
    match = _ABORT_MESSAGE_RE.search(content)
    if match:
        return str(match.group(1) or "").strip().strip("'\"")
    return extract_apple_termination_message(content)


def extract_apple_termination_message(content: str) -> str:
    """提取 Apple ``Termination Reason`` 与紧随的原因行、WatchdogEvent。"""
    if not content:
        return ""
    match = _APPLE_TERMINATION_RE.search(content)
    if not match:
        return ""
    namespace = match.group(1)
    code = match.group(2)
    reason_bits = []
    for line in content[match.end():].splitlines():
        stripped = line.strip()
        if not stripped:
            if reason_bits:
                break
            continue
        if _APPLE_TERM_STOP_RE.match(stripped):
            break
        cleaned = re.sub(r"^:\d+\]\s*", "", stripped)
        if cleaned:
            reason_bits.append(cleaned)
        if len(reason_bits) >= 2:
            break
    text = f"Namespace {namespace}, Code {code}"
    if reason_bits:
        text = f"{text}: {'; '.join(reason_bits)}"
    event = _WATCHDOG_EVENT_RE.search(content)
    if event:
        text = f"{text} (WatchdogEvent: {event.group(1)})"
    return text


def is_apple_watchdog_termination(*texts: Optional[str]) -> bool:
    """是否为前台退出看门狗（FRONTBOARD / Failed to terminate / WatchdogEvent）。"""
    blob = "\n".join(str(item or "") for item in texts)
    return bool(blob.strip() and _APPLE_WATCHDOG_RE.search(blob))


def is_apple_jetsam_termination(*texts: Optional[str]) -> bool:
    """是否为 jetsam / MEMORYSTATUS 内存杀进程。"""
    blob = "\n".join(str(item or "") for item in texts)
    return bool(blob.strip() and _APPLE_JETSAM_RE.search(blob))


def is_destroyed_mutex_abort(*texts: Optional[str]) -> bool:
    """是否为 Bionic FORTIFY 对已销毁 mutex 再次 destroy。"""
    blob = "\n".join(str(item or "") for item in texts)
    if not blob.strip():
        return False
    return bool(_DESTROYED_MUTEX_RE.search(blob))


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
