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

_MUST_TAGS = frozenset(
    {
        "崩溃函数",
        "崩溃行被调",
        "栈序保留",
        "调用崩溃点",
        "线程投递对照",
    }
)

_NOISE_FUNC_RE = re.compile(r"::(?:find|Invoke|CONFIG_INS)\s*\(", re.I)

DEFAULT_FUNCTION_CHARS_IN_PROMPT = 96000
DEFAULT_MAX_STACK_FRAMES_IN_PROMPT = 4
DEFAULT_MAX_STACK_FRAMES_SYMBOL_ENRICH = 8

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
    # 0 表示不限制函数数量；弱线索仍会按 bucket 丢弃
    max_functions_in_prompt: int = 0
    max_stack_frames_in_prompt: int = DEFAULT_MAX_STACK_FRAMES_IN_PROMPT
    max_function_chars_in_prompt: int = DEFAULT_FUNCTION_CHARS_IN_PROMPT


def norm_graph_nid(raw: Optional[str]) -> str:
    if not isinstance(raw, str):
        return ""
    return raw.rstrip().rstrip("{").rstrip()


def resolve_prompt_filter_options(
    code_context: Optional[Dict[str, Any]],
    problem: Optional[Dict[str, Any]],
) -> PromptFilterOptions:
    """从 code_context_options / problem 读取筛选上限。"""
    diagnostics = (code_context or {}).get("diagnostics") if isinstance(code_context, dict) else {}
    diagnostics_options = (
        diagnostics.get("code_context_options")
        if isinstance(diagnostics, dict)
        else None
    )

    def _pick_int(key: str, default: int, lo: int, hi: int) -> int:
        for src in (
            diagnostics_options or {},
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
        max_functions_in_prompt=_pick_int("max_functions_in_prompt", 0, 0, 64),
        max_stack_frames_in_prompt=_pick_int(
            "max_stack_frames_in_prompt", DEFAULT_MAX_STACK_FRAMES_IN_PROMPT, 2, 16
        ),
        max_function_chars_in_prompt=_pick_int(
            "max_function_chars_in_prompt", DEFAULT_FUNCTION_CHARS_IN_PROMPT, 0, 400000
        ),
    )


def prompt_edit_eligibility_hint(tags: Any) -> str:
    """根据来源标签生成改码依据说明（写入 05 提示词）。"""
    tag_set = set(tags) if isinstance(tags, (set, list)) else set()
    if not tag_set:
        return "改码依据: 仅排查线索（不得单独列入「需要修改的函数」）"
    if tag_set <= _WEAK_ONLY_TAGS or "调用链" in tag_set:
        return "改码依据: 仅排查线索（不得单独列入「需要修改的函数」）"
    if tag_set == {"共享变量读/访问"} or (
        tag_set <= {"共享变量读/访问", "共享变量关键读"} and "共享变量写" not in tag_set
    ):
        return "改码依据: 需与高置信「共享变量写」证据一并论证，不得单独改码"
    if "共享变量关键读" in tag_set and "共享变量写" not in tag_set:
        return "改码依据: 需与高置信写路径一并论证，不得单独改码"
    return "改码依据: 可作为改码候选（须在证据清单引用本片段中的具体行）"


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
            from tools.owner_class_context import resolve_owner_class_from_code_context

            owner = resolve_owner_class_from_code_context(code_context)
            if isinstance(owner, dict) and owner.get("definition_file"):
                anchors.add(_normalize_path_key(str(owner["definition_file"])))
            candidate_node_ids = [cs.get("node_id")]
            crash_location = cs.get("crash_location")
            if isinstance(crash_location, dict):
                candidate_node_ids.append(crash_location.get("node_id"))
            for node_id in candidate_node_ids:
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
        from tools.resolve_stack_errors import flatten_resolved_frames_from_stack

        for frame in flatten_resolved_frames_from_stack(resolved):
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
    from tools.resolve_stack_errors import flatten_resolved_frames_from_stack

    frames = flatten_resolved_frames_from_stack(resolved)
    if not frames:
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
    if tags & _MUST_TAGS:
        return False
    if not tags:
        return True
    return tags <= _WEAK_ONLY_TAGS


def _looks_like_noise_helper(rec: Dict[str, Any]) -> bool:
    """find/Invoke 等行窗口：非 must 时默认不进 05。"""
    tags = rec.get("tags")
    tag_set = set(tags) if isinstance(tags, (set, list)) else set()
    if tag_set & _MUST_TAGS:
        return False
    node = rec.get("node") if isinstance(rec.get("node"), dict) else {}
    sig = str(node.get("signature") or "")
    return bool(_NOISE_FUNC_RE.search(sig))


