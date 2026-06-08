# -*- coding: utf-8 -*-
"""函数片段/签名合法性判断（排除 if/else if 等控制流误命中）。"""

from __future__ import annotations

import re
from typing import Optional

_CONTROL_FLOW_LINE_RE = re.compile(
    r"^(else\s+if|if|for|while|switch|case|default|do|return|try|catch|throw|goto|break|continue)\b"
)

_CONTROL_FLOW_NAME_KEYWORDS = frozenset(
    {
        "if",
        "else",
        "for",
        "while",
        "switch",
        "case",
        "do",
        "return",
        "try",
        "catch",
        "throw",
        "goto",
        "break",
        "continue",
        "default",
    }
)


def strip_leading_close_braces(line: str) -> str:
    """去掉行首 ``}``，便于识别 ``} else if (...)`` 类控制流。"""
    s = str(line or "").lstrip()
    while s.startswith("}"):
        s = s[1:].lstrip()
    return s


def is_control_flow_source_line(line: str) -> bool:
    """当前源码行是否为控制流语句（非函数定义）。"""
    return bool(_CONTROL_FLOW_LINE_RE.match(strip_leading_close_braces(line)))


def is_plausible_function_name(name: Optional[str]) -> bool:
    n = str(name or "").strip()
    if not n:
        return False
    return n not in _CONTROL_FLOW_NAME_KEYWORDS


def is_plausible_function_signature(sig: Optional[str]) -> bool:
    """05/graph：签名不得为控制流片段或明显语句级误命中。"""
    s = strip_leading_close_braces(str(sig or "").strip())
    if not s:
        return False
    if is_control_flow_source_line(s):
        return False
    if re.match(r"^(if|for|while|switch|catch)\b", s):
        return False
    if re.match(r"^\s*(?:template\s*<[^>]+>\s*)?(?:struct|class)\s+\w+", s):
        return True
    if ";" in s and "(" in s and ")" in s and "::" not in s:
        return False
    if "(" not in s or ")" not in s:
        return False
    return True
