#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Apple iOS 线程栈块裁剪。"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

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


_IOS_COLON_THREAD_RE = re.compile(r"^Thread:\s*(\S.*?)\s*$", re.IGNORECASE)


def split_ios_colon_named_threads(content: str) -> Optional[List[Tuple[str, str, int]]]:
    """按 ``Thread: 名字`` 拆缩写栈。返回 (name, block, start_line) 列表。

    第一个标题之前的正文是崩溃栈，name 为空。没有这种标题时返回 None。
    ``Thread 0:`` 这类带序号的 Apple 线程头不在此列。
    """
    lines = content.splitlines()
    headers = []
    for index, line in enumerate(lines):
        match = _IOS_COLON_THREAD_RE.match(line.strip())
        if not match:
            continue
        name = match.group(1).strip()
        if re.match(r"^\d+\b", name):
            continue
        headers.append((index, name))
    if not headers:
        return None
    blocks = []
    first_header = headers[0][0]
    if first_header > 0:
        blocks.append(("", "\n".join(lines[:first_header]), 1))
    for position, (index, name) in enumerate(headers):
        end = headers[position + 1][0] if position + 1 < len(headers) else len(lines)
        blocks.append((name, "\n".join(lines[index:end]), index + 1))
    return blocks


def _ios_extract_crashed_thread_index(content: str) -> Optional[int]:
    """提取 Apple 报告中的崩溃线程序号（Thread N 中的 N）。"""
    m = re.search(r"^\s*Crashed Thread:\s*(\d+)\s*$", content, re.MULTILINE)
    if m:
        return int(m.group(1))
    m = re.search(r"(?m)^Thread\s+(\d+)\s+Crashed:\s*$", content)
    if m:
        return int(m.group(1))
    m = re.search(r"(?m)^Triggered by Thread:\s*(\d+)\b", content)
    if m:
        return int(m.group(1))
    dm = re.search(r"(?m)^Thread\s+(\d+)\s+Deadlock:\s*$", content)
    if dm:
        return int(dm.group(1))
    return None


_IOS_THREAD_NAME_RE = re.compile(
    r"(?m)^Thread\s+(\d+)\s+name:\s*(.+?)\s*$",
    re.IGNORECASE,
)
_IOS_TRIGGERED_THREAD_RE = re.compile(
    r"(?m)^Triggered by Thread:\s*(\d+)\s+(.+?)\s*$",
    re.IGNORECASE,
)
_IOS_QUEUE_SPLIT_RE = re.compile(r"\s*,?\s*Dispatch queue:\s*", re.IGNORECASE)


def _split_apple_thread_name(raw: str) -> tuple:
    """拆开 ``名字 Dispatch queue: ...``，返回 (name, queue)。"""
    parts = _IOS_QUEUE_SPLIT_RE.split(raw.strip(), maxsplit=1)
    name = parts[0].strip(" ,")
    queue = parts[1].strip() if len(parts) > 1 else ""
    return name, queue


def _apple_is_main_queue(name: str, queue: str) -> Optional[bool]:
    """队列含 main-thread 时为主线程；没有队列信息则返回 None。"""
    if queue:
        return "main-thread" in queue.lower()
    if name.lower() in {"main", "main-thread", "ui"}:
        return True
    return None


def apple_crash_thread_identity(content: str, thread_index: Optional[int]):
    """返回崩溃线程的 (name, is_main)。

    优先 ``Thread N name:``，否则用 ``Triggered by Thread:``。
    """
    named = []
    for match in _IOS_THREAD_NAME_RE.finditer(content or ""):
        index = int(match.group(1))
        name, queue = _split_apple_thread_name(match.group(2))
        if name:
            named.append((index, name, queue))
    chosen = None
    if thread_index is not None:
        chosen = next((item for item in named if item[0] == thread_index), None)
    if chosen is None and named:
        chosen = named[0]
    if chosen is not None:
        _index, name, queue = chosen
        return name, _apple_is_main_queue(name, queue)
    triggered = _IOS_TRIGGERED_THREAD_RE.search(content or "")
    if not triggered:
        return None, None
    if thread_index is not None and int(triggered.group(1)) != thread_index:
        return None, None
    name, queue = _split_apple_thread_name(triggered.group(2))
    if not name:
        return None, None
    return name, _apple_is_main_queue(name, queue)
