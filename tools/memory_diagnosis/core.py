#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内存压力 / OOM 诊断编排（阶段 A：crash 主轨旁路，产出 04d）。

不做独立 workflow；不依赖 heap snapshot。
从日志抽取内存线索 + 匹配 LEAK_FAULT_MODES 关键词。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_PSS_RE = re.compile(
    r"(?:PSS|Pss)\s*[:=]?\s*([\d.]+)\s*(K|KB|M|MB|G|GB)?",
    re.IGNORECASE,
)
_RSS_RE = re.compile(
    r"(?:RSS|Rss|VmRSS)\s*[:=]?\s*([\d.]+)\s*(K|KB|M|MB|G|GB)?",
    re.IGNORECASE,
)
_JAVA_HEAP_RE = re.compile(
    r"(?:Java\s+heap|Dalvik\s+Heap|Heap:\s*size)\s*[:=]?\s*([\d.]+)\s*(K|KB|M|MB|G|GB)?",
    re.IGNORECASE,
)
_NATIVE_HEAP_RE = re.compile(
    r"(?:Native\s+Heap|native\s+heap)\s*[:=]?\s*([\d.]+)\s*(K|KB|M|MB|G|GB)?",
    re.IGNORECASE,
)
_FD_RE = re.compile(
    r"(?:FD(?:s)?|file\s+descriptors?)\s*[:=]?\s*(\d+)",
    re.IGNORECASE,
)
_THREAD_COUNT_RE = re.compile(
    r"(?:Threads?|thread\s+count)\s*[:=]?\s*(\d+)",
    re.IGNORECASE,
)


def should_run_memory_analysis(
    parse_result: Dict[str, Any],
    *,
    force: bool = False,
) -> bool:
    """是否应执行内存压力/OOM 旁路诊断。"""
    if force:
        return True
    if not isinstance(parse_result, dict):
        return False
    meta = parse_result.get("meta_info") or {}
    if isinstance(meta, dict):
        try:
            from tools.crash_parser.log_kind_classifier import is_oom_family_kind
            if is_oom_family_kind(meta.get("log_kind")):
                return True
        except Exception:
            pass
        if meta.get("oom_suspected"):
            return True
    crash_info = parse_result.get("crash_info") or {}
    if not isinstance(crash_info, dict):
        return False
    category = str(crash_info.get("category") or "").lower()
    reason = str(crash_info.get("crash_reason") or "").lower()
    if category == "oom":
        return True
    if "oom" in reason or "out of memory" in reason or "outofmemory" in reason:
        return True
    return False


def run_memory_pressure_diagnosis(
    parse_result: Dict[str, Any],
    resolved_stack: Dict[str, Any],
    crash_log_content: str = "",
    *,
    force: bool = False,
) -> Optional[Dict[str, Any]]:
    """执行内存压力/OOM 诊断。

    Returns:
        可写入 ``04d_memory_pressure_diagnosis.json`` 的字典；不应跑则 None。
    """
    if not should_run_memory_analysis(parse_result, force=force):
        return None

    if not isinstance(parse_result, dict):
        parse_result = {}
    if not isinstance(resolved_stack, dict):
        resolved_stack = {}
    log_content = crash_log_content or str(parse_result.get("raw_content") or "")
    meta = parse_result.get("meta_info") if isinstance(parse_result.get("meta_info"), dict) else {}
    crash_info = parse_result.get("crash_info") if isinstance(parse_result.get("crash_info"), dict) else {}
    log_kind = str((meta or {}).get("log_kind") or "")

    indicators = _extract_memory_indicators(log_content)
    subtype = _classify_memory_subtype(log_kind, log_content, indicators, crash_info)
    fault_mode_matches = _match_leak_fault_modes(
        log_content,
        resolved_stack,
        parse_result,
        indicators,
    )
    prompt_section = _build_prompt_section(
        subtype=subtype,
        log_kind=log_kind,
        indicators=indicators,
        fault_mode_matches=fault_mode_matches,
        forced=force,
    )

    try:
        from tools.crash_parser.log_kind_classifier import is_oom_family_kind
        oom_flag = is_oom_family_kind(log_kind) or bool((meta or {}).get("oom_suspected"))
    except Exception:
        oom_flag = bool((meta or {}).get("oom_suspected"))

    return {
        "analyzed": True,
        "forced": bool(force),
        "log_kind": log_kind or None,
        "oom_suspected": oom_flag,
        "memory_subtype": subtype,
        "memory_indicators": indicators,
        "fault_mode_matches": fault_mode_matches,
        "prompt_section_zh": prompt_section,
        "skill": "memory-leak-analysis",
        "note_zh": (
            "阶段 A：基于日志线索的内存压力/OOM 旁路；"
            "非完整 heap snapshot 泄漏分析。OOM≠必然泄漏。"
        ),
    }


def _extract_memory_indicators(log_content: str) -> Dict[str, Any]:
    text = log_content or ""
    head = text[:20000]
    indicators: Dict[str, Any] = {
        "keywords_hit": [],
        "metrics": {},
    }
    kw_checks = [
        ("OutOfMemoryError", r"OutOfMemoryError"),
        ("jetsam", r"\bjetsam\b"),
        ("lowmemorykiller", r"lowmemorykiller|Low\s+Memory\s+Killer"),
        ("EXC_RESOURCE_MEMORY", r"EXC_RESOURCE[^\n]*MEMORY"),
        ("ENOMEM", r"\bENOMEM\b|Cannot\s+allocate\s+memory"),
        ("memory_pressure", r"memory\s+pressure|low[_ ]?memory|memory\s+warning"),
        ("trim_memory", r"onTrimMemory|didReceiveMemoryWarning"),
    ]
    for name, pat in kw_checks:
        if re.search(pat, head, re.IGNORECASE):
            indicators["keywords_hit"].append(name)

    for key, cre in (
        ("pss", _PSS_RE),
        ("rss", _RSS_RE),
        ("java_heap", _JAVA_HEAP_RE),
        ("native_heap", _NATIVE_HEAP_RE),
    ):
        m = cre.search(head)
        if m:
            indicators["metrics"][key] = {
                "value": m.group(1),
                "unit": (m.group(2) or "").upper() or None,
                "raw": m.group(0)[:80],
            }

    m_fd = _FD_RE.search(head)
    if m_fd:
        indicators["metrics"]["fd_count"] = {"value": m_fd.group(1), "unit": None}
    m_th = _THREAD_COUNT_RE.search(head)
    if m_th:
        indicators["metrics"]["thread_count_hint"] = {"value": m_th.group(1), "unit": None}

    return indicators


