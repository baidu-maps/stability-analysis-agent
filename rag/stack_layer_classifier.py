#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""调用栈分层分类器。

将符号化后的调用栈帧分为三层：崩溃帧、首个非运行时调用方、首个应用侧帧。
参考华为 DFX Skills 的分层思想，避免将系统运行时帧误判为业务根因。
"""

from __future__ import annotations
import re
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# System library patterns (modules that are part of OS / runtime)
SYSTEM_MODULE_PATTERNS = [
    # iOS / macOS system libraries
    r"^libsystem_", r"^libdispatch", r"^libobjc", r"^libxpc",
    r"^CoreFoundation$", r"^Foundation$", r"^UIKit$", r"^AppKit$",
    r"^libc\+\+", r"^libstdc\+\+", r"^libc\.", r"^libpthread",
    r"^libdyld", r"^libclosure", r"^GraphicsServices",
    # Android / Linux system
    r"^libart\.so", r"^libandroid_runtime", r"^libbinder",
    r"^libutils\.so", r"^libcutils\.so", r"^liblog\.so",
    r"^libc\.so", r"^libm\.so", r"^libdl\.so", r"^libstdc\+\+\.so",
    r"^linker", r"^/system/lib", r"^/apex/",
    # HarmonyOS
    r"^libace", r"^libark", r"^libhilog", r"^libhitrace",
    # Generic
    r"^ld-linux", r"^linux-vdso",
]

# Runtime / crash-infrastructure functions (always skip when looking for app code)
RUNTIME_FUNCTION_PATTERNS = [
    r"^__cxa_throw", r"^__cxa_rethrow", r"^__cxa_begin_catch",
    r"^abort$", r"^raise$", r"^__pthread_kill$", r"^pthread_kill$",
    r"^_objc_msgSend", r"^objc_exception_throw", r"^objc_msgSend$",
    r"^___forwarding___", r"^_CF_forwarding_prep",
    r"^__assert_rtn", r"^__assert_fail",
    r"^signal_handler", r"^__restore_rt",
    r"^_sigtramp$", r"^__kill$",
    r"^std::terminate", r"^__terminate$",
    r"^__fortify_fail", r"^__stack_chk_fail",
    r"^_ZSt9terminatev",  # std::terminate mangled
]


@dataclass
class FrameAnnotation:
    """单帧的分层标注。"""
    frame_index: int
    function: str
    module: str
    layer: str  # "crash_frame" / "runtime" / "system" / "app" / "unknown"


@dataclass
class StackLayers:
    """调用栈分层结果。"""
    crash_frame: Optional[Dict[str, Any]] = None
    first_non_runtime: Optional[Dict[str, Any]] = None
    first_app_frame: Optional[Dict[str, Any]] = None
    annotations: List[FrameAnnotation] = field(default_factory=list)
    summary: str = ""


class StackLayerClassifier:
    """对符号化后的调用栈进行分层分类。"""

    def __init__(
        self,
        system_patterns: Optional[List[str]] = None,
        runtime_patterns: Optional[List[str]] = None,
    ):
        self._system_res = [
            re.compile(p, re.IGNORECASE)
            for p in (system_patterns or SYSTEM_MODULE_PATTERNS)
        ]
        self._runtime_res = [
            re.compile(p, re.IGNORECASE)
            for p in (runtime_patterns or RUNTIME_FUNCTION_PATTERNS)
        ]

    def classify(self, resolved_frames: List[Dict[str, Any]]) -> StackLayers:
        """对帧列表进行分层分类。

        Args:
            resolved_frames: 符号化后的帧列表，每帧含 function, module, resolved_file 等字段

        Returns:
            StackLayers 分层结果
        """
        if not resolved_frames:
            return StackLayers(summary="无可用帧")

        annotations: List[FrameAnnotation] = []
        crash_frame = None
        first_non_runtime = None
        first_app_frame = None

        for i, frame in enumerate(resolved_frames):
            func = str(frame.get("function") or frame.get("resolved_function") or "")
            module = str(frame.get("module") or "")

            if i == 0:
                layer = "crash_frame"
                crash_frame = frame
            elif self._is_runtime_function(func):
                layer = "runtime"
            elif self._is_system_module(module):
                layer = "system"
            else:
                layer = "app"
                if first_app_frame is None:
                    first_app_frame = frame

            # First non-runtime (could be system or app)
            if i > 0 and first_non_runtime is None and layer not in ("runtime",):
                first_non_runtime = frame

            annotations.append(FrameAnnotation(
                frame_index=i,
                function=func,
                module=module,
                layer=layer,
            ))

        # Build summary
        summary_parts = []
        if crash_frame:
            summary_parts.append(f"崩溃帧: {crash_frame.get('function', '?')} ({crash_frame.get('module', '?')})")
        if first_non_runtime:
            summary_parts.append(f"首个非运行时: {first_non_runtime.get('function', '?')} ({first_non_runtime.get('module', '?')})")
        if first_app_frame:
            summary_parts.append(f"首个应用帧: {first_app_frame.get('function', '?')} ({first_app_frame.get('module', '?')})")
        elif first_non_runtime and not first_app_frame:
            summary_parts.append("未发现明确应用帧（可能为纯系统/框架崩溃）")

        return StackLayers(
            crash_frame=crash_frame,
            first_non_runtime=first_non_runtime,
            first_app_frame=first_app_frame,
            annotations=annotations,
            summary=" | ".join(summary_parts),
        )

    def _is_system_module(self, module: str) -> bool:
        if not module:
            return False
        return any(r.search(module) for r in self._system_res)

    def _is_runtime_function(self, func: str) -> bool:
        if not func:
            return False
        return any(r.search(func) for r in self._runtime_res)

    def render_stack_layers(self, layers: StackLayers) -> str:
        """渲染分层结果为 Markdown 表格。"""
        lines = ["调用栈分层分析:"]
        lines.append("")
        lines.append("| 层级 | 函数 | 模块 |")
        lines.append("|------|------|------|")
        if layers.crash_frame:
            lines.append(f"| 崩溃帧(#00) | {layers.crash_frame.get('function', '?')} | {layers.crash_frame.get('module', '?')} |")
        if layers.first_non_runtime:
            lines.append(f"| 首个非运行时 | {layers.first_non_runtime.get('function', '?')} | {layers.first_non_runtime.get('module', '?')} |")
        if layers.first_app_frame:
            lines.append(f"| 首个应用帧 | {layers.first_app_frame.get('function', '?')} | {layers.first_app_frame.get('module', '?')} |")
        if not layers.first_app_frame:
            lines.append("| 首个应用帧 | (未发现) | — |")
        return "\n".join(lines)
