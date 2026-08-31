#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""堆栈符号与 RTF 崩溃日志的轻量规范化工具。"""

from __future__ import annotations

import re
from typing import List, Optional, Sequence, Tuple

# iOS / 导出日志中常见的 C++ 限定名（可无括号）
_CPP_QUALIFIED_SYMBOL_RE = re.compile(
    r"(?:\b[A-Za-z_][\w:]*::)+\w+"
)


def looks_like_rtf(content: str) -> bool:
    head = (content or "")[:512].lstrip()
    if head.startswith("{\\rtf"):
        return True
    return bool(re.search(r"\\rtf\d*\b", head, re.IGNORECASE))


def rtf_to_plain_text(content: str) -> str:
    """
    将 RTF 粗略转为纯文本（无第三方依赖）。
    足够处理常见 crash.rtf 导出：控制字、段落换行、花括号组。
    """
    if not content or not looks_like_rtf(content):
        return content

    text = content.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), text)
    text = re.sub(r"\\par[d]?\b", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"\\line\b", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"\\tab\b", "\t", text, flags=re.IGNORECASE)

    # 仅去掉 RTF 目的地组（字体表/颜色表等），保留正文组
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r"\{\\\*[^{}]*\}", "", text)

    text = re.sub(r"\\[a-z]+-?\d*\s?", "", text, flags=re.IGNORECASE)
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        text = text[1:-1].strip()
    text = text.replace("{", "").replace("}", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _owner_class_token(owner: str) -> str:
    """owner 末段类名，去掉返回类型（如 'VVoid NaviMapRender' -> 'NaviMapRender'）。"""
    last = (owner or "").split("::")[-1].strip()
    toks = last.split()
    return toks[-1] if toks else last


def cpp_owner_and_method(resolved_function: Optional[str]) -> Optional[Tuple[str, str]]:
    """
    从 C++ 符号提取 (owner, method)。
    例：walk_navi::NaviMapRender::UpdateFoo(...) -> (walk_navi::NaviMapRender, UpdateFoo)
    """
    s = sanitize_stack_symbol(resolved_function) or ""
    s = s.strip()
    if "::" not in s:
        return None
    head = s.split("(", 1)[0].strip()
    if "::" not in head:
        return None
    # 签名行可能带返回类型：VVoid NaviMapRender::Foo
    pre, rest = head.split("::", 1)
    pre = pre.strip()
    if " " in pre:
        pre = pre.split()[-1]
        head = f"{pre}::{rest}"
    parts = [p.strip() for p in head.split("::") if p.strip()]
    if len(parts) < 2:
        return None
    owner = "::".join(parts[:-1]).strip()
    method = parts[-1].strip()
    if not owner or not method:
        return None
    return owner, method


def stack_symbol_aliases_crash_function(
    resolved_function: str,
    crash_simple_name: str,
    *crash_symbols: str,
) -> bool:
    """
    栈帧符号是否应复用崩溃函数图节点。

    同名成员函数挂在不同类上时必须拆开，否则会把
    WalkMapControl::UpdateFoo 折叠成 NaviMapRender::UpdateFoo。
    崩溃点源码签名若只有 simple name（无 Class::），不得仅凭同名折叠其它类。
    """
    simple = (crash_simple_name or "").strip()
    if not simple:
        return False
    parsed = cpp_owner_and_method(resolved_function)
    method = parsed[1] if parsed else ""
    if not method:
        head = (resolved_function or "").split("(", 1)[0].strip()
        method = head.split("::")[-1].strip() if head else ""
        method = method.split()[-1] if method.split() else method
    if method != simple:
        return False
    crash_parsed = None
    for sym in crash_symbols:
        if not sym:
            continue
        crash_parsed = cpp_owner_and_method(sym)
        if crash_parsed:
            break
    if parsed and crash_parsed:
        return _owner_class_token(parsed[0]) == _owner_class_token(crash_parsed[0])
    if parsed and not crash_parsed:
        return False
    return True


def caller_is_same_cpp_method(
    caller_name: str,
    target_simple_name: str,
    crash_function_name: str,
) -> bool:
    """
    反向搜调用方时，是否应跳过「崩溃函数自身」。

    同 simple name 但类不同（薄封装/同名转发）应保留为调用方。
    """
    simple = (target_simple_name or "").strip()
    if not simple or not (caller_name or "").strip():
        return False
    caller_parsed = cpp_owner_and_method(caller_name)
    crash_parsed = cpp_owner_and_method(crash_function_name)
    caller_method = caller_parsed[1] if caller_parsed else caller_name.split("::")[-1].strip()
    if caller_method != simple:
        return False
    if caller_parsed and crash_parsed:
        return _owner_class_token(caller_parsed[0]) == _owner_class_token(crash_parsed[0])
    # 无类限定时退回旧行为：simple name 相同视为自身
    return not caller_parsed


def stack_has_same_named_trampoline(symbols: Sequence[str]) -> bool:
    """
    栈符号列表（崩溃帧在前）是否存在「不同类、同方法名」的转发调用方。
    例如 NaviMapRender::UpdateFoo ← WalkMapControl::UpdateFoo。
    """
    cleaned = [str(s).strip() for s in (symbols or []) if str(s or "").strip()]
    if len(cleaned) < 2:
        return False
    crash = cpp_owner_and_method(cleaned[0])
    if not crash:
        return False
    crash_owner, crash_method = crash
    crash_tail = _owner_class_token(crash_owner)
    if not crash_method or not crash_tail:
        return False
    for other in cleaned[1:]:
        parsed = cpp_owner_and_method(other)
        if not parsed:
            continue
        o_owner, o_method = parsed
        if o_method == crash_method and _owner_class_token(o_owner) != crash_tail:
            return True
    return False


def sanitize_stack_symbol(symbol: Optional[str]) -> Optional[str]:
    """
    清洗堆栈符号中的 RTF/导出残留。
    例：walk_navi::CRoute::GetLegSize\\ -> walk_navi::CRoute::GetLegSize
    """
    if symbol is None:
        return None
    if not isinstance(symbol, str):
        return symbol
    s = symbol.strip()
    if not s:
        return symbol

    # RTF 行尾续行反斜杠
    while s.endswith("\\"):
        s = s[:-1].rstrip()
    #  stray 花括号/反斜杠（如 _pthread_start}）
    s = re.sub(r"[\\{}]+$", "", s).strip()
    return s if s else symbol


def is_objc_stack_symbol(symbol: Optional[str]) -> bool:
    """ObjC 方法符号：-[Class sel:] / +[Class sel:]"""
    s = sanitize_stack_symbol(symbol) or ""
    s = s.strip()
    if not s:
        return False
    return bool(re.match(r"^[\-\+]\[[^\]]+\]$", s))


def is_cpp_native_stack_symbol(resolved_function: Optional[str]) -> bool:
    """
    是否参与 native C++ 崩溃分析（与 code_content_provider 策略一致）。
    保留 Itanium mangled、C++ 限定名、带括号的 C 风格签名；排除 ObjC。
    """
    s = sanitize_stack_symbol(resolved_function) or ""
    s = s.strip()
    if not s:
        return False
    if is_objc_stack_symbol(s):
        return False
    if s.startswith("_Z"):
        return True
    if "::" in s:
        head = s.split("(", 1)[0].strip()
        parts = [p.strip() for p in head.split("::") if p.strip()]
        if len(parts) >= 2 and re.search(r"[A-Za-z_~]", parts[-1]):
            return True
    if "(" in s and ")" in s:
        head = s.split("(", 1)[0].strip()
        if head and re.search(r"[A-Za-z_~]", head):
            return True
    return False


def frame_analysis_symbol(frame: Dict[str, Any]) -> str:
    """从 02 帧中取用于 native C++ 判定的符号名。"""
    for key in ("resolved_function", "function"):
        val = frame.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def should_keep_frame_for_cpp_stack_output(frame: Dict[str, Any]) -> bool:
    """
    02 输出保留策略：仅保留带可读 C++ native 符号的帧。
    无 function/resolved_function 的空帧（含仅地址的 AGXMetal 崩溃点）不写入 02，避免干扰 03。
    崩溃地址仍保留在 01 的 crash_info 中供摘要使用。
    """
    sym = frame_analysis_symbol(frame)
    if not sym:
        return False
    return is_cpp_native_stack_symbol(sym)


def looks_like_cpp_qualified_stack(content: str) -> bool:
    """启发式：内容是否像已符号化的 C++ iOS 堆栈片段。"""
    if not content:
        return False
    if not _CPP_QUALIFIED_SYMBOL_RE.search(content):
        return False
    if not re.search(r"\b[0-9a-fA-F]{8,16}\b", content):
        return False
    markers = (
        r"Thread\s+\d+",
        r"Backtrace",
        r"Crashed",
        r"SIGSEGV",
        r"EXC_",
        r"null pointer",
        r"Last Exception",
    )
    return any(re.search(p, content, re.IGNORECASE) for p in markers)
