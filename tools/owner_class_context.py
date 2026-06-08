#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""类成员分析入口的 owner 类上下文：条件落盘、graph 节点承载、从 code_context 解析。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def should_persist_owner_class_from_crash_summary(cs: Any) -> bool:
    """
    是否将 owner 类上下文写入 graph（class_skeleton 节点）并在 crash_summary 记录 owner_class_node_id。

    弱归因（跨线程排查入口、归因线程无库目录业务帧）不持久化，避免与「非确定崩溃点」语义冲突。
    """
    if not isinstance(cs, dict):
        return False
    if cs.get("selected_analysis_is_crash_thread") is not True:
        return False
    if cs.get("attributed_crash_location_status") == "unresolved_crash_thread_no_business_frame":
        return False
    if cs.get("selected_analysis_confidence") == "investigation_hint":
        return False
    return True


def should_persist_owner_class_for_crash_summary_dataclass(crash_summary: Any) -> bool:
    """CrashSummary dataclass → 与 dict 规则一致。"""
    if crash_summary is None:
        return False
    return should_persist_owner_class_from_crash_summary(
        {
            "selected_analysis_is_crash_thread": getattr(
                crash_summary, "selected_analysis_is_crash_thread", None
            ),
            "attributed_crash_location_status": getattr(
                crash_summary, "attributed_crash_location_status", None
            ),
            "selected_analysis_confidence": getattr(
                crash_summary, "selected_analysis_confidence", None
            ),
        }
    )


def class_skeleton_node_id(definition_file: str, class_name: str) -> str:
    return f"class_skeleton|{definition_file}|{class_name}"


def build_class_skeleton_node(owner_ctx: Dict[str, Any]) -> Dict[str, Any]:
    """由内存快照构建 graph.class_skeleton 节点（唯一落盘载体）。"""
    definition_file = str(owner_ctx.get("definition_file") or "").strip()
    class_name = str(owner_ctx.get("class_name") or "").strip()
    skel_lines = owner_ctx.get("skeleton")
    if isinstance(skel_lines, list):
        skeleton_text = "\n".join(str(ln) for ln in skel_lines)
    else:
        skeleton_text = str(skel_lines or "")
    excerpt = owner_ctx.get("class_body_excerpt")
    member_fields = owner_ctx.get("member_fields")
    node: Dict[str, Any] = {
        "id": class_skeleton_node_id(definition_file, class_name),
        "type": "class_skeleton",
        "class_name": class_name,
        "file": definition_file,
        "skeleton": skeleton_text,
    }
    if isinstance(member_fields, list) and member_fields:
        node["member_fields"] = list(member_fields)
    if isinstance(excerpt, list) and excerpt:
        node["class_body_excerpt"] = [str(ln) for ln in excerpt]
    return node


def owner_dict_from_skeleton_node(node: Dict[str, Any]) -> Dict[str, Any]:
    """将 class_skeleton 节点还原为与历史 owner_class_context 兼容的 dict（供 fixer/filter 使用）。"""
    skel = node.get("skeleton")
    if isinstance(skel, str):
        skeleton_lines = [ln for ln in skel.splitlines()]
    elif isinstance(skel, list):
        skeleton_lines = [str(ln) for ln in skel]
    else:
        skeleton_lines = []
    excerpt = node.get("class_body_excerpt")
    if not isinstance(excerpt, list):
        excerpt = []
    fields = node.get("member_fields")
    if not isinstance(fields, list):
        fields = []
    return {
        "class_name": node.get("class_name"),
        "definition_file": node.get("file") or node.get("definition_file"),
        "member_fields": fields,
        "class_body_excerpt": excerpt,
        "skeleton": skeleton_lines,
    }


def resolve_owner_class_from_code_context(
    code_context: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    从 code_context 解析 owner 类上下文：优先 owner_class_node_id → graph 节点；
    兼容旧报告 crash_summary.owner_class_context。
    """
    if not isinstance(code_context, dict):
        return None
    cs = code_context.get("crash_summary")
    if not isinstance(cs, dict):
        return None

    legacy = cs.get("owner_class_context")
    if isinstance(legacy, dict) and legacy.get("class_name"):
        return legacy

    crash_location = cs.get("crash_location")
    nid = cs.get("owner_class_node_id")
    if not nid and isinstance(crash_location, dict):
        nid = crash_location.get("owner_class_node_id")
    if not isinstance(nid, str) or not nid.strip():
        return None
    graph = code_context.get("graph")
    if not isinstance(graph, dict):
        return None
    for raw in graph.get("nodes") or []:
        if isinstance(raw, dict) and raw.get("id") == nid:
            return owner_dict_from_skeleton_node(raw)
    return None
