#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""code_content_provider 用户可见错误文案与 JSON 载荷（与调试 detail 分离）。"""

from __future__ import annotations

from typing import Any, Dict, Optional

# --- error codes ---
NO_FRAMES_IN_CODE_ROOT = "NO_FRAMES_IN_CODE_ROOT"
NO_EXTRACTABLE_CONTEXT = "NO_EXTRACTABLE_CONTEXT"

_USER_MESSAGES: Dict[str, str] = {
    NO_FRAMES_IN_CODE_ROOT: (
        "无法在代码目录中定位崩溃栈。"
        "请检查 --code-root 是否与符号化路径一致，或堆栈是否落在工程外（系统库/SDK）。"
    ),
    NO_EXTRACTABLE_CONTEXT: (
        "未能从堆栈提取可分析的 C++ 源码。"
        "请确认 addr2line/库目录正确，且栈顶为工程内符号（非仅 ObjC/系统库）。"
    ),
}

# 历史 error 字符串 → 默认 user_message（兼容旧报告 JSON）
_LEGACY_ERROR_HINTS: Dict[str, str] = {
    "没有可用的堆栈帧（所有堆栈帧都被过滤或未解析）": _USER_MESSAGES[NO_FRAMES_IN_CODE_ROOT],
}


def user_message_for_code(code: str) -> str:
    return _USER_MESSAGES.get(code, "未能生成崩溃源码上下文，请检查 code_root 与符号化结果。")


