#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""analysis_entry：内部英文枚举与 03 落盘 entry_type 中文展示。"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

# 内部逻辑用英文枚举（勿改字符串，避免破坏分支判断）
CONFIDENCE_DIRECT_CRASH_THREAD = "direct_crash_thread"
CONFIDENCE_INVESTIGATION_HINT = "investigation_hint"

SOURCE_CRASH_THREAD_BUSINESS_FRAME = "crash_thread_business_frame"
SOURCE_RESOLVED_BUSINESS_THREAD = "resolved_business_thread"
SOURCE_SELECTED_STACK_FRAME = "selected_stack_frame"

# 03 JSON 展示：两句完整说明（勿改措辞，compat 靠全文匹配）
ENTRY_TYPE_ZH_DIRECT = (
    "当前源码来自日志里标为崩溃的那条线程，且栈帧命中你提供的库目录。"
)
ENTRY_TYPE_ZH_ALTERNATE = (
    "日志标记的崩溃线程栈帧未命中你提供的库目录，"
    "改从其它已符号化业务线程取的入口，不能当作确定崩溃点。"
)

# 旧版 03 曾用 confidence/source 短中文标签（仅 compat 读旧报告）
_CONFIDENCE_ZH: Dict[str, str] = {
    CONFIDENCE_DIRECT_CRASH_THREAD: "归因崩溃线程直接入口",
    CONFIDENCE_INVESTIGATION_HINT: "跨线程排查线索",
}

_SOURCE_ZH: Dict[str, str] = {
    SOURCE_CRASH_THREAD_BUSINESS_FRAME: "归因崩溃线程业务库帧",
    SOURCE_RESOLVED_BUSINESS_THREAD: "其它已符号化业务线程",
    SOURCE_SELECTED_STACK_FRAME: "堆栈选定帧（线程未明确）",
}

_CONFIDENCE_ZH_TO_EN = {v: k for k, v in _CONFIDENCE_ZH.items()}
_SOURCE_ZH_TO_EN = {v: k for k, v in _SOURCE_ZH.items()}


def display_entry_type(confidence: Any, source: Any) -> Optional[str]:
    """内部 confidence/source → 03 analysis_entry.entry_type。"""
    conf = str(confidence or "").strip()
    src = str(source or "").strip()
    if (
        conf == CONFIDENCE_DIRECT_CRASH_THREAD
        and src == SOURCE_CRASH_THREAD_BUSINESS_FRAME
    ):
        return ENTRY_TYPE_ZH_DIRECT
    if conf == CONFIDENCE_INVESTIGATION_HINT and src in (
        SOURCE_RESOLVED_BUSINESS_THREAD,
        SOURCE_SELECTED_STACK_FRAME,
    ):
        return ENTRY_TYPE_ZH_ALTERNATE
    if conf == CONFIDENCE_DIRECT_CRASH_THREAD:
        return ENTRY_TYPE_ZH_DIRECT
    if conf == CONFIDENCE_INVESTIGATION_HINT:
        return ENTRY_TYPE_ZH_ALTERNATE
    return None


def normalize_entry_type(value: Any) -> Tuple[Optional[str], Optional[str]]:
    """03 entry_type（或旧短中文）→ (confidence, source)。"""
    raw = str(value or "").strip()
    if not raw:
        return None, None
    if raw == ENTRY_TYPE_ZH_DIRECT:
        return CONFIDENCE_DIRECT_CRASH_THREAD, SOURCE_CRASH_THREAD_BUSINESS_FRAME
    if raw == ENTRY_TYPE_ZH_ALTERNATE:
        return CONFIDENCE_INVESTIGATION_HINT, SOURCE_RESOLVED_BUSINESS_THREAD
    conf = normalize_analysis_entry_confidence(raw)
    if conf:
        src = normalize_analysis_entry_source(raw)
        if conf == CONFIDENCE_DIRECT_CRASH_THREAD:
            return conf, SOURCE_CRASH_THREAD_BUSINESS_FRAME
        if conf == CONFIDENCE_INVESTIGATION_HINT:
            return conf, src or SOURCE_RESOLVED_BUSINESS_THREAD
    src = normalize_analysis_entry_source(raw)
    if src:
        if src == SOURCE_CRASH_THREAD_BUSINESS_FRAME:
            return CONFIDENCE_DIRECT_CRASH_THREAD, src
        return CONFIDENCE_INVESTIGATION_HINT, src
    return None, None


def display_analysis_entry_confidence(value: Any) -> Optional[str]:
    """英文枚举 → 旧版短中文（仅读旧 03）。"""
    key = str(value or "").strip()
    if not key:
        return None
    return _CONFIDENCE_ZH.get(key, key)


def display_analysis_entry_source(value: Any) -> Optional[str]:
    """英文枚举 → 旧版短中文（仅读旧 03）。"""
    key = str(value or "").strip()
    if not key:
        return None
    return _SOURCE_ZH.get(key, key)


def normalize_analysis_entry_confidence(value: Any) -> Optional[str]:
    """03 JSON（中/英）→ 内部英文 confidence。"""
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw in _CONFIDENCE_ZH:
        return raw
    return _CONFIDENCE_ZH_TO_EN.get(raw, raw)


def normalize_analysis_entry_source(value: Any) -> Optional[str]:
    """03 JSON（中/英）→ 内部英文 source。"""
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw in _SOURCE_ZH:
        return raw
    return _SOURCE_ZH_TO_EN.get(raw, raw)


def is_investigation_hint_attribution(cs: Any) -> bool:
    """弱归因（跨线程 / 崩溃线程无业务库帧）：不向 LLM 指定分析入口。"""
    if not isinstance(cs, dict):
        return False
    if cs.get("selected_analysis_is_crash_thread") is False:
        return True
    from tools.crash_location_display import (
        LOCATION_TYPE_ZH_UNRESOLVED_NO_BUSINESS,
        STATUS_UNRESOLVED_CRASH_THREAD_NO_BUSINESS,
    )

    if (
        cs.get("attributed_crash_location_status")
        == STATUS_UNRESOLVED_CRASH_THREAD_NO_BUSINESS
    ):
        return True
    crash_location = cs.get("crash_location")
    if isinstance(crash_location, dict):
        if crash_location.get("location_type") == LOCATION_TYPE_ZH_UNRESOLVED_NO_BUSINESS:
            return True
    return False


def should_emit_crash_location_coordinates_in_03(cs: Any) -> bool:
    """03 仅强归因（direct_crash_thread）时在 crash_location 写入完整坐标。"""
    if not isinstance(cs, dict):
        return False
    return cs.get("selected_analysis_confidence") == CONFIDENCE_DIRECT_CRASH_THREAD


def should_include_analysis_entry_in_03(cs: Any) -> bool:
    """已废弃：方案 A 后 03 不再落盘 analysis_entry，请用 should_emit_crash_location_coordinates_in_03。"""
    return should_emit_crash_location_coordinates_in_03(cs)


def resolve_analysis_entry_confidence_source(
    analysis_entry: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str]]:
    """从 v2 analysis_entry 解析内部 confidence/source（entry_type 优先）。"""
    if not isinstance(analysis_entry, dict):
        return None, None
    entry_type = analysis_entry.get("entry_type")
    conf, src = normalize_entry_type(entry_type)
    if conf:
        return conf, src
    conf = normalize_analysis_entry_confidence(analysis_entry.get("confidence"))
    src = normalize_analysis_entry_source(analysis_entry.get("source"))
    return conf, src
