#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Apple iOS 线程栈块裁剪。"""

from __future__ import annotations

import re
from typing import Optional, Tuple

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