def build_error_payload(
    code: str,
    *,
    detail: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构建写入 03_code_content_provider.json 的错误对象字段。"""
    user_msg = user_message_for_code(code)
    payload: Dict[str, Any] = {
        "error_code": code,
        "user_message": user_msg,
        # 保留 error 供旧逻辑读取；内容与 user_message 一致
        "error": user_msg,
    }
    if detail:
        payload["error_detail"] = detail
    if extra:
        payload.update(extra)
    return payload


def code_context_failure_message(code_context: Any) -> Optional[str]:
    """从 code_context 取出面向用户的失败说明；无失败时返回 None。"""
    if not isinstance(code_context, dict):
        return None
    if not (code_context.get("error") or code_context.get("error_code")):
        return None
    user = str(code_context.get("user_message") or "").strip()
    if user:
        return user
    legacy = str(code_context.get("error") or "").strip()
    if not legacy:
        return None
    return _LEGACY_ERROR_HINTS.get(legacy, legacy)


def code_context_has_failure(code_context: Any) -> bool:
    return code_context_failure_message(code_context) is not None


# 占位/错误片段标记（非真实可分析源码）
_SNIPPET_PLACEHOLDER_MARKERS: tuple = (
    "源码提取失败",
    "请检查 code_roots",
    "代码上下文整阶段超时",
    "未完成源码提取",
)


def _text_looks_like_placeholder(text: str) -> bool:
    s = str(text or "").strip()
    if not s:
        return True
    if s.startswith("（") and ("失败" in s or "超时" in s):
        return True
    return any(m in s for m in _SNIPPET_PLACEHOLDER_MARKERS)


def _snippet_is_usable(snippet: Any, *, snippet_scope: Optional[str] = None) -> bool:
    if str(snippet_scope or "").strip().lower() == "error":
        return False
    if not isinstance(snippet, list) or not snippet:
        return False
    lines = [str(x).strip() for x in snippet if str(x).strip()]
    if not lines:
        return False
    joined = "\n".join(lines)
    if _text_looks_like_placeholder(joined):
        return False
    if len(lines) >= 2:
        return True
    # 单行：需像源码而非纯说明
    line = lines[0]
    return ("(" in line or "{" in line or ";" in line) and not _text_looks_like_placeholder(line)


def _iter_graph_nodes(code_context: Dict[str, Any]) -> list:
    graph = code_context.get("graph", {})
    if not isinstance(graph, dict):
        return []
    nodes = graph.get("nodes", [])
    return nodes if isinstance(nodes, list) else []


def code_context_has_usable_code(code_context: Any) -> bool:
    """03 中是否包含至少一段可用于 AI 分析的源码（非占位/非仅顶层 error）。"""
    if not isinstance(code_context, dict):
        return False
    if code_context_has_failure(code_context):
        return False

    for node in _iter_graph_nodes(code_context):
        if not isinstance(node, dict):
            continue
        ntype = str(node.get("type") or "")
        if ntype == "function":
            if _snippet_is_usable(
                node.get("snippet"),
                snippet_scope=node.get("snippet_scope"),
            ):
                return True
        elif ntype == "class_skeleton":
            skel = node.get("skeleton")
            if isinstance(skel, str) and skel.strip() and not _text_looks_like_placeholder(skel):
                return True
            if isinstance(skel, list) and _snippet_is_usable(skel):
                return True

    cs = code_context.get("crash_summary", {})
    if isinstance(cs, dict):
        crash_location = cs.get("crash_location")
        line_code = str(
            cs.get("crash_line_code")
            or (crash_location.get("code") if isinstance(crash_location, dict) else "")
            or ""
        ).strip()
        if line_code and not _text_looks_like_placeholder(line_code):
            return True
        from tools.owner_class_context import resolve_owner_class_from_code_context

        owner = resolve_owner_class_from_code_context(code_context)
        if isinstance(owner, dict):
            excerpt = owner.get("class_body_excerpt")
            if isinstance(excerpt, list) and _snippet_is_usable(excerpt):
                return True
            skel = owner.get("skeleton")
            if isinstance(skel, list) and _snippet_is_usable(skel):
                return True
            if isinstance(skel, str) and skel.strip() and not _text_looks_like_placeholder(skel):
                return True

    return False


def code_context_skip_pipeline_message(code_context: Any, *, scope: str = "full") -> str:
    """无可用源码时提前终止流水线的终端说明（含 gen_prompt_only）。"""
    fail = code_context_failure_message(code_context)
    scope_norm = str(scope or "full").strip() or "full"
    tail = "已终止后续流程"
    if scope_norm == "gen_prompt_only":
        tail = "已跳过提示词生成（06_ai_prompt.md）及大模型调用"
    elif scope_norm == "full":
        tail = "已跳过 AI 分析、自动改码及大模型调用"
    if fail:
        return f"{fail} {tail}。"
    return (
        "03 中未包含任何可用源码片段（仅堆栈/元数据）。"
        f"{tail}；请检查 02 符号化与 --code-root。"
    )


def code_context_skip_llm_user_message(code_context: Any) -> str:
    """兼容旧名；等同 code_context_skip_pipeline_message(scope=full)。"""
    return code_context_skip_pipeline_message(code_context, scope="full")


def pipeline_skip_metadata_code(
    code_context: Any,
    *,
    scope: str = "full",
    reason: str = "no_usable_code",
) -> Dict[str, Any]:
    msg = code_context_skip_pipeline_message(code_context, scope=scope)
    return {
        "pipeline_skipped": True,
        "pipeline_skip_reason": reason,
        "pipeline_skip_user_message": msg,
        "llm_skipped": True,
        "llm_skip_reason": reason,
        "llm_skip_user_message": msg,
    }


def llm_skip_metadata(code_context: Any) -> Dict[str, Any]:
    """兼容旧调用；请优先使用 pipeline_skip_metadata_code。"""
    return pipeline_skip_metadata_code(code_context, scope="full")


class CodeContextUserError(Exception):
    """可预期的源码上下文失败；由 code_content_provider 捕获并转为 JSON。"""

    def __init__(
        self,
        code: str,
        *,
        detail: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.code = code
        self.payload = build_error_payload(code, detail=detail, extra=extra)
        super().__init__(self.payload.get("user_message") or code)
