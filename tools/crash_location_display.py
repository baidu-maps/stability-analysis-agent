#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""crash_location：内部英文枚举与 03 落盘 location_type 中文展示。"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

# 内部逻辑用英文枚举（勿改字符串，避免破坏分支判断）
STATUS_RESOLVED_TO_BUSINESS_FRAME = "resolved_to_business_frame"
STATUS_UNRESOLVED_CRASH_THREAD_NO_BUSINESS = "unresolved_crash_thread_no_business_frame"
STATUS_UNRESOLVED_OR_INDIRECT = "unresolved_or_indirect"

SOURCE_FROM_ADD2LINE = "from_add2line"
SOURCE_FROM_LOG_DEDUCE = "from_log_deduce"
SOURCE_UNRESOLVED_CRASH_THREAD_NO_BUSINESS = "unresolved_crash_thread_no_business_frame"

# 03 JSON 展示：完整中文句（compat 靠全文匹配）
LOCATION_TYPE_ZH_RESOLVED_ADD2LINE = (
    "日志中标记的崩溃线程已通过符号化栈定位到业务库源码行。"
)
LOCATION_TYPE_ZH_LOG_DEDUCE = (
    "已关联到工程内的崩溃函数，但未精确定位到 file:line 级行号，"
    "请以该函数源码为主分析。"
)
LOCATION_TYPE_ZH_UNRESOLVED_NO_BUSINESS = (
    "日志标记的崩溃线程栈帧未命中你提供的库目录，"
    "无法在该线程上确认崩溃源码行。"
)
LOCATION_TYPE_ZH_LIMITED = (
    "崩溃点定位信息有限，请结合下文函数源码与调用链分析。"
)

# 05 弱归因摘要：结论 + 行动暗示（勿与 03 location_type 混用）
CRASH_POSITION_PROMPT_ZH_WEAK = (
    "结论：无法在日志崩溃线程上确定崩溃源码行；"
    "该线程当前只能视为崩溃承载线程/现象线程，不能作为可分析的业务源码入口；"
    "不要围绕主线程入口函数继续请求源码或猜测根因，"
    "请优先分析下文其它包含业务帧的线程是否存在跨线程影响、异步任务或对象生命周期问题。"
)

_ALL_LOCATION_TYPE_ZH = (
    LOCATION_TYPE_ZH_RESOLVED_ADD2LINE,
    LOCATION_TYPE_ZH_LOG_DEDUCE,
    LOCATION_TYPE_ZH_UNRESOLVED_NO_BUSINESS,
    LOCATION_TYPE_ZH_LIMITED,
)


def _has_crash_line(cs: Dict[str, Any]) -> bool:
    try:
        line_no = int(cs.get("crash_line_number") or 0)
    except (TypeError, ValueError):
        return False
    return line_no > 0 and bool(str(cs.get("crash_line_code") or "").strip())


