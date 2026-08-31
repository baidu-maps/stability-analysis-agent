#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
调用方生命周期对照：按结构抽取，不依赖 Init/WalkDispatch 等词表。

- 转发成员：调用方体内 `m_x->崩溃方法(`
- 赋值切片：同类中 `m_x =` 的函数，及其一层调用者
- 自投递序言：`if (F(... Class::Method ...)) return`（嵌套括号）
"""

from __future__ import annotations

import re
from typing import Iterable, List, Sequence, Set

_ARROW_CALL_RE = re.compile(r"\b(m_[A-Za-z_]\w*)\s*->\s*([A-Za-z_~]\w*)\s*\(")


def body_after_open_brace(code: str) -> str:
    text = str(code or "")
    idx = text.find("{")
    return text[idx + 1 :] if idx >= 0 else text


def deref_members_from_text(text: str) -> List[str]:
    """`m_foo->` 解引用（崩溃行 / 函数体）。"""
    seen: List[str] = []
    for match in re.finditer(r"\b(m_[A-Za-z_]\w*)\s*->", str(text or "")):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return seen


def forwarding_members(func_code: str, crash_method: str) -> List[str]:
    """调用方体内指向崩溃方法名的成员指针，如 `m_render->UpdateFoo(`。"""
    method = str(crash_method or "").strip()
    body = body_after_open_brace(func_code)
    if not method:
        return []
    seen: List[str] = []
    for match in _ARROW_CALL_RE.finditer(body):
        member, callee = match.group(1), match.group(2)
        if callee == method and member not in seen:
            seen.append(member)
    return seen


def arrow_callees_of_member(func_code: str, member: str) -> List[str]:
    """函数体中 `member->Foo(` 的 Foo 列表（去重保序）。"""
    if not member:
        return []
    body = body_after_open_brace(func_code)
    pat = re.compile(rf"\b{re.escape(member)}\s*->\s*([A-Za-z_~]\w*)\s*\(")
    seen: List[str] = []
    for match in pat.finditer(body):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return seen


def member_is_assigned(func_code: str, member: str) -> bool:
    """是否对成员赋值（排除 == / != / `->`）。"""
    if not member:
        return False
    body = body_after_open_brace(func_code)
    if re.search(rf"\b{re.escape(member)}\s*=(?!=)", body):
        return True
    if re.search(rf"\b{re.escape(member)}\s*\.\s*reset\s*\(", body):
        return True
    return False


def body_calls_unqualified(func_code: str, callee: str) -> bool:
    """函数体是否调用 `callee(`（非限定名）。"""
    name = str(callee or "").strip()
    if not name or not re.match(r"^[A-Za-z_]\w*$", name):
        return False
    body = body_after_open_brace(func_code)
    return bool(re.search(rf"\b{re.escape(name)}\s*\(", body))


def if_return_conditions(func_code: str) -> List[str]:
    """收集 `if (cond) return` / `if (cond) { return` 的 cond（支持嵌套括号）。"""
    text = str(func_code or "")
    out: List[str] = []
    start = 0
    while True:
        match = re.search(r"\bif\s*\(", text[start:])
        if not match:
            break
        open_paren = start + match.end() - 1
        depth = 0
        k = open_paren
        close = -1
        while k < len(text):
            ch = text[k]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    close = k
                    break
            k += 1
        if close < 0:
            break
        rest = text[close + 1 : close + 96].lstrip()
        if rest.startswith("{"):
            rest = rest[1:].lstrip()
        if rest.startswith("return"):
            out.append(text[open_paren + 1 : close])
        start = open_paren + 1
    return out


def has_self_repost_prologue(func_code: str, class_name: str) -> bool:
    """
    是否存在「带本类限定名的 if-return」：
    `if (F(..., Class::Method, ...)) return;`
    不绑定具体投递 API 名。
    """
    cls = str(class_name or "").strip()
    if not cls:
        return False
    token = rf"\b{re.escape(cls)}::[A-Za-z_~]\w*"
    for cond in if_return_conditions(func_code):
        if re.search(token, cond):
            return True
    return False


def pick_lifecycle_contrast(
    *,
    caller_has_repost: bool,
    assign_names: Sequence[str],
    callers_of_assign: Sequence[str],
    hop_exemplars: Sequence[str],
    crash_writers: Sequence[str],
    names_with_repost: Iterable[str],
    cap: int,
) -> List[str]:
    """
    稳定挑选少量对照函数。
    优先赋值链；若调用方缺自投递且赋值链也没有，再补一个序言范例。
    """
    cap_n = max(1, min(int(cap or 3), 4))
    picked: List[str] = []
    seen: Set[str] = set()

    def _add(name: str) -> None:
        n = str(name or "").strip()
        if not n or n in seen or len(picked) >= cap_n:
            return
        seen.add(n)
        picked.append(n)

    for name in assign_names:
        _add(name)
    for name in callers_of_assign:
        _add(name)

    repost_set = {str(x).strip() for x in names_with_repost if str(x or "").strip()}
    picked_has_repost = any(n in repost_set for n in picked)
    if not caller_has_repost and not picked_has_repost:
        for name in hop_exemplars:
            if name in seen:
                continue
            _add(name)
            break

    for name in crash_writers:
        _add(name)
    return picked
