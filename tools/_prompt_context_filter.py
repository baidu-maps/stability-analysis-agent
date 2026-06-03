#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按置信度筛选送入 LLM 的函数源码，降低 prompt 信噪比并保证确定性。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

_WEAK_ONLY_TAGS = frozenset(
    {
        "调用链",
        "共享变量读/访问",
    }
)

# 路径片段：栈相关 walk/pano 模块（通用子串，非单一工程符号）
_STACK_PATH_HINTS = (
    "/walk/",
    "/pano",
    "/navi",
    "/guidance/",
    "/route_plan/",
    "/panodata/",
    "/navi_control/",
)


@dataclass(frozen=True)
class PromptFilterOptions:
    max_functions_in_prompt: int = 12
    max_stack_frames_in_prompt: int = 6


def norm_graph_nid(raw: Optional[str]) -> str:
    if not isinstance(raw, str):
        return ""
    return raw.rstrip().rstrip("{").rstrip()


def resolve_prompt_filter_options(
    code_context: Optional[Dict[str, Any]],
    problem: Optional[Dict[str, Any]],
) -> PromptFilterOptions:
    """从 code_context_options / problem 读取筛选上限。"""

    def _pick_int(key: str, default: int, lo: int, hi: int) -> int:
        for src in (
            (code_context or {}).get("code_context_options") or {},
            problem or {},
        ):
            if not isinstance(src, dict):
                continue
            raw = src.get(key)
            if raw is None:
                continue
            try:
                return max(lo, min(int(raw), hi))
            except (TypeError, ValueError):
                pass
        return default

    return PromptFilterOptions(
        max_functions_in_prompt=_pick_int("max_functions_in_prompt", 12, 4, 24),
        max_stack_frames_in_prompt=_pick_int("max_stack_frames_in_prompt", 6, 2, 16),
    )


def _function_name_from_signature(sig: str) -> str:
    s = str(sig or "").strip()
    if not s:
        return ""
    m = re.search(r"([~]?[A-Za-z_]\w*)\s*\([^)]*\)\s*(?:const)?\s*$", s)
    if m:
        return m.group(1)
    m2 = re.search(r"::([~]?[A-Za-z_]\w*)\s*\(", s)
    if m2:
        return m2.group(1)
    return s.split("(")[0].strip()[-64:]


def _normalize_path_key(path: str) -> str:
    return str(path or "").replace("\\", "/").lower()


def _file_basename(path: str) -> str:
    return Path(_normalize_path_key(path)).name


def _signatures_compatible(site_sig: str, node_sig: str) -> bool:
    a = str(site_sig or "").strip()
    b = str(node_sig or "").strip()
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    na = _function_name_from_signature(a)
    nb = _function_name_from_signature(b)
    if na and nb and na == nb:
        return True
    return False


def _resolved_symbol_key(resolved_function: str) -> str:
    """用于栈帧与图节点匹配的稳定键（类名+方法名）。"""
    s = str(resolved_function or "").strip()
    if not s:
        return ""
    s = re.sub(r"\s+", " ", s)
    if "::" in s:
        tail = s.split("::")[-1]
        owner = s.rsplit("::", 1)[0].split()[-1]
        method = tail.split("(")[0].strip()
        owner_tail = owner.split("::")[-1] if owner else owner
        return f"{owner_tail}::{method}".lower()
    return s.split("(")[0].strip().lower()


