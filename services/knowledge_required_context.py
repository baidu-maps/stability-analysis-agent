#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""领域知识点名且出现在崩溃栈上的函数，首轮必须有完整函数体。"""

from __future__ import annotations

import re
from typing import Any, List, Sequence

_KNOWLEDGE_HEADER = "## 代码库领域知识"
_FUNC_SRC_HEADER = "## 函数源码"
_QUALIFIED_RE = re.compile(
    r"(?:`)?((?:[A-Z][A-Za-z0-9_]*)(?:::(?:~?[A-Za-z_][A-Za-z0-9_]*))+)\(?(?:`)?"
)
_SKIP_MARKERS = ("std::", "std::__", "operator", "shared_ptr", "unique_ptr")


def _section_after(text: str, header: str) -> str:
    """返回某个二级标题到下一个二级标题之间的正文。"""
    start = text.find(header)
    if start < 0:
        return ""
    rest = text[start + len(header):]
    nxt = re.search(r"\n## ", rest)
    return rest if nxt is None else rest[: nxt.start()]


def _looks_like_project_symbol(symbol: str) -> bool:
    """过滤标准库、allocator 与运算符。"""
    text = str(symbol or "").strip()
    if "::" not in text:
        return False
    lowered = text.lower()
    if any(marker in lowered for marker in _SKIP_MARKERS):
        return False
    tail = text.split("::")[-1]
    if tail.startswith("_"):
        return False
    return True


def missing_knowledge_stack_functions(prompt: str) -> List[str]:
    """知识章节点名、栈上出现、但「函数源码」没有完整体的工程函数。"""
    text = str(prompt or "")
    knowledge = _section_after(text, _KNOWLEDGE_HEADER)
    if not knowledge.strip():
        return []
    source = _section_after(text, _FUNC_SRC_HEADER)
    named: List[str] = []
    seen = set()
    for match in _QUALIFIED_RE.finditer(knowledge):
        symbol = match.group(1).strip()
        if not _looks_like_project_symbol(symbol) or symbol in seen:
            continue
        seen.add(symbol)
        named.append(symbol)
    missing: List[str] = []
    for symbol in named:
        if symbol not in text:
            continue
        short = symbol.split("::")[-1]
        if symbol in source or f"::{short}" in source:
            continue
        missing.append(symbol)
    return missing[:6]


def build_prefetch_requests(symbols: Sequence[str]) -> List[dict]:
    """把缺失符号编成 function 请求。"""
    requests: List[dict] = []
    for symbol in symbols:
        name = str(symbol or "").strip()
        if not name:
            continue
        requests.append(
            {
                "type": "function",
                "symbol": name,
                "reason": "领域知识点名且出现在崩溃栈，首轮必须有完整函数体",
                "priority": "critical",
                "expected_return_form": "function_definition",
            }
        )
    return requests


def _snippet_text(item: Any) -> str:
    """从 resolver 结果取出函数体；成功项可能只有 snippet 列表、没有 content。"""
    if not isinstance(item, dict):
        return ""
    snippet = item.get("snippet")
    if isinstance(snippet, list) and snippet:
        return "\n".join(str(line) for line in snippet)
    return str(item.get("content") or item.get("source_content") or "").strip()


def format_prefetched_function_source(item: Any) -> str:
    """把已定位的函数体编成「函数源码」块，直接写入首轮提示，避开 24k evidence 预算。"""
    if not isinstance(item, dict):
        return ""
    body = _snippet_text(item)
    if not body.strip():
        return ""
    request = item.get("request") if isinstance(item.get("request"), dict) else {}
    signature = str(
        item.get("function_signature") or request.get("symbol") or ""
    ).strip()
    if not signature:
        return ""
    file_path = str(item.get("file") or request.get("file") or "N/A").strip() or "N/A"
    return "\n".join(
        [
            f"#### 函数源码: {signature}",
            f"- 文件: {file_path}",
            "- 来源标签: 领域知识栈函数, 预取",
            "- 代码片段:",
            body,
            "",
        ]
    )


def insert_function_source_blocks(prompt: str, blocks: Sequence[str]) -> str:
    """把预取函数体插入「函数源码」节，避免再走会裁掉它的 build_prompt。"""
    text = str(prompt or "")
    usable = [str(block).strip() for block in blocks if str(block).strip()]
    if not usable:
        return text
    chunk = "\n\n".join(usable)
    start = text.find(_FUNC_SRC_HEADER)
    if start < 0:
        return text.rstrip() + "\n\n" + _FUNC_SRC_HEADER + "\n\n" + chunk + "\n"
    rest_start = start + len(_FUNC_SRC_HEADER)
    rest = text[rest_start:]
    nxt = re.search(r"\n## ", rest)
    if nxt is None:
        return text.rstrip() + "\n\n" + chunk + "\n"
    insert_at = rest_start + nxt.start()
    return text[:insert_at].rstrip() + "\n\n" + chunk + "\n" + text[insert_at:]


def inject_resolved_function_sources(prompt: str, resolved: Sequence[Any]) -> str:
    """把 resolve 成功的函数体插入提示词的「函数源码」。"""
    blocks = [format_prefetched_function_source(item) for item in resolved]
    return insert_function_source_blocks(prompt, blocks)


def prefetch_missing_knowledge_functions(
    context_engine: Any,
    *,
    stable_prompt: str,
    assembled_prompt: str,
) -> str:
    """在首轮模型调用前补齐知识要求的栈函数；失败时原样返回 assembled_prompt。"""
    prompt = assembled_prompt or stable_prompt
    missing = missing_knowledge_stack_functions(prompt)
    if not missing:
        return prompt
    requests = build_prefetch_requests(missing)
    if not requests:
        return prompt
    resolved = context_engine.resolve_requests(requests, round_index=0)
    injected = inject_resolved_function_sources(prompt, resolved)
    if injected == prompt:
        return prompt
    return injected
