#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
工具链解耦合并工具。

提供从独立的 01/02/03 文件重建「虚拟合并视图」的能力，
使 prompt builder 和 code_content_provider 无需依赖跨文件冗余数据。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def merge_resolved_view(parse_result: dict, add2line_result: dict) -> List[dict]:
    """合并 01 帧数据 + 02 解析结果，返回带线程上下文的虚拟帧列表。

    通过 ``resolved_threads[i].frames[j]`` 与 01 的 ``threads[i].frames[j]``
    按索引对齐。返回扁平列表，每个 frame dict 同时包含：
    - 01 的原始帧信息（address, function, module, offset, ...）
    - 02 的解析结果（resolved_function, resolved_file, resolved_line, resolution_kind）
    - 所属线程上下文（thread_tid, thread_name, thread_index,
      thread_is_crash_thread, thread_is_main_thread）

    兼容旧格式：若 02 帧已含 thread_* 字段则直接透传。
    """
    if not isinstance(parse_result, dict) or not isinstance(add2line_result, dict):
        return _fallback_flatten(add2line_result)

    resolved_threads = add2line_result.get("resolved_threads")
    if not isinstance(resolved_threads, list):
        return _fallback_flatten(add2line_result)

    parse_threads = parse_result.get("threads")
    if not isinstance(parse_threads, list):
        parse_threads = []

    out: List[dict] = []
    for t_idx, rt in enumerate(resolved_threads):
        if not isinstance(rt, dict):
            continue
        # 线程级元数据（来自 02 的 ResolvedThreadStack）
        tid = rt.get("tid")
        tname = rt.get("name")
        t_index = rt.get("thread_index")
        is_crash = bool(rt.get("is_crash_thread"))
        is_main = rt.get("is_main_thread")

        # 对应 01 的原始线程（按索引对齐）
        parse_thread = parse_threads[t_idx] if t_idx < len(parse_threads) else {}
        if not isinstance(parse_thread, dict):
            parse_thread = {}
        parse_frames = parse_thread.get("frames") or []

        frames = rt.get("frames") or []
        for f_idx, frame in enumerate(frames):
            if not isinstance(frame, dict):
                continue

            # 兼容旧格式：已有 thread_* 字段则直接透传
            if "thread_is_crash_thread" in frame:
                out.append(frame)
                continue

            # 从 01 补充原始帧信息
            parse_frame = parse_frames[f_idx] if f_idx < len(parse_frames) else {}
            if not isinstance(parse_frame, dict):
                parse_frame = {}

            merged = {}
            # 优先用 02 帧自身的字段（address, function, module 等 02 也保留了）
            merged.update(frame)
            # 补充 01 独有字段（offset, library_type, layer, language, subsystem, frame_number）
            for key in ("offset", "library_type", "layer", "language",
                        "subsystem", "frame_number", "stack_type"):
                if key not in merged and key in parse_frame:
                    merged[key] = parse_frame[key]

            # 注入线程上下文
            merged["thread_tid"] = tid
            merged["thread_name"] = tname
            merged["thread_index"] = t_index if t_index is not None else t_idx
            merged["thread_is_crash_thread"] = is_crash
            merged["thread_is_main_thread"] = is_main

            out.append(merged)

    return out


def build_crash_summary_view(
    parse_result: dict,
    add2line_result: dict,
    code_context: Optional[dict] = None,
) -> dict:
    """从 01+02+03 构建 crash_summary 视图（纯计算，不持久化）。

    返回 dict 与原 03.crash_summary 结构兼容，供 prompt builder 消费。

    字段来源:
    - error_type: parse_result["crash_info"]["signal"]
    - crash_thread_id/name/is_main_thread_crash: add2line_result 顶层字段
    - crash_location (file/function/line): 优先 code_context graph crash node，
      其次 02 首个已解析帧
    - analysis_entry_*: 来自 code_context graph 的 crash_func 或 crash node
    """
    if not isinstance(parse_result, dict):
        parse_result = {}
    if not isinstance(add2line_result, dict):
        add2line_result = {}
    if code_context is None:
        code_context = {}
    if not isinstance(code_context, dict):
        code_context = {}

    crash_info = parse_result.get("crash_info") or {}
    if not isinstance(crash_info, dict):
        crash_info = {}

    # --- error_type ---
    signal = crash_info.get("signal") or ""
    error_type = _extract_error_type(signal, crash_info)

    # --- crash thread metadata (from 02) ---
    crash_thread_id = add2line_result.get("crash_thread_id")
    crash_thread_name = add2line_result.get("crash_thread_name")
    crash_is_main = add2line_result.get("crash_thread_is_main_thread")
    crash_has_biz = add2line_result.get("crash_thread_has_business_frames")

    # --- crash location (from 03 graph or 02 resolution) ---
    graph = code_context.get("graph") or {}
    if not isinstance(graph, dict):
        graph = {}
    nodes = graph.get("nodes") or []
    crash_func_data = code_context.get("crash_func") or {}
    if not isinstance(crash_func_data, dict):
        crash_func_data = {}

    # 尝试从 graph nodes 找到 crash node（type=function, 有 snippet）
    crash_node = _find_crash_node(nodes, crash_func_data)

    # 从 crash node 或 02 首帧获取位置信息
    location = _resolve_crash_location(crash_node, crash_func_data, add2line_result)

    summary: Dict[str, Any] = {
        "error_type": error_type,
        "crash_thread_id": crash_thread_id,
        "crash_thread_name": crash_thread_name,
        "is_main_thread_crash": crash_is_main,
        "crash_thread_has_business_frames": crash_has_biz,
        "file": location.get("file", ""),
        "function": location.get("function", ""),
        "crash_line_number": location.get("line"),
        "stack_address": location.get("address", ""),
        "crash_line_code": location.get("code"),
        "crash_location_source": location.get("source"),
        "node_id": location.get("node_id"),
        "analysis_entry_file": location.get("analysis_entry_file"),
        "analysis_entry_function": location.get("analysis_entry_function"),
        "analysis_entry_line_number": location.get("analysis_entry_line"),
    }

    # 合并 code_context 中可能存在的额外 attribution 字段（兼容旧格式）
    old_summary = code_context.get("crash_summary")
    if isinstance(old_summary, dict):
        for key in (
            "crash_attribution_source",
            "selected_analysis_thread_id",
            "selected_analysis_thread_name",
            "selected_analysis_is_crash_thread",
            "selected_analysis_is_main_thread",
            "selected_analysis_source",
            "selected_analysis_confidence",
            "selected_analysis_note",
            "attributed_crash_location_status",
            "crash_line_note",
            "location_type",
        ):
            if key in old_summary and key not in summary:
                summary[key] = old_summary[key]

    return summary


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _fallback_flatten(add2line_result: Any) -> List[dict]:
    """回退：直接从 resolved_threads 扁平化帧列表。"""
    if not isinstance(add2line_result, dict):
        return []
    threads = add2line_result.get("resolved_threads")
    if not isinstance(threads, list):
        return []
    out: List[dict] = []
    for t in threads:
        if not isinstance(t, dict):
            continue
        for f in t.get("frames") or []:
            if isinstance(f, dict):
                out.append(f)
    return out


