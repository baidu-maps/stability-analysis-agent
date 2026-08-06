#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采样栈热点函数统计分析器。

对崩溃调用栈（或多个采样栈）中的函数进行频次统计，
识别 CPU 热点函数和重复出现的调用模式。

参考华为 DFX Skills 的 sample_stack_analyzer.py 设计思路。
"""

from __future__ import annotations
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class HotspotFunction:
    """热点函数统计。"""
    function: str
    module: str
    occurrences: int       # 出现次数
    percentage: float      # 占比 (0-100)
    is_app_code: bool      # 是否为应用代码
    is_system: bool        # 是否为系统库
    call_contexts: List[str] = field(default_factory=list)  # 出现时的上下文（前后帧）


@dataclass
class CallPattern:
    """调用模式。"""
    pattern: Tuple[str, ...]  # 连续函数名元组
    occurrences: int
    threads: List[str] = field(default_factory=list)


@dataclass
class StackHotspotAnalysis:
    """栈热点分析结果。"""
    total_frames_analyzed: int = 0
    total_threads_analyzed: int = 0
    hotspot_functions: List[HotspotFunction] = field(default_factory=list)
    app_hotspots: List[HotspotFunction] = field(default_factory=list)
    call_patterns: List[CallPattern] = field(default_factory=list)
    blocking_indicators: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_frames_analyzed": self.total_frames_analyzed,
            "total_threads_analyzed": self.total_threads_analyzed,
            "hotspot_functions": [
                {
                    "function": h.function,
                    "module": h.module,
                    "occurrences": h.occurrences,
                    "percentage": round(h.percentage, 1),
                    "is_app_code": h.is_app_code,
                }
                for h in self.hotspot_functions[:10]
            ],
            "app_hotspots": [
                {
                    "function": h.function,
                    "module": h.module,
                    "occurrences": h.occurrences,
                    "percentage": round(h.percentage, 1),
                }
                for h in self.app_hotspots[:5]
            ],
            "call_patterns": [
                {"pattern": list(p.pattern), "occurrences": p.occurrences}
                for p in self.call_patterns[:5]
            ],
            "blocking_indicators": self.blocking_indicators,
        }

    def render_markdown(self) -> str:
        """渲染热点分析为 Markdown。"""
        if not self.hotspot_functions:
            return ""
        lines = [
            f"栈帧热点分析 (共 {self.total_frames_analyzed} 帧, {self.total_threads_analyzed} 线程):",
            "",
        ]
        if self.app_hotspots:
            lines.append("**应用层热点函数:**")
            lines.append("| 函数 | 模块 | 出现次数 | 占比 |")
            lines.append("|------|------|----------|------|")
            for h in self.app_hotspots[:5]:
                lines.append(f"| {h.function[:50]} | {h.module} | {h.occurrences} | {h.percentage:.1f}% |")
            lines.append("")

        if self.blocking_indicators:
            lines.append("**阻塞指标:**")
            for indicator in self.blocking_indicators[:5]:
                lines.append(f"- {indicator}")
            lines.append("")

        if self.call_patterns:
            lines.append("**重复调用模式:**")
            for p in self.call_patterns[:3]:
                pattern_str = " → ".join(p.pattern)
                lines.append(f"- ({p.occurrences}次) {pattern_str}")

        return "\n".join(lines)


# System library detection (reused from stack_layer_classifier patterns)
_SYSTEM_MODULE_RE = re.compile(
    r"^(libc\.|libsystem_|libdispatch|libobjc|libstdc\+\+|libc\+\+|"
    r"libpthread|CoreFoundation|Foundation|UIKit|AppKit|"
    r"libart\.so|libandroid_runtime|libbinder|libutils\.so|"
    r"libcutils\.so|liblog\.so|linker|libace|libark|"
    r"libdyld|libxpc|GraphicsServices)",
    re.IGNORECASE,
)

# Blocking function patterns
_BLOCKING_PATTERNS = [
    (r"pthread_mutex_lock|pthread_mutex_timedlock", "互斥锁等待"),
    (r"pthread_cond_wait|pthread_cond_timedwait", "条件变量等待"),
    (r"__psynch_mutexwait|__psynch_cvwait", "内核锁等待(macOS)"),
    (r"futex|SYS_futex", "futex 等待(Linux)"),
    (r"dispatch_sync|dispatch_barrier_sync", "GCD 同步等待"),
    (r"objc_msgSend.*wait|.*Wait.*Sync", "同步等待调用"),
    (r"sleep|usleep|nanosleep", "显式 sleep"),
    (r"epoll_wait|poll|select|kevent", "I/O 多路复用等待"),
    (r"read|write|recv|send|connect", "I/O 阻塞"),
    (r"binder.*transact|ioctl.*binder", "Binder IPC 等待"),
]


class StackHotspotAnalyzer:
    """对调用栈进行热点函数统计分析。"""

    def __init__(self):
        self._blocking_res = [
            (re.compile(pattern, re.IGNORECASE), desc)
            for pattern, desc in _BLOCKING_PATTERNS
        ]

    def analyze(
        self,
        resolved_stack: Dict[str, Any],
        parsed_data: Optional[Dict[str, Any]] = None,
    ) -> StackHotspotAnalysis:
        """分析符号化后的调用栈，统计热点函数。

        Args:
            resolved_stack: 02 符号化结果
            parsed_data: 01 解析结果（用于获取多线程信息）

        Returns:
            StackHotspotAnalysis 结果
        """
        result = StackHotspotAnalysis()

        # Collect all frames from all threads
        all_frames: List[Dict[str, Any]] = []
        thread_frames: Dict[str, List[Dict[str, Any]]] = {}

        threads = []
        if isinstance(resolved_stack, dict):
            threads = resolved_stack.get("resolved_threads", [])
        if not threads and isinstance(parsed_data, dict):
            threads = parsed_data.get("threads", [])

        for thread in threads:
            tid = str(thread.get("tid") or thread.get("name") or len(thread_frames))
            frames = thread.get("frames", [])
            all_frames.extend(frames)
            thread_frames[tid] = frames

        result.total_frames_analyzed = len(all_frames)
        result.total_threads_analyzed = len(thread_frames)

        if not all_frames:
            return result

        # Count function occurrences
        func_counter: Counter = Counter()
        func_module: Dict[str, str] = {}

        for frame in all_frames:
            func = frame.get("function") or frame.get("resolved_function") or ""
            module = frame.get("module") or ""
            if func:
                func_counter[func] += 1
                if func not in func_module:
                    func_module[func] = module

        # Build hotspot list
        total = len(all_frames)
        for func, count in func_counter.most_common(20):
            module = func_module.get(func, "")
            is_system = bool(_SYSTEM_MODULE_RE.search(module))
            hotspot = HotspotFunction(
                function=func,
                module=module,
                occurrences=count,
                percentage=(count / total) * 100,
                is_app_code=not is_system and module != "",
                is_system=is_system,
            )
            result.hotspot_functions.append(hotspot)
            if hotspot.is_app_code:
                result.app_hotspots.append(hotspot)

        # Detect blocking indicators
        for frame in all_frames:
            func = frame.get("function") or frame.get("resolved_function") or ""
            for pattern, desc in self._blocking_res:
                if pattern.search(func):
                    indicator = f"{desc}: {func[:60]}"
                    if indicator not in result.blocking_indicators:
                        result.blocking_indicators.append(indicator)

        # Find repeated call patterns (bigrams)
        self._find_call_patterns(thread_frames, result)

        return result

    def _find_call_patterns(
        self,
        thread_frames: Dict[str, List[Dict[str, Any]]],
        result: StackHotspotAnalysis,
    ) -> None:
        """Find repeated call patterns (function pairs that appear together)."""
        bigram_counter: Counter = Counter()

        for tid, frames in thread_frames.items():
            funcs = [
                f.get("function") or f.get("resolved_function") or ""
                for f in frames
                if f.get("function") or f.get("resolved_function")
            ]
            # Extract bigrams (consecutive function pairs)
            for i in range(len(funcs) - 1):
                if funcs[i] and funcs[i+1]:
                    bigram_counter[(funcs[i], funcs[i+1])] += 1

        # Top patterns (exclude single-occurrence)
        for pattern, count in bigram_counter.most_common(10):
            if count >= 2:
                result.call_patterns.append(CallPattern(
                    pattern=pattern,
                    occurrences=count,
                ))