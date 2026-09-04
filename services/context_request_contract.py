"""Shared context-request return-form and rendering contract."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from services.agent_output_parser import parse_agent_decision
from services.agent_schema import CONTEXT_REQUEST_TYPES


CONTEXT_REQUEST_RETURN_FORMS: Dict[str, str] = {
    "function": "function_source",
    "field": "member_declaration",
    "references": "read_write_references",
    "callers": "caller_snippets",
    "grep": "grep_matches",
    "read_file": "file_snippet",
}

CONTEXT_REQUEST_RETURN_FORM_LABELS: Dict[str, str] = {
    "function_source": "函数定义处完整源码（含签名与函数体）",
    "member_declaration": "所属类成员声明（优先头文件）",
    "member_initialization": "成员初始化语句",
    "class_declaration": "类/结构体声明块（头文件）",
    "read_write_references": "读写/引用位置（文件:行 + 片段）",
    "caller_snippets": "调用方函数名 + 调用点片段",
    "grep_matches": "仓库 grep 匹配（文件:行 + 片段）",
    "file_snippet": "指定文件的源码片段",
}


def default_expected_return_form(request_type: str) -> str:
    return CONTEXT_REQUEST_RETURN_FORMS.get(
        str(request_type or "").strip().lower(), "function_source"
    )


def return_form_label(form: str) -> str:
    return CONTEXT_REQUEST_RETURN_FORM_LABELS.get(
        str(form or "").strip(), str(form or "")
    )


def normalize_expected_return_form(request_type: str, raw_form: Any) -> str:
    request_type = str(request_type or "").strip().lower()
    default_form = default_expected_return_form(request_type)
    token = str(raw_form or "").strip().lower()
    aliases = {
        "function": "function_source",
        "function_source": "function_source",
        "source": "function_source",
        "field": "member_declaration",
        "member_declaration": "member_declaration",
        "declaration": "member_declaration",
        "references": "read_write_references",
        "read_write_references": "read_write_references",
        "reference": "read_write_references",
        "callers": "caller_snippets",
        "caller_snippets": "caller_snippets",
    }
    normalized = aliases.get(token, token) if token else default_form
    allowed_by_type = {
        "function": {"function_source"},
        "field": {"member_declaration", "member_initialization"},
        "references": {"read_write_references"},
        "callers": {"caller_snippets"},
        "grep": {"grep_matches"},
        "read_file": {"file_snippet"},
    }
    return normalized if normalized in allowed_by_type.get(request_type, {default_form}) else default_form


def infer_actual_return_form(item: Dict[str, Any]) -> Optional[str]:
    if not item.get("success"):
        return None
    context_type = str(item.get("context_type") or "function").strip().lower()
    if context_type == "function":
        return "function_source"
    if context_type == "field":
        matches = item.get("matches")
        if isinstance(matches, list) and matches:
            kind = str((matches[0] or {}).get("match_kind") or "").strip().lower()
            if kind == "class_declaration":
                return "class_declaration"
            if kind == "initialization":
                return "member_initialization"
        return "member_declaration"
    if context_type == "references":
        return "read_write_references"
    if context_type == "callers":
        return "caller_snippets"
    if context_type == "grep":
        return "grep_matches"
    if context_type == "read_file":
        return "file_snippet"
    return None


def fulfillment_matched(expected_form: str, actual_form: Optional[str]) -> Optional[bool]:
    if not actual_form:
        return None
    expected = str(expected_form or "").strip().lower()
    actual = str(actual_form or "").strip().lower()
    return expected == actual or (
        expected == "member_declaration"
        and actual in {"member_initialization", "class_declaration"}
    )


def attach_return_form_metadata(item: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(item, dict):
        return item
    request = item.get("request") if isinstance(item.get("request"), dict) else {}
    request_type = str(request.get("type") or "function").strip().lower()
    expected = normalize_expected_return_form(
        request_type,
        request.get("expected_return_form")
        or request.get("return_form")
        or request.get("expected_form"),
    )
    item["expected_return_form"] = expected
    item["expected_return_form_label"] = return_form_label(expected)
    actual = infer_actual_return_form(item)
    if actual:
        item["actual_return_form"] = actual
        item["actual_return_form_label"] = return_form_label(actual)
        matched = fulfillment_matched(expected, actual)
        if matched is not None:
            item["fulfillment_matched"] = matched
    return item


def parse_context_requests(analysis_text: str) -> Dict[str, Any]:
    parsed = parse_agent_decision(analysis_text, allowed_types=set(CONTEXT_REQUEST_TYPES))
    normalized: List[Dict[str, Any]] = []
    for item in parsed.get("context_requests", []):
        if not isinstance(item, dict):
            continue
        request_type = str(item.get("type") or "function").strip().lower()
        expected = normalize_expected_return_form(
            request_type,
            item.get("expected_return_form") or item.get("return_form") or item.get("expected_form"),
        )
        normalized.append({
            **item,
            "expected_return_form": expected,
            "expected_return_form_label": return_form_label(expected),
            "fulfillment_note": str(
                item.get("fulfillment_note")
                or item.get("request_type_note")
                or item.get("return_form_note")
                or ""
            ).strip(),
        })
    return {
        "agent_can_fetch_more": parsed.get("agent_can_fetch_more", False),
        "context_requests": normalized,
        "invalid_context_requests": parsed.get("invalid_context_requests", []),
        "raw_payload": parsed.get("raw_payload", {}),
        "degraded": parsed.get("degraded", False),
        "hypotheses": parsed.get("hypotheses", []),
        "next_action": parsed.get("next_action", {}),
    }


def resolved_context_status(item: Dict[str, Any]) -> str:
    if item.get("success"):
        return "located"
    if item.get("lookup_exhausted"):
        return "lookup_exhausted"
    if item.get("rejected"):
        return "rejected"
    if item.get("skipped"):
        return "duplicate_success"
    return "not_located"


def all_context_requests_blocked(resolved: List[Dict[str, Any]]) -> bool:
    items = [item for item in resolved if isinstance(item, dict)]
    if not items or any(bool(item.get("success")) for item in items):
        return False
    return all(
        bool(item.get("lookup_exhausted"))
        or bool(item.get("rejected"))
        or (bool(item.get("skipped")) and item.get("skip_reason") == "duplicate_request")
        for item in items
    )


def build_pre_round_add_res(
    *,
    source_round: int,
    target_round: int,
    resolved_context: List[Dict[str, Any]],
) -> Dict[str, Any]:
    requests_out: List[Dict[str, Any]] = []
    for item in resolved_context:
        if not isinstance(item, dict):
            continue
        request = item.get("request") if isinstance(item.get("request"), dict) else {}
        request_type = str(request.get("type") or "function")
        expected = normalize_expected_return_form(
            request_type,
            request.get("expected_return_form") or item.get("expected_return_form"),
        )
        entry: Dict[str, Any] = {
            "type": request_type,
            "symbol": str(request.get("symbol") or ""),
            "reason": str(request.get("reason") or ""),
            "priority": str(request.get("priority") or ""),
            "expected_return_form": expected,
            "expected_return_form_label": return_form_label(expected),
            "fulfillment_note": str(request.get("fulfillment_note") or "") or None,
            "status": resolved_context_status(item),
            "located": bool(item.get("success")),
            "error": str(item.get("error") or "") or None,
        }
        actual = infer_actual_return_form(item)
        if actual:
            entry["actual_return_form"] = actual
            entry["actual_return_form_label"] = return_form_label(actual)
            entry["fulfillment_matched"] = fulfillment_matched(expected, actual)
        if item.get("success"):
            if request_type == "function":
                entry.update({
                    "file": item.get("file"),
                    "line_start": item.get("snippet_start_line"),
                    "line_end": item.get("snippet_end_line"),
                    "function_signature": item.get("function_signature"),
                })
            else:
                entry["matches"] = item.get("matches")
        requests_out.append(entry)
    return {
        "schema_version": 2,
        "source_round": int(source_round),
        "target_round": int(target_round),
        "requests": requests_out,
    }


def format_context_resolution(item: Dict[str, Any]) -> str:
    request = item.get("request") if isinstance(item.get("request"), dict) else {}
    symbol = str(request.get("symbol") or request.get("file") or "未知符号").strip()
    request_type = str(request.get("type") or item.get("context_type") or "function").strip()
    expected = normalize_expected_return_form(
        request_type,
        request.get("expected_return_form") or item.get("expected_return_form"),
    )
    lines: List[str] = [
        f"#### 其它代码上下文: {symbol}（{request_type}）",
        f"- 期望回填形式: {return_form_label(expected)} (`{expected}`)",
    ]
    if request.get("fulfillment_note"):
        lines.append(f"- 请求说明: {request.get('fulfillment_note')}")
    if not item.get("success"):
        status_label = {
            "located": "已定位",
            "not_located": "未定位",
            "lookup_exhausted": "此前已尝试未定位",
            "rejected": "已拒绝",
            "duplicate_success": "已成功补充（重复请求）",
        }.get(resolved_context_status(item), "未定位")
        lines.append(f"- 状态: {status_label}")
        if item.get("error"):
            lines.append(f"- 说明: {item.get('error')}")
        return "\n".join(lines)

    context_type = str(item.get("context_type") or "function")
    lines.append("- 状态: 已定位")
    if context_type in {"field", "references"}:
        lines.append(
            "- 回填说明: 优先返回所属类的成员声明；若无声明则返回同类初始化；不包含调用链。"
            if context_type == "field"
            else "- 回填说明: 返回符号在所属类相关文件中的读写/引用位置。"
        )
        for index, match in enumerate(item.get("matches", [])[:8]):
            if not isinstance(match, dict):
                continue
            if index:
                lines.append("")
            if match.get("match_kind_label") or match.get("match_kind"):
                lines.append(f"- 命中类型: {match.get('match_kind_label') or match.get('match_kind')}")
            lines.extend([f"- 文件: {match.get('file')}", f"- 行号: {match.get('line_number')}"])
            if match.get("line_text"):
                lines.append(f"- 命中行: {match.get('line_text')}")
            context_lines = match.get("context")
            if isinstance(context_lines, list) and context_lines:
                lines.extend(["- 代码片段:", "```cpp", *[str(x) for x in context_lines], "```"])
        return "\n".join(lines)
    if context_type == "callers":
        for index, match in enumerate(item.get("matches", [])[:8]):
            if not isinstance(match, dict):
                continue
            if index:
                lines.append("")
            if match.get("name"):
                lines.append(f"- 函数: {match.get('name')}")
            lines.append(f"- 文件: {match.get('file')}")
            snippet = match.get("snippet")
            if isinstance(snippet, list) and snippet:
                lines.extend(["- 代码片段:", "```cpp", *[str(x) for x in snippet[:80]], "```"])
        return "\n".join(lines)
    if context_type == "grep":
        lines.append("- 回填说明: 仓库 grep 匹配结果。")
        for index, match in enumerate(item.get("matches", [])[:12]):
            if not isinstance(match, dict):
                continue
            if index:
                lines.append("")
            fp = match.get("file", "")
            ln = match.get("line", match.get("line_number", ""))
            text = str(match.get("line_text") or match.get("text") or "").strip()
            lines.append(f"- `{fp}:{ln}` {text[:200]}")
        snippet = item.get("snippet")
        if isinstance(snippet, list) and snippet and not item.get("matches"):
            lines.extend(["- 代码片段:", "```", *[str(x) for x in snippet[:80]], "```"])
        return "\n".join(lines)
    if context_type == "read_file":
        lines.append(f"- 文件: {item.get('file')}")
        if item.get("snippet_start_line") and item.get("snippet_end_line"):
            lines.append(f"- 行号范围: {item.get('snippet_start_line')}～{item.get('snippet_end_line')}")
        snippet = item.get("snippet")
        if isinstance(snippet, list) and snippet:
            lines.extend(["- 代码片段:", "```cpp", *[str(x) for x in snippet[:120]], "```"])
        return "\n".join(lines)

    lines.append(f"- 文件: {item.get('file')}")
    if item.get("snippet_start_line") and item.get("snippet_end_line"):
        lines.append(f"- 行号范围: {item.get('snippet_start_line')}～{item.get('snippet_end_line')}")
    if item.get("function_signature"):
        lines.append(f"- 函数: {item.get('function_signature')}")
    snippet = item.get("snippet")
    if isinstance(snippet, list) and snippet:
        lines.extend(["- 代码片段:", "```cpp", *[str(x) for x in snippet], "```"])
    return "\n".join(lines)