def _snippet_char_len(rec: Dict[str, Any]) -> int:
    """估算函数片段写入 05 时占用的字符数。"""
    node = rec.get("node") if isinstance(rec.get("node"), dict) else {}
    snippet = node.get("snippet") if isinstance(node, dict) else None
    if not isinstance(snippet, list):
        return 0
    return sum(len(str(line)) + 1 for line in snippet)


def _record_bucket(
    rec: Dict[str, Any],
    *,
    stack_norm: Set[str],
    root_cause_norm: Set[str],
    anchor_paths: Set[str],
) -> str:
    """将函数记录分为 must / should / drop。"""
    tags = rec.get("tags")
    tag_set = set(tags) if isinstance(tags, (set, list)) else set()
    nfid = str(rec.get("norm_id") or "")
    if nfid in stack_norm or tag_set & _MUST_TAGS:
        return "must"
    if _looks_like_noise_helper(rec) or _record_is_weak_only(tag_set, nfid, root_cause_norm):
        return "drop"
    tier = _selection_tier(
        rec,
        stack_norm=stack_norm,
        root_cause_norm=root_cause_norm,
        anchor_paths=anchor_paths,
    )
    if tier >= 90:
        return "drop"
    return "should"


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
    if "崩溃函数" in tag_set or "崩溃行被调" in tag_set:
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
    max_functions: int = 0,
    max_function_chars: int = DEFAULT_FUNCTION_CHARS_IN_PROMPT,
    stats: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    按 must/should/drop 筛选送入 05 的函数源码。

    ``max_functions <= 0`` 时不限制条数；弱线索与噪音函数仍会 drop。
    ``max_function_chars <= 0`` 时不限制字符；must 始终保留。
    若传入 ``stats`` 字典，写入分桶计数。
    """
    root_cause_norm = root_cause_norm_ids or set()
    stack_norm = stack_frame_norm_ids or set()
    anchors = anchor_paths or set()
    unlimited = int(max_functions) <= 0
    cap = 0 if unlimited else max(4, min(int(max_functions), 64))
    char_cap = int(max_function_chars or 0)

    must_recs: List[Dict[str, Any]] = []
    should_recs: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []
    drop_count = 0

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
        bucket = _record_bucket(
            rec,
            stack_norm=stack_norm,
            root_cause_norm=root_cause_norm,
            anchor_paths=anchors,
        )
        rec_copy = dict(rec)
        rec_copy["_select_tier"] = tier
        rec_copy["_bucket"] = bucket
        if bucket == "must":
            must_recs.append(rec_copy)
        elif bucket == "should":
            should_recs.append(rec_copy)
        else:
            drop_count += 1
            excluded.append(rec_copy)

    def _sort_key(item: Dict[str, Any]) -> Tuple[int, int, str]:
        """must/should 装箱时的稳定排序键。"""
        return (
            int(item.get("_select_tier", 50)),
            int(item.get("priority", 99)),
            str((item.get("node") or {}).get("signature") or ""),
        )

    must_recs.sort(key=_sort_key)
    should_recs.sort(key=_sort_key)

    included: List[Dict[str, Any]] = []
    seen_norm: Set[str] = set()
    used_chars = 0
    must_included = 0
    should_included = 0

    def _append(rec: Dict[str, Any]) -> bool:
        """去重后纳入 included；重复 norm_id 返回 False。"""
        nf = str(rec.get("norm_id") or "")
        if nf and nf in seen_norm:
            return False
        if nf:
            seen_norm.add(nf)
        rec.pop("_select_tier", None)
        rec.pop("_bucket", None)
        included.append(rec)
        return True

    for rec in must_recs:
        extra = _snippet_char_len(rec)
        if _append(rec):
            used_chars += extra
            must_included += 1

    for rec in should_recs:
        extra = _snippet_char_len(rec)
        if not unlimited and len(included) >= cap:
            rec["_exclude_reason"] = "超出函数条数预算已裁剪"
            excluded.append(rec)
            continue
        if char_cap > 0 and used_chars + extra > char_cap:
            rec["_exclude_reason"] = "超出函数体字符预算已裁剪"
            excluded.append(rec)
            continue
        if _append(rec):
            used_chars += extra
            should_included += 1

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
        reason = str(rec.get("_exclude_reason") or "")
        if not reason:
            bucket = rec.get("_bucket")
            reason = "弱线索/噪音已裁剪" if bucket == "drop" else "置信度较低已裁剪"
        index_lines.append(f"- {sig}（来源: {tag_txt}；未纳入：{reason}）")

    if isinstance(stats, dict):
        stats["must_count"] = must_included
        stats["should_count"] = should_included
        stats["drop_count"] = drop_count
        stats["included_count"] = len(included)
        stats["excluded_count"] = len(excluded)
        stats["used_function_chars"] = used_chars
        stats["max_function_chars"] = char_cap

    return included, index_lines
