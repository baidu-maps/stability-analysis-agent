#!/usr/bin/env python3
"""Tool System adapter for JS/ArkTS heap analysis."""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from tool_system import BaseTool, ToolDefinition
from .core import analyze_js_heap


class JsHeapAnalyzerTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="js_heap_analyzer",
            description="Analyze V8/HarmonyOS heapsnapshot artifacts and return retained-size clusters, reference summaries, and ArkTS leak fault-mode candidates.",
            input_schema={"type": "object", "properties": {"path": {"type": "string"}, "top_n": {"type": "integer"}}, "required": ["path"]},
            output_schema={"type": "object"},
            category="analyzer",
        )

    def validate_input(self, input_data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        if not str(input_data.get("path") or "").strip():
            return False, "path is required"
        return True, None

    def execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        return analyze_js_heap(str(input_data["path"]), top_n=int(input_data.get("top_n") or 20), baseline=input_data.get("baseline"))
