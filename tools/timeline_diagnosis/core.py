#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""崩溃前日志时序 / 业务路径诊断（crash 主轨旁路 → 04e）。

复用 ``LogTimelineExtractor`` + ``BusinessFlowAnalyzer``，
不改动 01 解析 schema；无时序时跳过落盘与 prompt 注入。

默认仅在「有业务日志信号」或 ``force`` 时尝试，避免精简 tombstone /
纯符号化 dump 误产 04e。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 01.raw_log_sections 中视为「可能含业务日志」的段落名
_INTERESTING_SECTIONS = frozenset(
    {
        "hilog",
        "logcat",
        "application_specific",
        "asi",
        "syslog",
        "console",
        "console_log",
        "app_log",
    }
)

# 强格式：默认可落盘；generic_timestamp 仅 force 时视为成功分析
_STRONG_FORMATS = frozenset(
    {"android_logcat", "harmony_hilog", "ios_syslog"}
)

# 与 extractor 对齐的廉价预检（不必抽满条目）
_LOGCAT_LINE_RE = re.compile(
    r"(?m)^\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3}\s+\d+\s+\d+\s+[VDIWEF]\s+\S+"
)
_HILOG_LINE_RE = re.compile(
    r"(?m)^\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3}\s+\d+\s+\d+\s+[VDIWEF]\s+\S+/\S+\s*:"
)
_IOS_SYSLOG_HINT_RE = re.compile(
    r"(?m)^(?:Application Specific Information:|Console Log:|Standard Error:)"
)

_BANNER_KEYWORDS = (
    "hilog:",
    "logcat",
    "application specific information",
    "console log",
    "standard error:",
)


def _text_preview(text: str, limit: int = 200_000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit]


def _has_timeline_signals(
    parse_result: Dict[str, Any],
    crash_log_content: str = "",
) -> bool:
    """是否具备值得跑时序旁路的业务日志信号。"""
    text = crash_log_content or ""
    if not text and isinstance(parse_result, dict):
        text = str(parse_result.get("raw_content") or "")
    text = text.strip()
    if len(text) < 80:
        return False

    if isinstance(parse_result, dict):
        sections = parse_result.get("raw_log_sections") or []
        for sec in sections:
            sl = str(sec or "").strip().lower()
            if not sl:
                continue
            if sl in _INTERESTING_SECTIONS:
                return True
            if "log" in sl or "hilog" in sl or "syslog" in sl:
                return True

    preview = _text_preview(text)
    lower = preview.lower()
    for kw in _BANNER_KEYWORDS:
        if kw in lower:
            return True

    # 至少 3 行强格式，避免单行噪声
    if len(_LOGCAT_LINE_RE.findall(preview)) >= 3:
        return True
    if len(_HILOG_LINE_RE.findall(preview)) >= 3:
        return True
    if _IOS_SYSLOG_HINT_RE.search(preview):
        return True
    return False


def should_run_timeline_analysis(
    parse_result: Dict[str, Any],
    crash_log_content: str = "",
    *,
    force: bool = False,
) -> bool:
    """是否尝试抽取崩溃前时序。

    - ``force=True``：总是尝试
    - 默认：仅当检测到 logcat/HiLog/ASI 等业务日志信号时尝试
    """
    if force:
        return True
    if not isinstance(parse_result, dict):
        parse_result = {}
    return _has_timeline_signals(parse_result, crash_log_content)