def _classify_memory_subtype(
    log_kind: str,
    log_content: str,
    indicators: Dict[str, Any],
    crash_info: Dict[str, Any],
) -> str:
    hits = set(indicators.get("keywords_hit") or [])
    if log_kind == "mixed_oom_crash":
        return "mixed_oom_with_crash"
    if "OutOfMemoryError" in hits:
        return "java_oom"
    if hits & {"jetsam", "lowmemorykiller", "EXC_RESOURCE_MEMORY"}:
        return "system_oom_kill"
    if "ENOMEM" in hits:
        return "native_alloc_failure"
    if log_kind == "oom_kill":
        return "oom_kill"
    if log_kind == "memory_pressure" or "memory_pressure" in hits or "trim_memory" in hits:
        return "memory_pressure"
    category = str(crash_info.get("category") or "").lower()
    if category == "oom":
        return "oom_category"
    return "memory_related"


def _match_leak_fault_modes(
    log_content: str,
    resolved_stack: Dict[str, Any],
    parse_result: Dict[str, Any],
    indicators: Dict[str, Any],
) -> List[Dict[str, Any]]:
    try:
        from skill_system.skill_templates.memory_leak_analysis import LEAK_FAULT_MODES
    except Exception:
        LEAK_FAULT_MODES = {}

    corpus_parts: List[str] = [log_content[:8000].lower()]
    for k in indicators.get("keywords_hit") or []:
        corpus_parts.append(str(k).lower())
    for th in (resolved_stack.get("resolved_threads") or [])[:5]:
        if not isinstance(th, dict):
            continue
        for fr in (th.get("frames") or [])[:12]:
            if isinstance(fr, dict):
                corpus_parts.append(str(fr.get("resolved_function") or fr.get("function") or "").lower())
                corpus_parts.append(str(fr.get("module") or "").lower())
    for th in (parse_result.get("threads") or [])[:5]:
        if not isinstance(th, dict):
            continue
        for fr in (th.get("frames") or [])[:8]:
            if isinstance(fr, dict):
                corpus_parts.append(str(fr.get("function") or "").lower())
    corpus = " ".join(corpus_parts)

    matches: List[Dict[str, Any]] = []
    for mode_id, mode in (LEAK_FAULT_MODES or {}).items():
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
            "score": round(min(1.0, 0.3 + 0.2 * best_hits), 2),
        })

    matches.sort(key=lambda m: float(m.get("score") or 0), reverse=True)
    return matches[:5]


def _build_prompt_section(
    *,
    subtype: str,
    log_kind: str,
    indicators: Dict[str, Any],
    fault_mode_matches: List[Dict[str, Any]],
    forced: bool,
) -> str:
    lines: List[str] = [
        "## 内存压力 / OOM 诊断辅助",
        "",
        "以下为日志侧内存线索（阶段 A 旁路）。"
        "注意：OOM/内存压力 ≠ 必然内存泄漏；"
        "完整泄漏定位需 heap snapshot / 增长曲线（尚未接入）。",
        "",
        f"- 子类型: {subtype}",
    ]
    if log_kind:
        lines.append(f"- log_kind: {log_kind}")
    if forced:
        lines.append("- （本次为强制内存分析）")
    lines.append("")

    hits = indicators.get("keywords_hit") or []
    if hits:
        lines.append("### 命中关键字")
        for h in hits:
            lines.append(f"- {h}")
        lines.append("")

    metrics = indicators.get("metrics") or {}
    if metrics:
        lines.append("### 日志中的内存指标（若有）")
        for k, v in metrics.items():
            if isinstance(v, dict):
                unit = v.get("unit") or ""
                lines.append(f"- {k}: {v.get('value')}{unit}")
            else:
                lines.append(f"- {k}: {v}")
        lines.append("")

    if fault_mode_matches:
        lines.append("### 初步泄漏模式匹配（关键词，弱证据）")
        for m in fault_mode_matches[:3]:
            lines.append(
                f"- [{m.get('mode_id')}] {m.get('root_cause_l1')} / {m.get('root_cause_l2')} / "
                f"{m.get('root_cause_l3')}（匹配度 {float(m.get('score') or 0):.0%}，"
                f"关键词: {', '.join(m.get('matched_keywords') or [])}）"
            )
        lines.append("")

    lines.extend([
        "### 分析建议",
        "- 若为系统杀进程（jetsam/LMK）：优先查峰值内存与系统策略，而非单次空指针",
        "- 若为 OutOfMemoryError：区分 Java heap / Native heap，查大对象与缓存上限",
        "- 若为 mixed_oom_crash：崩溃可能是耗尽后的次生故障，根因仍可能在内存",
        "- 缺 heap dump 时明确写「无法定位泄漏源，仅能判断内存压力/OOM 形态」",
        "",
    ])
    return "\n".join(lines).rstrip() + "\n"
