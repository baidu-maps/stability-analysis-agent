#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gate B：结论完备性检查——可开跑之后、调用大模型之前。

Gate A（材料/解析/符号化/源码可用）只决定能否进入分析流水线；
Gate B 决定证据是否足以主张主根因或自动改码。不满足时提前终止 LLM，
并给出需补充的材料清单，避免在缺口上编造完整故事（如缺系统版本却断言 iOS 15 API）。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# 崩溃相关源码里出现这些标记时，缺少 os_version 不得下「版本相关 API」结论。
_VERSION_GATED_MARKERS = (
    "@available",
    "API_AVAILABLE",
    "__builtin_available",
    "CAFrameRateRangeMake",
    "preferredFrameRateRange",
    "CAFrameRateRange",
    "@available(iOS",
    "@available(macos",
    "TARGET_OS_IOS",
)

_OS_FIELD = "系统版本（OS Version）"
_LOG_FIELD = "崩溃日志"


def _meta(parse_result: Any) -> Dict[str, Any]:
    """取出 parse_result.meta_info。"""
    if not isinstance(parse_result, dict):
        return {}
    meta = parse_result.get("meta_info")
    return meta if isinstance(meta, dict) else {}


def extract_os_version(parse_result: Any) -> str:
    """返回已解析的系统版本字符串；没有则空串。"""
    meta = _meta(parse_result)
    return str(meta.get("os_version") or "").strip()


def extract_os_type(parse_result: Any) -> str:
    """返回 os_type（小写）。"""
    meta = _meta(parse_result)
    for key in ("os_type", "platform"):
        text = str(meta.get(key) or "").strip().lower()
        if text:
            return text
    if isinstance(parse_result, dict):
        return str(parse_result.get("os_type") or "").strip().lower()
    return ""


def _collect_code_text(code_context: Any, *, limit: int = 200_000) -> str:
    """从 code_context / graph nodes 抽取可读源码文本，供版本门控检测。"""
    if not isinstance(code_context, dict):
        return ""
    chunks: List[str] = []
    graph = code_context.get("graph") if isinstance(code_context.get("graph"), dict) else {}
    nodes = graph.get("nodes") if isinstance(graph, dict) else None
    if isinstance(nodes, list):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            snippet = node.get("snippet")
            if isinstance(snippet, list):
                chunks.append("\n".join(str(line) for line in snippet))
            elif isinstance(snippet, str) and snippet.strip():
                chunks.append(snippet)
            for key in ("code", "body", "text", "signature"):
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    chunks.append(value)
    for key in ("functions", "snippets", "code_snippets"):
        blob = code_context.get(key)
        if isinstance(blob, list):
            for item in blob:
                if isinstance(item, dict):
                    for field in ("code", "snippet", "body", "text"):
                        value = item.get(field)
                        if isinstance(value, list):
                            chunks.append("\n".join(str(line) for line in value))
                        elif isinstance(value, str) and value.strip():
                            chunks.append(value)
                elif isinstance(item, str) and item.strip():
                    chunks.append(item)
        elif isinstance(blob, str) and blob.strip():
            chunks.append(blob)
    text = "\n".join(chunks)
    return text if len(text) <= limit else text[:limit]


def detect_version_gated_apis(code_context: Any) -> List[str]:
    """返回在崩溃相关源码中命中的版本门控标记（去重保序）。"""
    text = _collect_code_text(code_context)
    if not text:
        return []
    hits: List[str] = []
    for marker in _VERSION_GATED_MARKERS:
        if marker in text and marker not in hits:
            hits.append(marker)
    return hits


def _crash_function_complete(code_context: Any) -> Optional[bool]:
    """从 graph.evidence_summary 读取崩溃函数片段是否完整；未知时返回 None。"""
    if not isinstance(code_context, dict):
        return None
    graph = code_context.get("graph") if isinstance(code_context.get("graph"), dict) else {}
    summary = graph.get("evidence_summary") if isinstance(graph, dict) else None
    if not isinstance(summary, dict):
        return None
    if "crash_function_snippet_complete" not in summary:
        return None
    return bool(summary.get("crash_function_snippet_complete"))


def _pc_null_like(parse_result: Any) -> bool:
    """PC 落在 0 / 0x1 等空指针偏移形态时返回真。"""
    if not isinstance(parse_result, dict):
        return False
    registers = parse_result.get("registers")
    if not isinstance(registers, dict):
        return False
    address_map = registers.get("address_map")
    if isinstance(address_map, dict):
        pc = address_map.get("pc")
        if isinstance(pc, dict):
            pattern = str(pc.get("pattern") or "").strip().lower()
            if pattern in {"null_pointer", "null_pointer_offset", "null"}:
                return True
            kind = str(pc.get("kind") or "").strip().lower()
            if kind == "null":
                return True
            va = str(pc.get("va") or "").strip().lower()
            if va in {"0x0", "0", "0x1", "1"}:
                return True
    values = registers.get("values")
    if isinstance(values, dict):
        raw = str(values.get("pc") or "").strip().lower().replace("_", "")
        if raw in {"0x0", "0x0000000000000000", "0", "0x1", "0x0000000000000001"}:
            return True
    return False


