#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ANR/Freeze 诊断编排：热点栈 + EventHandler/Binder + 故障模式初匹配。"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def should_run_anr_analysis(
    parse_result: Dict[str, Any],
    *,
    force: bool = False,
) -> bool:
    """是否应执行 ANR/Freeze 诊断（非普通 crash 默认路径）。

    优先读 ``meta_info.log_kind``；``anr_suspected`` 仅作兼容回退。
    """
    if force:
        return True
    if not isinstance(parse_result, dict):
        return False
    meta = parse_result.get("meta_info") or {}
    if isinstance(meta, dict):
        try:
            from tools.crash_parser.log_kind_classifier import is_anr_family_kind
            if is_anr_family_kind(meta.get("log_kind")):
                return True
        except Exception:
            pass
        if meta.get("anr_suspected"):
            return True
    crash_info = parse_result.get("crash_info") or {}
    if not isinstance(crash_info, dict):
        return False
    category = str(crash_info.get("category") or "").lower()
    reason = str(crash_info.get("crash_reason") or "").lower()
    if "anr" in category or "freeze" in category:
        return True
    if "anr" in reason or "appfreeze" in reason or "watchdog" in reason:
        return True
    return False


def run_anr_freeze_diagnosis(
    parse_result: Dict[str, Any],
    resolved_stack: Dict[str, Any],
    crash_log_content: str = "",
    *,
    force: bool = False,
) -> Optional[Dict[str, Any]]:
    """执行 ANR/Freeze 诊断。

    Returns:
        可写入 ``04c_anr_freeze_diagnosis.json`` 的字典；若不应跑则返回 None。
    """
    if not should_run_anr_analysis(parse_result, force=force):
        return None

    if not isinstance(parse_result, dict):
        parse_result = {}
    if not isinstance(resolved_stack, dict):
        resolved_stack = {}
    log_content = crash_log_content or str(parse_result.get("raw_content") or "")

    hotspot_raw: Optional[Dict[str, Any]] = None
    hotspot_md = ""
    try:
        from tools.stack_hotspot_analyzer import StackHotspotAnalyzer
        hotspot = StackHotspotAnalyzer().analyze(resolved_stack, parse_result)
        hotspot_raw = hotspot.to_dict()
        hotspot_md = hotspot.render_markdown() or ""
    except Exception as exc:
        logger.warning("StackHotspotAnalyzer failed: %s", exc)
        hotspot_raw = {"error": str(exc)}

    event_raw: Optional[Dict[str, Any]] = None
    event_md = ""
    binder_raw: Optional[Dict[str, Any]] = None
    binder_md = ""
    if log_content.strip():
        try:
            from tools.event_handler_analyzer import EventHandlerAnalyzer, BinderChainTracer
            eh = EventHandlerAnalyzer().parse_from_log(log_content)
            event_raw = eh.to_dict()
            event_md = eh.render_markdown() or ""
            binder = BinderChainTracer().trace_from_log(log_content)
            binder_raw = binder.to_dict()
            binder_md = binder.render_markdown() or ""
        except Exception as exc:
            logger.debug("EventHandler/Binder analysis skipped: %s", exc)

    fault_mode_matches = _match_anr_fault_modes(
        hotspot_raw or {},
        event_raw or {},
        binder_raw or {},
        log_content,
    )

    prompt_section = _build_prompt_section(
        hotspot_md=hotspot_md,
        event_md=event_md,
        binder_md=binder_md,
        fault_mode_matches=fault_mode_matches,
        forced=force,
    )

    meta = parse_result.get("meta_info") if isinstance(parse_result.get("meta_info"), dict) else {}
    log_kind = str((meta or {}).get("log_kind") or "")
    try:
        from tools.crash_parser.log_kind_classifier import is_anr_family_kind
        anr_flag = is_anr_family_kind(log_kind) or bool((meta or {}).get("anr_suspected"))
    except Exception:
        anr_flag = bool((meta or {}).get("anr_suspected"))
    return {
        "analyzed": True,
        "forced": bool(force),
        "log_kind": log_kind or None,
        "anr_suspected": anr_flag,
        "stack_hotspots": hotspot_raw,
        "event_handler": event_raw,
        "binder_chain": binder_raw,
        "fault_mode_matches": fault_mode_matches,
        "prompt_section_zh": prompt_section,
        "skill": "anr-freeze-analysis",
    }


