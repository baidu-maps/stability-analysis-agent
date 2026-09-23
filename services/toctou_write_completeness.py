#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TOCTOU 写路径终局完整性检查：prompt 已给写体时，禁止只交读侧补丁就收工。"""

from __future__ import annotations

import re
from typing import List, NamedTuple, Sequence

_WRITE_KEEP_SECTION = "### 写路径函数源码（压缩保活"
_WRITE_FUNC_HEADER = re.compile(
    r"^#{2,4}\s*写路径函数源码:\s*(.+?)\s*$",
    re.MULTILINE,
)
_COMPRESSED_HINT = re.compile(
    r"完整性:\s*压缩片段|write-path body truncated|\.\.\.\[snip\]\.\.\.",
    re.IGNORECASE,
)
_COMPLETE_HINT = re.compile(r"完整性:\s*完整函数体")
_RACE_HINT = re.compile(
    r"(竞态|TOCTOU|check\s*-?\s*then\s*-?\s*use|置空|并发改写|共享\s*(指针|状态|变量|成员)|race\s*condition)",
    re.IGNORECASE,
)
_CODE_FENCE = re.compile(r"```(?:cpp|c\+\+|c)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_SYNC_IN_FIX = re.compile(
    r"\b(Lock|Unlock|TryLock|Mutex|mutex|scoped_lock|unique_lock|lock_guard)\b"
)
# 仅在修复导向章节内计完整函数体，避免把根因证据里的 cpp 块误当成已提交补丁。
_FIX_SECTION_MARKERS = (
    "## 修复代码",
    "### 修复代码",
    "## 需要修改的函数",
    "### 需要修改的函数",
    "需要修改的函数",
    "### 6.1",
    "## 6. 修复建议",
    "## 6.",
    "直接修复（代码级）",
    "直接修复",
)


class WritePathKeepaliveEntry(NamedTuple):
    """写路径保活条目：签名 + 是否为完整可替换函数体。"""

    signature: str
    is_complete: bool


def short_function_name(signature: str) -> str:
    """从签名中提取短函数名（含可选 Class::）。"""
    text = str(signature or "").strip()
    if not text:
        return ""
    # 去掉尾部参数列表起点之后的内容，便于匹配
    head = text.split("(", 1)[0].strip()
    # 去掉返回类型：取最后一个词或 Class::Method
    if "::" in head:
        # e.g. "void DemoControl::HandleClearItem"
        parts = head.split()
        for part in reversed(parts):
            if "::" in part:
                return part.strip()
        return parts[-1].strip() if parts else head
    parts = head.split()
    return parts[-1].strip() if parts else head


def _write_keepalive_section(prompt: str) -> str:
    """截取写路径压缩保活小节文本。"""
    text = str(prompt or "")
    start = text.find(_WRITE_KEEP_SECTION)
    if start < 0:
        return ""
    rest = text[start:]
    end_markers = ("\n# 崩溃分析任务", "\n## 分析指导", "\n## 输出格式")
    end = len(rest)
    for marker in end_markers:
        pos = rest.find(marker)
        if pos >= 0:
            end = min(end, pos)
    return rest[:end]


def list_write_path_keepalive_entries(prompt: str) -> List[WritePathKeepaliveEntry]:
    """解析写路径保活条目，并标注是否为完整函数体。"""
    section = _write_keepalive_section(prompt)
    if not section:
        return []
    matches = list(_WRITE_FUNC_HEADER.finditer(section))
    out: List[WritePathKeepaliveEntry] = []
    seen = set()
    for idx, match in enumerate(matches):
        sig = str(match.group(1) or "").strip()
        if not sig:
            continue
        key = short_function_name(sig) or sig
        if key in seen:
            continue
        seen.add(key)
        block_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(section)
        block = section[match.start() : block_end]
        if _COMPRESSED_HINT.search(block):
            is_complete = False
        elif _COMPLETE_HINT.search(block):
            is_complete = True
        else:
            # 兼容旧 prompt：无完整性字段时，出现截断标记则视为不完整。
            is_complete = not bool(
                re.search(r"\.\.\.\[(?:snip|write-path body truncated)", block, re.I)
            )
        out.append(WritePathKeepaliveEntry(signature=sig, is_complete=is_complete))
    return out


