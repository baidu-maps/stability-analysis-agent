#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""各平台栈行解析（按行）。"""

from __future__ import annotations

import re
from typing import Optional, Tuple

# 已符号化 iOS 导出：``0 0 Module 0xADDR symbol``（双序号，第二列为重复帧号）
_IOS_PREPARSED_STACK_RE = re.compile(
    r"^\s*(\d+)\s+\d+\s+(\S+)\s+(0x[0-9a-fA-F]+)\s+(.+)$"
)
_IOS_UUID_TAIL_RE = re.compile(
    r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}(?:\s+\+\s+(\d+))?\s*$",
    re.IGNORECASE,
)


def _try_parse_ios_pre_parsed_stack_line(line: str) -> Optional[Tuple[str, str, str, str, int]]:
    """
    解析已符号化 iOS 精简栈行。返回 (module, address, function, offset, frame_number) 或 None。
    """
    m = _IOS_PREPARSED_STACK_RE.match(line)
    if not m:
        return None
    frame_num = int(m.group(1))
    module, addr, rest = m.group(2), m.group(3), m.group(4).strip()
    rest_st = rest.strip()
    if _IOS_UUID_TAIL_RE.match(rest_st):
        mu = _IOS_UUID_TAIL_RE.match(rest_st)
        offset = mu.group(1) if mu and mu.group(1) else "0"
        return module, addr, "", offset, frame_num
    mx = re.search(r"\s+\+\s+(\d+)(\s*\*\(.+\))?\s*$", rest_st)
    if mx:
        func = rest_st[: mx.start()].strip()
        return module, addr, func, mx.group(1), frame_num
    mx2 = re.match(r"^(.+?)\s+\+\s+(\d+)\s*$", rest_st)
    if mx2:
        return module, addr, mx2.group(1).strip(), mx2.group(2), frame_num
    return module, addr, rest_st, "0", frame_num

# Apple iOS/macOS .crash：「序号 模块 地址 符号 + 偏移」，且 C++ 可出现「+ 0 *(…)」等尾部
_IOS_STACK_PREFIX_RE = re.compile(r"^\s*\d+\s+(\S+)\s+(0x[0-9a-fA-F]+)\s+(.+)$")


def _try_parse_ios_macos_stack_line(line: str) -> Optional[Tuple[str, str, str, str]]:
    """
    解析 Xcode/iOS 风格栈行。返回 (module, address, function, offset) 或 None。
    支持符号尾部为「+ N」或「+ N *(…)」（如 demangled 模板/内联补充）。
    支持「模块 0x基址 + 仅偏移」（无符号名，rest 仅为「+ N」）。
    """
    m = _IOS_STACK_PREFIX_RE.match(line)
    if not m:
        return None
    module, addr, rest = m.group(1), m.group(2), m.group(3)
    rest_st = rest.strip()
    if re.match(r"^\+\s*\d+\s*$", rest_st):
        mrel = re.match(r"^\+\s*(\d+)\s*$", rest_st)
        if mrel:
            return module, addr, "", mrel.group(1)
    # 自右向左匹配最后一个「 + 数字」段（避免函数体内出现「 + 」误截断）
    mx = re.search(r"\s+\+\s+(\d+)(\s*\*\(.+\))?\s*$", rest)
    if mx:
        func = rest[: mx.start()].strip()
        return module, addr, func, mx.group(1)
    mx2 = re.match(r"^(.+?)\s+\+\s+(\d+)\s*$", rest)
    if mx2:
        return module, addr, mx2.group(1).strip(), mx2.group(2)
    return module, addr, rest.strip(), "0"


# 无指令地址、仅「序号 模块  符号 + 偏移」的符号化栈（导出/裁剪后常见）
_IOS_STACK_SYMBOL_ONLY_RE = re.compile(r"^\s*\d+\s+(\S+)\s+(.+)$")


def _try_parse_ios_symbol_only_stack_line(line: str) -> Optional[Tuple[str, str, str, str]]:
    """
    解析不含 0x PC 的 Apple 风格栈行。address 无则空串。
    """
    m = _IOS_STACK_SYMBOL_ONLY_RE.match(line)
    if not m:
        return None
    # Android tombstone「memory near」行：首列为 8–16 位十六进制地址，易被误当成「帧号 + 模块」
    mhead = re.match(r"^\s*(\d+)\s+", line)
    if mhead and len(mhead.group(1)) >= 8:
        return None
    module, rest = m.group(1), m.group(2).strip()
    # 「13 total frames」等统计行
    if module.lower() == "total" and rest.lower().startswith("frames"):
        return None
    if rest.startswith("0x"):
        return None
    mx = re.search(r"\s+\+\s+(\d+)(\s*\*\(.+\))?\s*$", rest)
    if mx:
        func = rest[: mx.start()].strip()
        return module, "", func, mx.group(1)
    mx2 = re.match(r"^(.+?)\s+\+\s+(\d+)\s*$", rest)
    if mx2:
        return module, "", mx2.group(1).strip(), mx2.group(2)
    return module, "", rest, "0"