def _extract_error_type(signal: str, crash_info: dict) -> str:
    """从 crash_info 提取 error_type 字符串。"""
    # 优先 signal 字段（如 "11 (SIGSEGV)"）
    if signal:
        # 提取括号内的信号名
        import re
        m = re.search(r"\((\w+)\)", signal)
        if m:
            return m.group(1)
        return signal
    # 回退到 crash_reason
    reason = crash_info.get("crash_reason") or ""
    if reason:
        return reason
    return crash_info.get("exception_type") or "UNKNOWN"


def _find_crash_node(nodes: list, crash_func_data: dict) -> Optional[dict]:
    """在 graph nodes 中找到崩溃函数节点。"""
    if not isinstance(nodes, list):
        return None

    # 优先匹配 crash_func 的 name/signature
    crash_sig = crash_func_data.get("signature") or ""
    crash_name = crash_func_data.get("name") or ""

    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_sig = node.get("signature") or ""
        node_type = node.get("type") or ""
        if node_type != "function":
            continue
        # 精确匹配签名
        if crash_sig and node_sig == crash_sig:
            return node
        # 匹配函数名
        if crash_name and crash_name in node_sig:
            return node

    # 回退：取第一个 function 类型 node
    for node in nodes:
        if isinstance(node, dict) and node.get("type") == "function":
            return node
    return None


def _resolve_crash_location(
    crash_node: Optional[dict],
    crash_func_data: dict,
    add2line_result: dict,
) -> dict:
    """从 crash node / crash_func / 02 首帧获取崩溃位置信息。"""
    location: Dict[str, Any] = {}

    # 优先从 crash_func_data
    if crash_func_data:
        location["function"] = crash_func_data.get("signature") or crash_func_data.get("name") or ""
        location["code"] = crash_func_data.get("crash_line") or ""
        if crash_func_data.get("crash_line_number"):
            location["line"] = crash_func_data["crash_line_number"]

    # 从 crash_node 补充
    if crash_node:
        if not location.get("function"):
            location["function"] = crash_node.get("signature") or ""
        if not location.get("file"):
            location["file"] = crash_node.get("file") or ""
        location["node_id"] = crash_node.get("id")
        # analysis_entry 字段
        location["analysis_entry_file"] = crash_node.get("file")
        location["analysis_entry_function"] = crash_node.get("signature")
        # snippet 中的行号
        snippet = crash_node.get("snippet")
        if isinstance(snippet, list) and snippet and not location.get("line"):
            # crash_node 本身可能不记录行号，仅补充 file
            pass

    # 从 02 首帧补充（如果 graph 信息不足）
    resolved_threads = add2line_result.get("resolved_threads") or []
    if isinstance(resolved_threads, list):
        for rt in resolved_threads:
            if not isinstance(rt, dict):
                continue
            if not rt.get("is_crash_thread"):
                continue
            frames = rt.get("frames") or []
            if frames and isinstance(frames[0], dict):
                first = frames[0]
                if not location.get("address"):
                    location["address"] = first.get("address") or ""
                if not location.get("file"):
                    location["file"] = first.get("resolved_file") or first.get("file") or ""
                if not location.get("function"):
                    location["function"] = first.get("resolved_function") or first.get("function") or ""
                if not location.get("line"):
                    rl = first.get("resolved_line") or first.get("line")
                    if rl is not None:
                        location["line"] = rl
                if not location.get("source"):
                    rk = first.get("resolution_kind")
                    if rk == "addr2line":
                        location["source"] = "from_add2line"
                    else:
                        location["source"] = "from_log_deduce"
                break

    # analysis_entry fallback
    if not location.get("analysis_entry_file"):
        location["analysis_entry_file"] = location.get("file")
    if not location.get("analysis_entry_function"):
        location["analysis_entry_function"] = location.get("function")
    if not location.get("analysis_entry_line"):
        location["analysis_entry_line"] = location.get("line")

    return location