def list_write_path_keepalive_signatures(prompt: str) -> List[str]:
    """从压缩保活段解析写路径函数签名（按出现顺序去重；含压缩片段）。"""
    return [entry.signature for entry in list_write_path_keepalive_entries(prompt)]


def list_complete_write_path_keepalive_signatures(prompt: str) -> List[str]:
    """仅返回标注为完整函数体的写路径签名（可用于强制补齐落盘修复）。"""
    return [
        entry.signature
        for entry in list_write_path_keepalive_entries(prompt)
        if entry.is_complete
    ]


def analysis_claims_shared_state_race(analysis: str) -> bool:
    """分析文本是否主张共享状态竞态 / TOCTOU 类根因。"""
    return bool(_RACE_HINT.search(str(analysis or "")))


def _extract_fix_oriented_text(analysis: str) -> str:
    """截取修复建议/修复代码等章节之后的文本；无此类章节则返回空串。"""
    text = str(analysis or "")
    starts = [text.find(marker) for marker in _FIX_SECTION_MARKERS]
    starts = [pos for pos in starts if pos >= 0]
    if not starts:
        return ""
    return text[min(starts) :]


def _analysis_has_complete_fix_block(analysis: str, signature: str) -> bool:
    """修复章节中是否已有对应该写路径的完整可替换函数体（须含同步原语）。"""
    short = short_function_name(signature)
    if not short:
        return False
    leaf = short.split("::")[-1]
    text = _extract_fix_oriented_text(analysis)
    if not text:
        return False
    for block in _CODE_FENCE.findall(text):
        body = str(block or "")
        if leaf not in body and short not in body:
            continue
        # 需要像函数定义：含短名 + '(' + '{'
        if re.search(rf"{re.escape(leaf)}\s*\(", body) and "{" in body and "}" in body:
            # 排除仅引用片段 / 省略号占位
            if re.search(r"\b省略\b|\.\.\.\s*$|/\*\s*\.\.\.|//\s*\.\.\.", body):
                continue
            # 写路径补丁必须带同步；裸拷贝未加锁原文不算“已修复”
            if not _SYNC_IN_FIX.search(body):
                continue
            # 至少若干实质行
            code_lines = [
                ln for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("//")
            ]
            if len(code_lines) >= 4:
                return True
    return False


def missing_write_path_fix_signatures(
    analysis: str,
    write_signatures: Sequence[str],
) -> List[str]:
    """返回分析中尚未给出完整修复函数体的写路径签名。"""
    missing: List[str] = []
    for sig in write_signatures:
        if not _analysis_has_complete_fix_block(analysis, sig):
            missing.append(str(sig))
    return missing


def build_write_path_revision_feedback(missing_signatures: Sequence[str]) -> str:
    """构造续轮反馈：要求补齐写路径完整修复代码。"""
    lines = [
        "上一轮终局修复不完整（写路径完整性门禁）：",
        "上文「写路径函数源码（压缩保活）」已给出**完整**写路径函数体，且分析已指向共享状态竞态/TOCTOU，",
        "但修复代码未包含下列写路径函数的完整可替换定义。",
        "本轮必须输出最终分析，并将读路径与下列写路径一并列入需要修改的函数，",
        "各自给出完整 ```cpp 函数体；同步保护必须使用上文已出现的成员（如 m_*Mutex），禁止臆造新锁。",
        "禁止只改读侧却声称竞态已彻底修复。",
        "若某写路径在上文仅为「压缩片段」，不得臆造整函数落盘，应改为调用方加锁或「需人工处理」。",
        "",
        "缺少完整修复代码的写路径：",
    ]
    for sig in missing_signatures:
        lines.append(f"- `{sig}`")
    return "\n".join(lines)


def should_revise_for_incomplete_write_fixes(
    *,
    prompt: str,
    analysis: str,
) -> List[str]:
    """若应续轮补写路径，返回缺失签名列表；否则返回空列表。

    仅对「完整性: 完整函数体」的写路径强制补齐；压缩片段不触发整函数落盘续轮。
    """
    write_sigs = list_complete_write_path_keepalive_signatures(prompt)
    if not write_sigs:
        return []
    if not analysis_claims_shared_state_race(analysis):
        return []
    return missing_write_path_fix_signatures(analysis, write_sigs)