def build_stack_anchor_paths(
    code_context: Optional[Dict[str, Any]],
    resolved: Optional[Dict[str, Any]],
) -> Set[str]:
    """
    栈锚定路径：崩溃文件所在目录 + 栈帧已解析文件 + graph 栈优先文件。
    用于共享变量扩展等与崩溃栈相关的路径过滤。
    """
    anchors: Set[str] = set()
    if isinstance(code_context, dict):
        cs = code_context.get("crash_summary")
        if isinstance(cs, dict):
            owner = cs.get("owner_class_context")
            if isinstance(owner, dict) and owner.get("definition_file"):
                anchors.add(_normalize_path_key(str(owner["definition_file"])))
            node_id = cs.get("node_id")
            if isinstance(node_id, str) and "|" in node_id:
                parts = node_id.split("|")
                if len(parts) >= 2 and parts[1].strip():
                    anchors.add(_normalize_path_key(parts[1]))
        graph = code_context.get("graph")
        if isinstance(graph, dict):
            for sym in graph.get("stack_function_symbols") or []:
                if not isinstance(sym, dict):
                    continue
                fn = str(sym.get("function") or "")
                if fn:
                    anchors.add(_resolved_symbol_key(fn))
            for item in graph.get("call_chain_from_add2line") or []:
                if not isinstance(item, dict):
                    continue
                for nid in item.get("nodes") or []:
                    if not isinstance(nid, str) or "|" not in nid:
                        continue
                    fp = nid.split("|", 2)[1] if nid.count("|") >= 2 else ""
                    if fp.strip():
                        anchors.add(_normalize_path_key(fp))
    if isinstance(resolved, dict):
        for frame in resolved.get("resolved_frames") or []:
            if not isinstance(frame, dict):
                continue
            rf = str(frame.get("resolved_function") or frame.get("function") or "")
            if rf:
                anchors.add(_resolved_symbol_key(rf))
            rfile = str(frame.get("resolved_file") or "")
            if rfile.strip():
                anchors.add(_normalize_path_key(rfile))
    dir_anchors: Set[str] = set()
    for a in list(anchors):
        if a.endswith((".cpp", ".cc", ".cxx", ".h", ".hpp")):
            dir_anchors.add(str(Path(a).parent))
    anchors.update(dir_anchors)
    return anchors


def path_under_stack_anchors(file_path: str, anchors: Set[str]) -> bool:
    """文件是否落在栈锚定范围内。"""
    if not file_path or not anchors:
        return False
    nf = _normalize_path_key(file_path)
    for hint in _STACK_PATH_HINTS:
        if hint in nf:
            return True
    for a in anchors:
        if not a:
            continue
        if a in nf or nf in a:
            return True
        if nf.endswith(a) or a.endswith(nf):
            return True
        ab = _file_basename(a)
        if ab and ab == _file_basename(nf):
            return True
    return False


def match_resolved_frames_to_node_ids(
    node_map: Dict[str, Any],
    resolved: Optional[Dict[str, Any]],
    *,
    max_frames: int = 6,
) -> List[str]:
    """按日志栈序（自上而下）匹配图节点，供 prompt 强制纳入。"""
    if not isinstance(resolved, dict):
        return []
    frames = resolved.get("resolved_frames")
    if not isinstance(frames, list):
        return []
    cap = max(2, min(int(max_frames), 16))
    out: List[str] = []
    seen: Set[str] = set()
    for frame in frames[: cap * 2]:
        if len(out) >= cap:
            break
        if not isinstance(frame, dict):
            continue
        rf = str(frame.get("resolved_function") or frame.get("function") or "")
        key = _resolved_symbol_key(rf)
        if not key:
            continue
        for nid, node in node_map.items():
            if not isinstance(nid, str) or not isinstance(node, dict):
                continue
            if str(node.get("type") or "") != "function":
                continue
            nsig = str(node.get("signature") or "")
            if not _signatures_compatible(rf, nsig) and _resolved_symbol_key(nsig) != key:
                continue
            snippet = node.get("snippet")
            if not (isinstance(snippet, list) and snippet):
                continue
            norm = norm_graph_nid(nid)
            if norm in seen:
                continue
            seen.add(norm)
            out.append(nid)
            break
    return out


