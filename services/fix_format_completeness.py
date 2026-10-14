#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修复代码格式门禁：禁止 class 骨架/臆造锁导致自动改码提取失败。"""

from __future__ import annotations

import re
from typing import List, Optional, Sequence, Set

from services.toctou_write_completeness import _extract_fix_oriented_text

_CODE_FENCE = re.compile(r"```(?:cpp|c\+\+|c|cc|cxx)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_CLASS_SKELETON = re.compile(r"^\s*(?:class|struct)\s+\w+", re.MULTILINE)
_ELLIPSIS_PLACEHOLDER = re.compile(
    r"(?://\s*\.\.\.|/\*\s*\.\.\.\s*\*/|\.\.\.\s*其他|其他成员|省略|其他逻辑保持不变|\[其他代码\])"
)
_INVENTED_SYNC_MEMBER = re.compile(r"\bm_\w*(?:Mutex|Lock|mutex|lock)\b")
_STANDALONE_METHOD = re.compile(
    r"(?:^|\n)\s*(?:[\w:*&<>,\s]+?\s+)?(?:~?\w+::[~]?\w+)\s*\([^;{]*\)\s*(?:const\s*)?\{",
    re.MULTILINE,
)


def _fix_fences(analysis: str) -> List[str]:
    """返回修复导向章节内的代码围栏内容。"""
    text = _extract_fix_oriented_text(analysis)
    if not text:
        return []
    return [str(block or "") for block in _CODE_FENCE.findall(text)]


def analysis_has_class_skeleton_fix_fence(analysis: str) -> bool:
    """修复章节是否含带占位的 class/struct 骨架（会阻断旧版 fast-path）。"""
    for body in _fix_fences(analysis):
        if not _CLASS_SKELETON.search(body):
            continue
        if _ELLIPSIS_PLACEHOLDER.search(body) or "其他成员" in body:
            return True
        # 纯类声明 + 新成员、无完整 Class::method，也视为骨架噪声
        if not _STANDALONE_METHOD.search(body) and "{" in body:
            return True
    return False


def invented_sync_members_in_fix(analysis: str, prompt: str) -> List[str]:
    """修复代码中引用但 prompt/上下文未出现的 m_*Mutex/m_*Lock。"""
    known: Set[str] = set(re.findall(r"\bm_\w+", str(prompt or "")))
    novel: List[str] = []
    seen: Set[str] = set()
    for body in _fix_fences(analysis):
        # 类骨架里的「新增互斥锁」声明也算臆造
        for name in _INVENTED_SYNC_MEMBER.findall(body):
            if name in known or name in seen:
                continue
            seen.add(name)
            novel.append(name)
    return novel


def should_revise_for_fix_format(*, prompt: str, analysis: str) -> Optional[str]:
    """若修复格式不合格需续轮，返回反馈文案；否则 None。"""
    text = str(analysis or "").strip()
    if not text:
        return None
    reasons: List[str] = []
    if analysis_has_class_skeleton_fix_fence(text):
        reasons.append(
            "检测到带占位（`// ...` /「其他成员」等）的 class/struct 骨架围栏；"
            "此类块不能用于自动替换，且会干扰提取。"
        )
    invented = invented_sync_members_in_fix(text, prompt)
    if invented:
        reasons.append(
            "修复代码引用了上下文未出现的同步成员：{}。".format(
                ", ".join(f"`{n}`" for n in invented[:8])
            )
        )
    if not reasons:
        return None
    return build_fix_format_revision_feedback(reasons, invented)


def build_fix_format_revision_feedback(
    reasons: Sequence[str],
    invented_members: Sequence[str] = (),
) -> str:
    """构造续轮反馈：要求去掉类骨架并禁止臆造锁。"""
    lines = [
        "上一轮终局修复格式不合格（自动改码提取门禁）：",
        *[f"- {r}" for r in reasons],
        "",
        "本轮必须输出最终分析，并遵守：",
        "- 「修复代码」只允许完整可替换的 `Class::method` / 析构函数定义，每个函数独立 ```cpp 围栏；",
        "- 新增锁时可以多一个只有两行的声明块：上一行是头文件已有成员，下一行是新成员；",
        "- **禁止**输出 class/struct 骨架或含 `// ...` /「其他成员」占位的类声明块；",
        "- 若「代码库领域知识」点名了锁，必须使用该名字，不要改用其它新名字，也不要因此放弃补丁；",
        "- 知识没有点名时，禁止臆造 `m_*Mutex` / `m_*Lock`，改为写入「无法生成完整修复代码/需人工处理」；",
        "- 必须使用「需要修改的函数」列表（反引号签名），并与修复代码围栏一一对应。",
    ]
    if invented_members:
        lines.append("")
        lines.append("需从可自动应用补丁中移除的臆造成员：")
        for name in invented_members:
            lines.append(f"- `{name}`")
    return "\n".join(lines)
