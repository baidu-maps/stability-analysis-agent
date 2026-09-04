#!/usr/bin/env python3
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
from tool_system import BaseTool, ToolDefinition
from .core import analyze_jank_artifact


class JankAnalyzerTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name="jank_analyzer", description="Normalize trace analyzer JSON/CSV results into frame jank, CPU thread, completion latency, and fault-mode evidence.", input_schema={"type": "object", "properties": {"path": {"type": "string"}, "mode": {"type": "string"}, "deadline_ms": {"type": "number"}, "top_n": {"type": "integer"}}, "required": ["path"]}, output_schema={"type": "object"}, category="analyzer", risk="read_only", side_effect=False, idempotent=True, requires_approval=False, cost_class="medium")

    def validate_input(self, input_data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        if not isinstance(input_data, dict) or not str(input_data.get("path") or "").strip():
            return False, "path is required"
        return True, None

    def execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        return analyze_jank_artifact(str(input_data["path"]), mode=str(input_data.get("mode") or "frame"), deadline_ms=float(input_data.get("deadline_ms") or 16.67), top_n=int(input_data.get("top_n") or 20))
