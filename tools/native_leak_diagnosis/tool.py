#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tool System adapter for native leak analysis."""

from __future__ import annotations

from typing import Any, Dict, Optional

from tool_system import BaseTool, ToolDefinition

from .core import analyze_native_leak_bundle


class NativeLeakAnalyzerTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="native_leak_analyzer",
            description="Parse HarmonyOS sample/smaps/NMD/kernel DMA and native_hook SQLite evidence.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "trace_db": {"type": "string"},
                    "max_callchains": {"type": "integer", "default": 5},
                    "min_callchain_percentage": {"type": "number", "default": 0.0},
                },
                "required": ["path"],
            },
            output_schema={"type": "object"},
            category="analysis",
            version="1.0.0",
            metadata={"platform": "HarmonyOS", "sidecar": True},
        )

    def validate_input(self, input_data: Dict[str, Any]) -> "tuple[bool, Optional[str]]":
        if not str(input_data.get("path") or "").strip():
            return False, "missing native leak input path"
        return True, None

    def execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        return analyze_native_leak_bundle(
            str(input_data["path"]),
            trace_db=str(input_data.get("trace_db") or ""),
            max_callchains=int(input_data.get("max_callchains") or 5),
            min_callchain_percentage=float(input_data.get("min_callchain_percentage") or 0.0),
        )