def _record_is_weak_only(
    tags: Set[str], norm_id: str, root_cause_norm: Set[str]
) -> bool:
    if norm_id in root_cause_norm:
        return False
    if "栈序保留" in tags or "崩溃函数" in tags:
        return False
    if not tags:
        return True
    return tags <= _WEAK_ONLY_TAGS


def _selection_tier(
    rec: Dict[str, Any],
    *,
    stack_norm: Set[str],
    root_cause_norm: Set[str],
    anchor_paths: Set[str],
) -> int:
    tags = rec.get("tags")
    tag_set = set(tags) if isinstance(tags, (set, list)) else set()
    nfid = str(rec.get("norm_id") or "")
    node = rec.get("node") if isinstance(rec.get("node"), dict) else {}
    nfile = str(node.get("file") or "")

    if nfid in stack_norm:
        return 0
    if "崩溃函数" in tag_set:
        return 1
    if _record_is_weak_only(tag_set, nfid, root_cause_norm):
        return 99
    if "调用崩溃点" in tag_set:
        return 2
    if "堆栈帧" in tag_set:
        return 3
    if "共享变量写" in tag_set:
        if anchor_paths and nfile and not path_under_stack_anchors(nfile, anchor_paths):
            return 90
        return 4
    if "共享变量关键读" in tag_set:
        if anchor_paths and nfile and not path_under_stack_anchors(nfile, anchor_paths):
            return 90
        return 5
    if "调用链" in tag_set:
        return 6
    if "共享变量读/访问" in tag_set:
        return 7
    return 50


def filter_prompt_function_records(
    ordered_records: List[Dict[str, Any]],
    *,
    root_cause_norm_ids: Optional[Set[str]] = None,
    stack_frame_norm_ids: Optional[Set[str]] = None,
    anchor_paths: Optional[Set[str]] = None,
    max_functions: int = 12,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    按置信度保留函数源码记录（全局上限，含强制档）。
    返回 (included_records, index_lines)。
    """
    root_cause_norm = root_cause_norm_ids or set()
    stack_norm = stack_frame_norm_ids or set()
    anchors = anchor_paths or set()
    cap = max(4, min(int(max_functions), 24))

    candidates: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []

    for rec in ordered_records:
        node = rec.get("node") if isinstance(rec.get("node"), dict) else {}
        snippet = node.get("snippet") if isinstance(node, dict) else None
        if not (isinstance(snippet, list) and snippet):
            continue
        tier = _selection_tier(
            rec,
            stack_norm=stack_norm,
            root_cause_norm=root_cause_norm,
            anchor_paths=anchors,
        )
        if tier >= 99:
            excluded.append(rec)
            continue
        rec_copy = dict(rec)
        rec_copy["_select_tier"] = tier
        candidates.append(rec_copy)

    candidates.sort(
        key=lambda r: (
            int(r.get("_select_tier", 50)),
            int(r.get("priority", 99)),
            str((r.get("node") or {}).get("signature") or ""),
        )
    )

    included: List[Dict[str, Any]] = []
    seen_norm: Set[str] = set()
    for rec in candidates:
        if len(included) >= cap:
            excluded.append(rec)
            continue
        nf = str(rec.get("norm_id") or "")
        if nf and nf in seen_norm:
            continue
        if nf:
            seen_norm.add(nf)
        rec.pop("_select_tier", None)
        included.append(rec)

    index_lines: List[str] = []
    for rec in excluded:
        node = rec.get("node") if isinstance(rec.get("node"), dict) else {}
        sig = str(node.get("signature") or "N/A")
        tags = rec.get("tags")
        tag_txt = (
            "、".join(sorted(tags))
            if isinstance(tags, (set, list)) and tags
            else "上下文候选"
        )
        tier = rec.get("_select_tier")
        reason = (
            "置信度较低已裁剪"
            if tier is None or tier >= 99
            else "超出函数体预算已裁剪"
        )
        index_lines.append(f"- {sig}（来源: {tag_txt}；未纳入：{reason}）")

    return included, index_lines