def display_location_type(
    status: Any,
    source: Any,
    *,
    crash_summary: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """内部 status/source → 03 crash_location.location_type。"""
    st = str(status or "").strip()
    src = str(source or "").strip()
    cs = crash_summary if isinstance(crash_summary, dict) else {}

    if (
        st == STATUS_UNRESOLVED_CRASH_THREAD_NO_BUSINESS
        or src == SOURCE_UNRESOLVED_CRASH_THREAD_NO_BUSINESS
    ):
        return LOCATION_TYPE_ZH_UNRESOLVED_NO_BUSINESS
    if src == SOURCE_FROM_LOG_DEDUCE:
        return LOCATION_TYPE_ZH_LOG_DEDUCE
    if st == STATUS_UNRESOLVED_OR_INDIRECT:
        return LOCATION_TYPE_ZH_LIMITED
    if st == STATUS_RESOLVED_TO_BUSINESS_FRAME and src == SOURCE_FROM_ADD2LINE:
        return LOCATION_TYPE_ZH_RESOLVED_ADD2LINE
    if src == SOURCE_FROM_ADD2LINE and _has_crash_line(cs):
        return LOCATION_TYPE_ZH_RESOLVED_ADD2LINE
    if st == STATUS_RESOLVED_TO_BUSINESS_FRAME:
        return LOCATION_TYPE_ZH_RESOLVED_ADD2LINE
    if src == SOURCE_FROM_ADD2LINE:
        return LOCATION_TYPE_ZH_RESOLVED_ADD2LINE
    return LOCATION_TYPE_ZH_LIMITED


def normalize_location_type(value: Any) -> Tuple[Optional[str], Optional[str]]:
    """03 location_type（或旧 status/source）→ (attributed_status, crash_location_source)。"""
    raw = str(value or "").strip()
    if not raw:
        return None, None
    if raw == LOCATION_TYPE_ZH_RESOLVED_ADD2LINE:
        return STATUS_RESOLVED_TO_BUSINESS_FRAME, SOURCE_FROM_ADD2LINE
    if raw == LOCATION_TYPE_ZH_LOG_DEDUCE:
        return STATUS_RESOLVED_TO_BUSINESS_FRAME, SOURCE_FROM_LOG_DEDUCE
    if raw == LOCATION_TYPE_ZH_UNRESOLVED_NO_BUSINESS:
        return (
            STATUS_UNRESOLVED_CRASH_THREAD_NO_BUSINESS,
            SOURCE_UNRESOLVED_CRASH_THREAD_NO_BUSINESS,
        )
    if raw == LOCATION_TYPE_ZH_LIMITED:
        return STATUS_UNRESOLVED_OR_INDIRECT, ""
    if raw in (
        STATUS_RESOLVED_TO_BUSINESS_FRAME,
        STATUS_UNRESOLVED_CRASH_THREAD_NO_BUSINESS,
        STATUS_UNRESOLVED_OR_INDIRECT,
    ):
        return raw, ""
    if raw in (
        SOURCE_FROM_ADD2LINE,
        SOURCE_FROM_LOG_DEDUCE,
        SOURCE_UNRESOLVED_CRASH_THREAD_NO_BUSINESS,
    ):
        if raw == SOURCE_UNRESOLVED_CRASH_THREAD_NO_BUSINESS:
            return STATUS_UNRESOLVED_CRASH_THREAD_NO_BUSINESS, raw
        if raw == SOURCE_FROM_LOG_DEDUCE:
            return STATUS_RESOLVED_TO_BUSINESS_FRAME, raw
        return STATUS_RESOLVED_TO_BUSINESS_FRAME, raw
    return None, None


def format_crash_position_summary_line(
    crash_summary: Dict[str, Any],
    crash_node: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """05 崩溃摘要：合并「业务库帧 / 源码行」为单行（弱归因用结论句，非 03 location_type）。"""
    if not isinstance(crash_summary, dict):
        return None

    from tools.analysis_entry_display import is_investigation_hint_attribution

    crash_loc = crash_summary.get("crash_location")
    loc_type: Optional[str] = None
    if isinstance(crash_loc, dict):
        loc_type = str(crash_loc.get("location_type") or "").strip() or None
    if not loc_type:
        loc_type = display_location_type(
            crash_summary.get("attributed_crash_location_status"),
            crash_summary.get("crash_location_source"),
            crash_summary=crash_summary,
        )

    if is_investigation_hint_attribution(crash_summary):
        return CRASH_POSITION_PROMPT_ZH_WEAK

    source = str(
        (crash_loc.get("source") if isinstance(crash_loc, dict) else None)
        or crash_summary.get("crash_location_source")
        or ""
    ).strip()
    node = crash_node if isinstance(crash_node, dict) else {}
    func = ""
    file_path = ""
    line_no = crash_summary.get("crash_line_number")
    code = str(
        crash_summary.get("crash_line_code")
        or crash_summary.get("analysis_entry_line_code")
        or ""
    ).strip()
    if isinstance(crash_loc, dict):
        func = str(crash_loc.get("function") or "").strip()
        file_path = str(crash_loc.get("file") or "").strip()
        if crash_loc.get("line") is not None:
            line_no = crash_loc.get("line")
        if crash_loc.get("code"):
            code = str(crash_loc.get("code")).strip()
    func = str(
        func
        or crash_summary.get("analysis_entry_function")
        or (node.get("signature") if node else "")
        or ""
    ).strip()
    file_path = str(
        file_path
        or crash_summary.get("analysis_entry_file")
        or (node.get("file") if node else "")
        or ""
    ).strip()
    if line_no is None:
        line_no = crash_summary.get("analysis_entry_line_number")

    if source == SOURCE_FROM_LOG_DEDUCE:
        if func and file_path:
            return (
                f"日志崩溃线程上的崩溃位置: {func} ({file_path})"
                "（未精确定位到 file:line 级行号）"
            )
        return loc_type or LOCATION_TYPE_ZH_LOG_DEDUCE

    try:
        line_int = int(line_no or 0)
    except (TypeError, ValueError):
        line_int = 0

    if line_int > 0 and (file_path or func):
        loc = f"{file_path}:{line_int}" if file_path else str(line_int)
        text = f"日志崩溃线程上的崩溃位置: {func or 'N/A'} ({loc})"
        if code:
            text += f" — `{code}`"
        return text

    return loc_type


def resolve_crash_location_status_source(
    crash_location: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str]]:
    """从 v2 crash_location 解析内部 status/source（location_type 优先）。"""
    if not isinstance(crash_location, dict):
        return None, None
    location_type = crash_location.get("location_type")
    st, src = normalize_location_type(location_type)
    if st:
        if not src and location_type == LOCATION_TYPE_ZH_LIMITED:
            legacy_src = str(crash_location.get("source") or "").strip()
            if legacy_src:
                src = legacy_src
        return st, src or None
    st = str(crash_location.get("status") or "").strip() or None
    src = str(crash_location.get("source") or "").strip() or st
    if st in _ALL_LOCATION_TYPE_ZH:
        return normalize_location_type(st)
    if src in _ALL_LOCATION_TYPE_ZH:
        return normalize_location_type(src)
    return st, src
