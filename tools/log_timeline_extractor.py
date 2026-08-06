#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""崩溃前日志时序提取工具。

从 crash log 原始文本中提取崩溃发生前的系统日志/业务日志片段，
提供触发路径的时序辅助证据。

支持格式:
- iOS: Last Exception Backtrace 前的 Application Specific Information
- Android Tombstone: logcat 段
- HarmonyOS: HiLog 段
- 通用: 带时间戳的日志行（崩溃前 N 行）
"""

from __future__ import annotations
import re
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class TimelineEntry:
    """单条时序日志。"""
    timestamp: str
    level: str       # "I" / "W" / "E" / "F" / "D" / ""
    tag: str         # 日志 tag
    message: str     # 日志内容
    line_number: int  # 在原始日志中的行号


@dataclass
class TimelineContext:
    """崩溃前日志时序上下文。"""
    format_detected: str   # "ios_syslog" / "android_logcat" / "harmony_hilog" / "generic_timestamp" / "none"
    entries: List[TimelineEntry] = field(default_factory=list)
    total_lines_scanned: int = 0
    extraction_note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "format_detected": self.format_detected,
            "entries": [
                {
                    "timestamp": e.timestamp,
                    "level": e.level,
                    "tag": e.tag,
                    "message": e.message,
                    "line_number": e.line_number,
                }
                for e in self.entries
            ],
            "total_lines_scanned": self.total_lines_scanned,
            "extraction_note": self.extraction_note,
        }

    def render_markdown(self) -> str:
        """渲染为 Markdown 时序表。"""
        if not self.entries:
            return ""
        lines = [
            f"崩溃前日志时序 (格式: {self.format_detected}, 共 {len(self.entries)} 条):",
            "",
            "| 时间 | 级别 | Tag | 内容 |",
            "|------|------|-----|------|",
        ]
        for e in self.entries[-30:]:  # Show last 30 at most
            msg = e.message[:80] + ("..." if len(e.message) > 80 else "")
            lines.append(f"| {e.timestamp} | {e.level} | {e.tag} | {msg} |")
        if len(self.entries) > 30:
            lines.append(f"\n(已省略前 {len(self.entries) - 30} 条)")
        return "\n".join(lines)


# --- Format patterns ---

# Android logcat: "07-29 10:30:45.123  1234  5678 E TagName: message"
_LOGCAT_RE = re.compile(
    r"^(\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})\s+\d+\s+\d+\s+([VDIWEF])\s+(\S+)\s*:\s*(.*)$"
)

# iOS syslog: "Jul 29 10:30:45 iPhone appname[1234]: message"
_IOS_SYSLOG_RE = re.compile(
    r"^(\w{3}\s+\d+\s+\d{2}:\d{2}:\d{2})\s+\S+\s+(\S+)\[?\d*\]?\s*:\s*(.*)$"
)

# HarmonyOS HiLog: "07-29 10:30:45.123 1234 5678 E C03210/TagName: message"
_HILOG_RE = re.compile(
    r"^(\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})\s+\d+\s+\d+\s+([VDIWEF])\s+(\S+)/(\S+)\s*:\s*(.*)$"
)

# Generic timestamp: "2026-07-29 10:30:45" or "[10:30:45]" at line start
_GENERIC_TS_RE = re.compile(
    r"^[\[\(]?(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}|\d{2}:\d{2}:\d{2}(?:\.\d+)?)"
)


class LogTimelineExtractor:
    """从 crash log 中提取崩溃前的日志时序。"""

    def __init__(self, max_entries: int = 50):
        self._max_entries = max_entries

    def extract(self, crash_log_content: str) -> TimelineContext:
        """从崩溃日志中提取崩溃前的时序日志。

        Args:
            crash_log_content: 完整的 crash log 文本

        Returns:
            TimelineContext 时序上下文
        """
        if not crash_log_content:
            return TimelineContext(format_detected="none", extraction_note="空日志")

        lines = crash_log_content.splitlines()

        # Try each format in priority order
        result = self._try_android_logcat(lines)
        if result and result.entries:
            return result

        result = self._try_harmony_hilog(lines)
        if result and result.entries:
            return result

        result = self._try_ios_syslog(lines)
        if result and result.entries:
            return result

        result = self._try_generic_timestamp(lines)
        if result and result.entries:
            return result

        return TimelineContext(
            format_detected="none",
            total_lines_scanned=len(lines),
            extraction_note="未检测到可解析的时序日志格式",
        )

    def _try_android_logcat(self, lines: List[str]) -> Optional[TimelineContext]:
        """尝试提取 Android logcat 格式。"""
        entries: List[TimelineEntry] = []
        for i, line in enumerate(lines):
            m = _LOGCAT_RE.match(line.strip())
            if m:
                entries.append(TimelineEntry(
                    timestamp=m.group(1),
                    level=m.group(2),
                    tag=m.group(3),
                    message=m.group(4),
                    line_number=i + 1,
                ))
        if len(entries) < 3:
            return None
        return TimelineContext(
            format_detected="android_logcat",
            entries=entries[-self._max_entries:],
            total_lines_scanned=len(lines),
            extraction_note=f"提取 {len(entries)} 条 logcat 日志",
        )

    def _try_harmony_hilog(self, lines: List[str]) -> Optional[TimelineContext]:
        """尝试提取 HarmonyOS HiLog 格式。"""
        entries: List[TimelineEntry] = []
        in_hilog_section = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("HiLog:"):
                in_hilog_section = True
                continue
            if in_hilog_section or _HILOG_RE.match(stripped):
                m = _HILOG_RE.match(stripped)
                if m:
                    entries.append(TimelineEntry(
                        timestamp=m.group(1),
                        level=m.group(2),
                        tag=f"{m.group(3)}/{m.group(4)}",
                        message=m.group(5),
                        line_number=i + 1,
                    ))
        if len(entries) < 3:
            return None
        return TimelineContext(
            format_detected="harmony_hilog",
            entries=entries[-self._max_entries:],
            total_lines_scanned=len(lines),
            extraction_note=f"提取 {len(entries)} 条 HiLog 日志",
        )

    def _try_ios_syslog(self, lines: List[str]) -> Optional[TimelineContext]:
        """尝试提取 iOS syslog 格式。"""
        entries: List[TimelineEntry] = []
        for i, line in enumerate(lines):
            m = _IOS_SYSLOG_RE.match(line.strip())
            if m:
                entries.append(TimelineEntry(
                    timestamp=m.group(1),
                    level="",
                    tag=m.group(2),
                    message=m.group(3),
                    line_number=i + 1,
                ))
        if len(entries) < 3:
            return None
        return TimelineContext(
            format_detected="ios_syslog",
            entries=entries[-self._max_entries:],
            total_lines_scanned=len(lines),
            extraction_note=f"提取 {len(entries)} 条 iOS syslog 日志",
        )

    def _try_generic_timestamp(self, lines: List[str]) -> Optional[TimelineContext]:
        """尝试提取通用带时间戳的日志行。"""
        entries: List[TimelineEntry] = []
        for i, line in enumerate(lines):
            m = _GENERIC_TS_RE.match(line.strip())
            if m:
                entries.append(TimelineEntry(
                    timestamp=m.group(1),
                    level="",
                    tag="",
                    message=line.strip()[m.end():].strip("] ):"),
                    line_number=i + 1,
                ))
        if len(entries) < 3:
            return None
        return TimelineContext(
            format_detected="generic_timestamp",
            entries=entries[-self._max_entries:],
            total_lines_scanned=len(lines),
            extraction_note=f"提取 {len(entries)} 条通用时序日志",
        )


# =====================================================================
# 业务流水分析：从时序日志中推断崩溃前的业务操作路径
# =====================================================================

# Common business operation patterns to detect in log messages
BUSINESS_OP_PATTERNS = [
    # UI lifecycle
    (r"onCreate|onStart|onResume|onPause|onStop|onDestroy|viewDidLoad|viewWillAppear|viewDidDisappear", "lifecycle"),
    (r"Activity|Fragment|ViewController|UIViewController", "ui_navigation"),
    # Network
    (r"request|response|HTTP|API|fetch|download|upload|connect|socket", "network"),
    # Database
    (r"query|insert|update|delete|transaction|commit|rollback|sqlite|realm|coredata", "database"),
    # File IO
    (r"read|write|open|close|file|stream|path|directory|mkdir", "file_io"),
    # User interaction
    (r"click|tap|touch|gesture|button|press|scroll|swipe|input", "user_action"),
    # Memory/Resource
    (r"alloc|dealloc|release|retain|free|new|delete|dispose|GC|finalize", "memory_mgmt"),
    # Threading
    (r"thread|dispatch|async|sync|queue|handler|executor|pool|task|runnable", "threading"),
    # Media
    (r"play|pause|stop|record|camera|video|audio|media|codec", "media"),
    # Service/Component
    (r"service|broadcast|receiver|provider|intent|notification|alarm", "service"),
]


@dataclass
class BusinessOperation:
    """一个推断出的业务操作。"""
    timestamp: str
    category: str       # lifecycle/network/database/...
    description: str    # 从日志提取的操作描述
    source_tag: str     # 日志 tag
    line_number: int
    severity: str = ""  # W/E/F 级别的操作更值得关注


@dataclass
class BusinessFlowContext:
    """业务操作流水分析结果。"""
    operations: List[BusinessOperation] = field(default_factory=list)
    inferred_path: str = ""       # 推断的操作路径摘要
    last_user_action: str = ""    # 最后一个用户操作
    active_component: str = ""    # 崩溃时活跃的组件
    suspicious_ops: List[BusinessOperation] = field(default_factory=list)  # 可疑操作

    def to_dict(self) -> Dict[str, Any]:
        return {
            "operations": [
                {
                    "timestamp": op.timestamp,
                    "category": op.category,
                    "description": op.description,
                    "source_tag": op.source_tag,
                    "severity": op.severity,
                }
                for op in self.operations
            ],
            "inferred_path": self.inferred_path,
            "last_user_action": self.last_user_action,
            "active_component": self.active_component,
            "suspicious_ops_count": len(self.suspicious_ops),
        }

    def render_markdown(self) -> str:
        """渲染为崩溃前业务操作路径 Markdown。"""
        if not self.operations:
            return ""
        lines = ["崩溃前业务操作路径:"]
        if self.inferred_path:
            lines.append(f"\n**推断路径**: {self.inferred_path}")
        if self.last_user_action:
            lines.append(f"**最后用户操作**: {self.last_user_action}")
        if self.active_component:
            lines.append(f"**活跃组件**: {self.active_component}")

        lines.append("\n| 时间 | 类别 | 操作 |")
        lines.append("|------|------|------|")
        for op in self.operations[-20:]:
            marker = "⚠️" if op.severity in ("E", "F", "W") else ""
            lines.append(f"| {op.timestamp} | {op.category} | {marker}{op.description[:60]} |")

        if self.suspicious_ops:
            lines.append(f"\n**可疑操作** ({len(self.suspicious_ops)} 项):")
            for op in self.suspicious_ops[:5]:
                lines.append(f"- [{op.category}] {op.description}")

        return "\n".join(lines)


class BusinessFlowAnalyzer:
    """从时序日志中推断崩溃前的业务操作路径。"""

    def __init__(self):
        self._patterns = [
            (re.compile(pattern, re.IGNORECASE), category)
            for pattern, category in BUSINESS_OP_PATTERNS
        ]

    def analyze(self, timeline: TimelineContext) -> BusinessFlowContext:
        """分析时序日志中的业务操作流水。

        Args:
            timeline: LogTimelineExtractor 产出的 TimelineContext

        Returns:
            BusinessFlowContext 含推断的操作路径
        """
        if not timeline.entries:
            return BusinessFlowContext()

        operations: List[BusinessOperation] = []

        for entry in timeline.entries:
            categories = self._classify_entry(entry)
            if categories:
                # Take the most specific category
                category = categories[0]
                operations.append(BusinessOperation(
                    timestamp=entry.timestamp,
                    category=category,
                    description=self._extract_description(entry, category),
                    source_tag=entry.tag,
                    line_number=entry.line_number,
                    severity=entry.level,
                ))

        if not operations:
            return BusinessFlowContext()

        # Infer the operation path
        context = BusinessFlowContext(operations=operations)
        context.inferred_path = self._infer_path(operations)
        context.last_user_action = self._find_last_user_action(operations)
        context.active_component = self._find_active_component(operations)
        context.suspicious_ops = [
            op for op in operations
            if op.severity in ("E", "F", "W") or op.category == "memory_mgmt"
        ]

        return context

    def _classify_entry(self, entry: TimelineEntry) -> List[str]:
        """Classify a log entry by matching against business operation patterns."""
        text = f"{entry.tag} {entry.message}"
        matches = []
        for pattern, category in self._patterns:
            if pattern.search(text):
                matches.append(category)
        return matches

    def _extract_description(self, entry: TimelineEntry, category: str) -> str:
        """Extract a concise description from the log entry."""
        msg = entry.message.strip()
        if entry.tag:
            return f"[{entry.tag}] {msg[:80]}"
        return msg[:100]

    def _infer_path(self, operations: List[BusinessOperation]) -> str:
        """Infer the high-level operation path from the sequence."""
        categories_seen = []
        for op in operations:
            if not categories_seen or categories_seen[-1] != op.category:
                categories_seen.append(op.category)
        # Summarize as path
        return " → ".join(categories_seen[-6:])

    def _find_last_user_action(self, operations: List[BusinessOperation]) -> str:
        """Find the last user interaction before crash."""
        for op in reversed(operations):
            if op.category == "user_action":
                return op.description
        return ""

    def _find_active_component(self, operations: List[BusinessOperation]) -> str:
        """Find the last active UI component."""
        for op in reversed(operations):
            if op.category in ("lifecycle", "ui_navigation"):
                return op.description
        return ""
