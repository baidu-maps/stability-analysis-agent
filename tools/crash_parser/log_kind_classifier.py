#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""稳定性日志类型强分类（log_kind）。

用于 CLI / workflow 路由：Crash vs ANR/Freeze vs mixed；
OOM/内存压力仍走 crash 主轨，旁路产出 04d。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 固定枚举
LOG_KIND_NATIVE_CRASH = "native_crash"
LOG_KIND_JAVA_CRASH = "java_crash"
LOG_KIND_ANR_TRACE = "anr_trace"
LOG_KIND_APP_FREEZE = "app_freeze"
LOG_KIND_WATCHDOG = "watchdog"
LOG_KIND_MIXED_ANR_CRASH = "mixed_anr_crash"
LOG_KIND_OOM_KILL = "oom_kill"
LOG_KIND_MEMORY_PRESSURE = "memory_pressure"
LOG_KIND_MIXED_OOM_CRASH = "mixed_oom_crash"
LOG_KIND_UNKNOWN = "unknown"

ANR_FAMILY_KINDS = frozenset({
    LOG_KIND_ANR_TRACE,
    LOG_KIND_APP_FREEZE,
    LOG_KIND_WATCHDOG,
    LOG_KIND_MIXED_ANR_CRASH,
})

OOM_FAMILY_KINDS = frozenset({
    LOG_KIND_OOM_KILL,
    LOG_KIND_MEMORY_PRESSURE,
    LOG_KIND_MIXED_OOM_CRASH,
})

_CRASH_SIGNAL_RE = re.compile(
    r"(?:"
    r"(?<![A-Za-z0-9])SIG(?:SEGV|ABRT|BUS|ILL|FPE|TRAP)(?![A-Za-z0-9])"
    r"|Fatal\s+signal"
    r"|EXC_BAD_ACCESS"
    r"|EXC_CRASH"
    r"|tombstone"
    r"|Segmentation\s+fault"
    r"|Abort\s+message"
    r"|崩溃类型\s*[:：].*SIG(?:SEGV|ABRT|BUS|ILL|FPE|TRAP)"
    r")",
    re.IGNORECASE,
)

_JAVA_FATAL_RE = re.compile(r"FATAL\s+EXCEPTION", re.IGNORECASE)
_APPFREEZE_RE = re.compile(r"appfreeze|AppFreeze", re.IGNORECASE)
_WATCHDOG_RE = re.compile(r"\bwatchdog\b|RBSAssertionReliability", re.IGNORECASE)
_ANR_KW_RE = re.compile(r"\bANR\b|am_anr(?:_info)?\b", re.IGNORECASE)

# 强 OOM 杀进程 / 明确 OOM 异常
_OOM_KILL_RE = re.compile(
    r"(?:"
    r"OutOfMemoryError"
    r"|out\s+of\s+memory"
    r"|lowmemorykiller"
    r"|Low\s+Memory\s+Killer"
    r"|\bjetsam\b"
    r"|EXC_RESOURCE[^\n]*MEMORY"
    r"|killed\s+due\s+to\s+memory"
    r"|Cannot\s+allocate\s+memory"
    r"|ENOMEM"
    r"|oom[_ -]?killer"
    r"|Memory\s+pressure\s+event.*kill"
    r")",
    re.IGNORECASE,
)

# 软内存压力（未必已杀进程）
_MEMORY_PRESSURE_RE = re.compile(
    r"(?:"
    r"memory\s+pressure"
    r"|low[_ ]?memory"
    r"|memory\s+warning"
    r"|didReceiveMemoryWarning"
    r"|onTrimMemory"
    r"|GC[_\s]?ALLOC"
    r"|Allocations?\s+failed"
    r"|heap\s+(?:limit|full|exhausted)"
    r"|PSS\s*[:=]"
    r"|RSS\s*[:=]"
    r"|Java\s+heap\s*[:=]"
    r"|native\s+heap\s*[:=]"
    r")",
    re.IGNORECASE,
)

_NATIVE_CRASH_HINT_RE = re.compile(
    r"(?:"
    r"\*\*\*\s*\*\*\*\s*Fatal\s+signal"
    r"|Build\s+fingerprint:"
    r"|Crash\s+reason:"
    r"|Exception\s+Type:"
    r"|Crashed\s+Thread:"
    r"|#00\s+pc\s+"
    r")",
    re.IGNORECASE,
)


@dataclass
class LogKindResult:
    log_kind: str
    confidence: float
    reasons: List[str] = field(default_factory=list)

    def to_meta_fields(self) -> Dict[str, Any]:
        return {
            "log_kind": self.log_kind,
            "log_kind_confidence": round(self.confidence, 3),
            "log_kind_reasons": list(self.reasons),
            "anr_suspected": True if self.log_kind in ANR_FAMILY_KINDS else None,
            "oom_suspected": True if self.log_kind in OOM_FAMILY_KINDS else None,
        }