def evaluate_conclusion_readiness(
    *,
    parse_result: Any,
    code_context: Any = None,
    scope: str = "full",
) -> Dict[str, Any]:
    """评估 Gate B。

    返回字典字段：
    - ready: 是否允许调用 LLM 主张主根因 / 进入改码分析
    - gaps: [{id, field, message, how_to_fix}, ...]
    - os_version / os_type / version_gated_markers / crash_function_complete
    - user_message: 给用户/数字员工的补材说明
    """
    scope_norm = str(scope or "full").strip() or "full"
    os_version = extract_os_version(parse_result)
    os_type = extract_os_type(parse_result)
    markers = detect_version_gated_apis(code_context)
    complete = _crash_function_complete(code_context)
    gaps: List[Dict[str, str]] = []

    # 仅 full / gen_prompt_only 需要结论完备性；解析/符号化 scope 不拦。
    if scope_norm in {"full", "gen_prompt_only"}:
        if markers and not os_version:
            gaps.append(
                {
                    "id": "missing_os_version_for_api_gate",
                    "field": _OS_FIELD,
                    "message": (
                        "崩溃相关源码含版本门控 API（如 @available / CAFrameRateRangeMake），"
                        "但日志元信息缺少系统版本，无法判断该分支是否在崩溃设备上执行。"
                    ),
                    "how_to_fix": (
                        "请补充完整 Apple/系统崩溃日志原文（须含 `OS Version:` 行），"
                        "或在 iCafe 详情/附件中写明系统版本后重发。"
                    ),
                }
            )
        elif markers and os_version and _pc_null_like(parse_result):
            # 有系统版本：不阻断，但标记需在提示词里强制按版本裁剪假说。
            pass

        if complete is False and markers:
            gaps.append(
                {
                    "id": "incomplete_crash_function_with_api_gate",
                    "field": "崩溃函数完整源码",
                    "message": (
                        "崩溃函数片段不完整，且源码含版本门控 API；"
                        "在缺少完整函数体与系统版本对齐证据时不宜主张主根因。"
                    ),
                    "how_to_fix": (
                        "请确认代码分支与崩溃版本一致，并保证崩溃帧对应函数可完整检出；"
                        "必要时提供同版本符号或扩大 --scan-roots。"
                    ),
                }
            )

    ready = len(gaps) == 0
    user_message = _format_user_message(ready, gaps, os_version=os_version, markers=markers)
    return {
        "schema_version": 1,
        "gate": "conclusion_readiness",
        "ready": ready,
        "scope": scope_norm,
        "os_type": os_type or None,
        "os_version": os_version or None,
        "version_gated_markers": markers,
        "crash_function_complete": complete,
        "pc_null_like": _pc_null_like(parse_result),
        "gaps": gaps,
        "user_message": user_message,
    }


def _format_user_message(
    ready: bool,
    gaps: Sequence[Mapping[str, str]],
    *,
    os_version: str,
    markers: Sequence[str],
) -> str:
    """生成面向用户的补材说明。"""
    if ready:
        if os_version and markers:
            return (
                f"结论完备性检查通过（系统版本={os_version}；"
                f"已检测到版本门控标记：{', '.join(markers[:6])}）。"
            )
        if os_version:
            return f"结论完备性检查通过（系统版本={os_version}）。"
        return "结论完备性检查通过。"
    lines = ["现有崩溃证据不足以安全下结论，已停止大模型分析与自动改码。"]
    for index, gap in enumerate(gaps, start=1):
        field = str(gap.get("field") or "材料").strip()
        message = str(gap.get("message") or "").strip()
        how = str(gap.get("how_to_fix") or "").strip()
        lines.append(f"{index}. 缺少「{field}」：{message}")
        if how:
            lines.append(f"   建议：{how}")
    return "\n".join(lines)


def pipeline_skip_metadata_conclusion(report: Mapping[str, Any]) -> Dict[str, Any]:
    """把 Gate B 失败结果转成与 no_usable_* 一致的 pipeline_skip metadata。"""
    msg = str(report.get("user_message") or "").strip() or (
        "结论完备性检查未通过，已终止大模型分析。"
    )
    return {
        "pipeline_skipped": True,
        "pipeline_skip_reason": "conclusion_not_ready",
        "pipeline_skip_user_message": msg,
        "llm_skipped": True,
        "llm_skip_reason": "conclusion_not_ready",
        "llm_skip_user_message": msg,
        "conclusion_readiness": dict(report),
    }


def prompt_os_constraint_lines(parse_result: Any, code_context: Any = None) -> List[str]:
    """当已有系统版本时，生成写入 Evidence Contract 的硬约束行。"""
    os_version = extract_os_version(parse_result)
    os_type = extract_os_type(parse_result) or "unknown"
    markers = detect_version_gated_apis(code_context)
    if not os_version:
        return []
    lines = [
        f"- 日志元信息系统版本：{os_type} {os_version}（事实）。",
        "- 主张「某系统版本才引入的 API / @available 分支」为主根因前，必须证明该分支在上述系统版本上会执行；",
        "  若 @available 门槛高于崩溃系统版本，不得将该分支内的调用当作本次崩溃触发点。",
    ]
    if markers:
        preview = ", ".join(markers[:8])
        lines.append(f"- 当前崩溃相关源码已出现版本门控标记：{preview}。")
    return lines