def _match_anr_fault_modes(
    hotspot: Dict[str, Any],
    event: Dict[str, Any],
    binder: Dict[str, Any],
    log_content: str,
) -> List[Dict[str, Any]]:
    """基于热点/阻塞/Binder 对 ANR_FAULT_MODES 做轻量关键词匹配。"""
    try:
        from skill_system.skill_templates.anr_freeze_analysis import ANR_FAULT_MODES
    except Exception:
        ANR_FAULT_MODES = {}

    corpus_parts: List[str] = [log_content[:8000].lower()]
    for h in (hotspot.get("hotspot_functions") or [])[:15]:
        if isinstance(h, dict):
            corpus_parts.append(str(h.get("function") or "").lower())
            corpus_parts.append(str(h.get("module") or "").lower())
    for ind in hotspot.get("blocking_indicators") or []:
        corpus_parts.append(str(ind).lower())
    if event.get("blocking_cause"):
        corpus_parts.append(str(event.get("blocking_cause")).lower())
    if binder.get("has_deadlock"):
        corpus_parts.append("deadlock binder mutex lock")
    corpus = " ".join(corpus_parts)

    matches: List[Dict[str, Any]] = []
    for mode_id, mode in (ANR_FAULT_MODES or {}).items():
        if not isinstance(mode, dict):
            continue
        best_sub = None
        best_hits = 0
        hit_keywords: List[str] = []
        for sub in mode.get("sub_causes") or []:
            if not isinstance(sub, dict):
                continue
            kws = [str(k).lower() for k in (sub.get("keywords") or []) if k]
            hits = [k for k in kws if k in corpus]
            if len(hits) > best_hits:
                best_hits = len(hits)
                best_sub = sub
                hit_keywords = hits
        if best_hits <= 0:
            continue
        matches.append({
            "mode_id": mode_id,
            "root_cause_l1": mode.get("root_cause_l1"),
            "root_cause_l2": mode.get("root_cause_l2"),
            "root_cause_l3": (best_sub or {}).get("root_cause_l3"),
            "matched_keywords": hit_keywords,
            "score": round(min(1.0, 0.35 + 0.2 * best_hits), 2),
        })

    matches.sort(key=lambda m: float(m.get("score") or 0), reverse=True)
    return matches[:5]


def _build_prompt_section(
    *,
    hotspot_md: str,
    event_md: str,
    binder_md: str,
    fault_mode_matches: List[Dict[str, Any]],
    forced: bool,
) -> str:
    lines: List[str] = [
        "## ANR/Freeze 诊断辅助",
        "",
        "以下由确定性工具产出，用于冻屏/无响应分析；普通 crash 根因仍以崩溃帧与寄存器为准。",
    ]
    if forced:
        lines.append("（本次为强制 ANR 分析）")
    lines.append("")

    if hotspot_md.strip():
        lines.append(hotspot_md.strip())
        lines.append("")
    if event_md.strip():
        lines.append(event_md.strip())
        lines.append("")
    if binder_md.strip():
        lines.append(binder_md.strip())
        lines.append("")

    if fault_mode_matches:
        lines.append("### 初步故障模式匹配")
        for m in fault_mode_matches[:3]:
            lines.append(
                f"- [{m.get('mode_id')}] {m.get('root_cause_l1')} / {m.get('root_cause_l2')} / "
                f"{m.get('root_cause_l3')}（匹配度 {float(m.get('score') or 0):.0%}，"
                f"关键词: {', '.join(m.get('matched_keywords') or [])}）"
            )
        lines.append("")

    if len(lines) <= 4:
        lines.append("（未提取到有效热点/队列/Binder 证据，请结合多线程栈人工判断）")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