def classify_log_kind(content: str) -> LogKindResult:
    """对日志原文做强分类（只读，不依赖全量 parse）。"""
    text = content or ""
    if not text.strip():
        return LogKindResult(LOG_KIND_UNKNOWN, 0.0, ["empty_content"])

    head = text[:12000]
    reasons: List[str] = []

    has_appfreeze = bool(_APPFREEZE_RE.search(head))
    has_watchdog = bool(_WATCHDOG_RE.search(head))
    has_anr_kw = bool(_ANR_KW_RE.search(head))
    has_crash_signal = bool(_CRASH_SIGNAL_RE.search(head))
    has_java_fatal = bool(_JAVA_FATAL_RE.search(head))
    has_oom_kill = bool(_OOM_KILL_RE.search(head))
    has_mem_pressure = bool(_MEMORY_PRESSURE_RE.search(head))

    anr_family = False
    family_kind = LOG_KIND_ANR_TRACE
    family_conf = 0.0

    if has_appfreeze:
        anr_family = True
        family_kind = LOG_KIND_APP_FREEZE
        family_conf = 0.95
        reasons.append("keyword:AppFreeze")
    elif has_watchdog:
        anr_family = True
        family_kind = LOG_KIND_WATCHDOG
        family_conf = 0.9
        reasons.append("keyword:watchdog")
    elif has_anr_kw:
        anr_family = True
        family_kind = LOG_KIND_ANR_TRACE
        family_conf = 0.92
        reasons.append("keyword:ANR")

    # Android traces 启发式（无显式 ANR 字样时）
    if not anr_family:
        try:
            from tools.crash_parser.android import _android_heuristic_anr_stack
            if _android_heuristic_anr_stack(text):
                anr_family = True
                family_kind = LOG_KIND_ANR_TRACE
                family_conf = 0.75
                reasons.append("android_traces_heuristic")
        except Exception:
            pass

    if anr_family and has_crash_signal:
        reasons.append("crash_signal_with_anr_family")
        return LogKindResult(LOG_KIND_MIXED_ANR_CRASH, min(0.95, family_conf + 0.05), reasons)

    if anr_family:
        return LogKindResult(family_kind, family_conf, reasons)

    # --- OOM / 内存压力（仍路由 crash workflow，旁路 04d）---
    if has_oom_kill:
        reasons.append("keyword:oom_kill")
        # 明确 OOM + 典型 native crash 信号 → 混合（崩溃可能是内存耗尽后的次生）
        if has_crash_signal and not re.search(r"OutOfMemoryError", head, re.I):
            # SIGSEGV 等 + jetsam/LMK 同现
            reasons.append("crash_signal_with_oom")
            return LogKindResult(LOG_KIND_MIXED_OOM_CRASH, 0.9, reasons)
        if has_crash_signal and re.search(r"OutOfMemoryError", head, re.I):
            # Java OOM 本身也常带 FATAL EXCEPTION，不算 mixed crash
            pass
        return LogKindResult(LOG_KIND_OOM_KILL, 0.92, reasons)

    if has_mem_pressure and has_crash_signal:
        reasons.append("keyword:memory_pressure")
        reasons.append("crash_signal_with_memory_pressure")
        return LogKindResult(LOG_KIND_MIXED_OOM_CRASH, 0.8, reasons)

    if has_mem_pressure:
        reasons.append("keyword:memory_pressure")
        return LogKindResult(LOG_KIND_MEMORY_PRESSURE, 0.75, reasons)

    if has_java_fatal and not has_crash_signal:
        reasons.append("keyword:FATAL_EXCEPTION")
        return LogKindResult(LOG_KIND_JAVA_CRASH, 0.88, reasons)

    if has_java_fatal and has_crash_signal:
        # JNI 混合：仍归 native/java crash 侧，走 crash workflow
        reasons.append("java_fatal_with_native_signal")
        return LogKindResult(LOG_KIND_NATIVE_CRASH, 0.7, reasons)

    if has_crash_signal or _NATIVE_CRASH_HINT_RE.search(head):
        if has_crash_signal:
            reasons.append("crash_signal_or_tombstone")
        else:
            reasons.append("native_crash_layout_hint")
        return LogKindResult(LOG_KIND_NATIVE_CRASH, 0.85 if has_crash_signal else 0.65, reasons)

    reasons.append("no_strong_markers")
    return LogKindResult(LOG_KIND_UNKNOWN, 0.3, reasons)


def is_anr_family_kind(log_kind: Optional[str]) -> bool:
    return str(log_kind or "") in ANR_FAMILY_KINDS


def is_oom_family_kind(log_kind: Optional[str]) -> bool:
    return str(log_kind or "") in OOM_FAMILY_KINDS


def workflow_name_for_log_kind(
    log_kind: str,
    *,
    force_anr: bool = False,
    confidence: float = 1.0,
) -> str:
    """CLI 预路由：返回 registry workflow name。

    OOM 族仍走 crash_analysis（阶段 A：旁路 04d，无独立 workflow）。

    改进：对 mixed_anr_crash，如果置信度较低（<0.80），优先走 crash_analysis
    并在 crash workflow 内以旁路方式执行 ANR 诊断，避免主次颠倒。
    """
    if force_anr:
        return "anr_freeze_analysis"
    if is_anr_family_kind(log_kind):
        # mixed_anr_crash 且置信度低时，优先 crash workflow
        # 因为可能是 native crash 导致的 ANR（crash 先于 ANR）
        if log_kind == LOG_KIND_MIXED_ANR_CRASH and confidence < 0.80:
            return "crash_analysis"
        return "anr_freeze_analysis"
    return "crash_analysis"


def log_kind_from_parse_result(parse_result: Dict[str, Any]) -> str:
    meta = parse_result.get("meta_info") if isinstance(parse_result, dict) else None
    if isinstance(meta, dict) and meta.get("log_kind"):
        return str(meta.get("log_kind"))
    return LOG_KIND_UNKNOWN