def run_log_timeline_diagnosis(
    parse_result: Dict[str, Any],
    crash_log_content: str = "",
    *,
    force: bool = False,
    max_entries: int = 50,
) -> Optional[Dict[str, Any]]:
    """执行时序 + 业务路径分析。

    Returns:
        可写入 ``04e_log_timeline.json`` 的字典；无可抽内容时返回
        ``analyzed=False`` 的占位（调用方可选择不落盘），或 None（不应跑）。
    """
    if not should_run_timeline_analysis(
        parse_result, crash_log_content, force=force
    ):
        return None

    if not isinstance(parse_result, dict):
        parse_result = {}
    log_content = crash_log_content or str(parse_result.get("raw_content") or "")
    if not log_content.strip():
        return {
            "analyzed": False,
            "forced": bool(force),
            "skip_reason": "empty_log",
            "prompt_section_zh": "",
        }

    try:
        from tools.log_timeline_extractor import (
            BusinessFlowAnalyzer,
            LogTimelineExtractor,
        )
    except Exception as exc:
        logger.warning("log_timeline_extractor import failed: %s", exc)
        return {
            "analyzed": False,
            "forced": bool(force),
            "skip_reason": "import_error",
            "skip_detail": str(exc),
            "prompt_section_zh": "",
        }

    timeline = LogTimelineExtractor(max_entries=max_entries).extract(log_content)
    business = BusinessFlowAnalyzer().analyze(timeline)

    has_timeline = bool(timeline.entries)
    has_business = bool(business.operations)
    fmt = str(timeline.format_detected or "none")

    if not has_timeline and not force:
        return {
            "analyzed": False,
            "forced": False,
            "skip_reason": "no_timeline_entries",
            "format_detected": fmt,
            "extraction_note": timeline.extraction_note,
            "total_lines_scanned": timeline.total_lines_scanned,
            "prompt_section_zh": "",
            "skill": "log-timeline",
        }

    # 弱格式（generic_timestamp）默认不落盘成功态，避免 Date/Time 误报
    if (
        has_timeline
        and not force
        and fmt not in _STRONG_FORMATS
    ):
        return {
            "analyzed": False,
            "forced": False,
            "skip_reason": "weak_format_requires_force",
            "format_detected": fmt,
            "extraction_note": timeline.extraction_note,
            "entry_count": len(timeline.entries),
            "prompt_section_zh": "",
            "skill": "log-timeline",
            "note_zh": (
                "仅检出通用时间戳、非 logcat/HiLog/syslog；"
                "默认不写入 04e。需要时请加 --force-timeline-analysis。"
            ),
        }

    timeline_dict = timeline.to_dict()
    business_dict = business.to_dict()
    # 补充可疑操作明细（to_dict 默认只有 count）
    business_dict["suspicious_ops"] = [
        {
            "timestamp": op.timestamp,
            "category": op.category,
            "description": op.description,
            "source_tag": op.source_tag,
            "suspicious": op.suspicious,
        }
        for op in (business.suspicious_ops or [])[:10]
    ]

    prompt_section = _build_prompt_section(timeline, business, forced=force)

    return {
        "analyzed": True,
        "forced": bool(force),
        "skip_reason": None,
        "format_detected": fmt,
        "timeline": timeline_dict,
        "business_flow": business_dict,
        "has_business_ops": has_business,
        "entry_count": len(timeline.entries),
        "operation_count": len(business.operations),
        "prompt_section_zh": prompt_section,
        "skill": "log-timeline",
        "note_zh": (
            "崩溃前日志时序与业务路径旁路；"
            "用于辅助触发路径分析，不能单独定根因。"
            "精简 tombstone/无 logcat 时可能为空。"
        ),
    }


def _build_prompt_section(timeline: Any, business: Any, *, forced: bool) -> str:
    lines: List[str] = [
        "## 崩溃前日志时序 / 业务路径",
        "",
        "以下用于还原「用户/业务在崩溃前做了什么」，"
        "请与 PC/寄存器/源码证据交叉验证；无时序时勿臆造操作路径。",
        "",
    ]
    if forced:
        lines.append("（本次为强制时序分析）")
        lines.append("")

    biz_md = ""
    try:
        biz_md = business.render_markdown() or ""
    except Exception:
        biz_md = ""
    if biz_md.strip():
        lines.append(biz_md.strip())
        lines.append("")
    else:
        lines.append("（未匹配到明确业务操作关键字，仅保留原始时序摘要）")
        lines.append("")

    tl_md = ""
    try:
        tl_md = timeline.render_markdown() or ""
    except Exception:
        tl_md = ""
    if tl_md.strip():
        # render_markdown 已含表头；限制 prompt 体积：只保留后 15 行表内容已在工具内截断到 30
        lines.append(tl_md.strip())
        lines.append("")

    if getattr(business, "last_user_action", None):
        lines.append(f"- 请重点关注最后用户操作: {business.last_user_action}")
    if getattr(business, "active_component", None):
        lines.append(f"- 崩溃时活跃组件线索: {business.active_component}")
    if getattr(business, "inferred_path", None):
        lines.append(f"- 推断路径: {business.inferred_path}")
    lines.append("")
    lines.append(
        "分析要求: 若时序与崩溃栈一致，说明触发路径；"
        "若不一致或缺失，在「需补充材料」中请求完整 logcat/HiLog。"
    )
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"
