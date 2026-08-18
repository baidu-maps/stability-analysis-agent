#!/usr/bin/env python3
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
from tool_system import BaseTool, ToolDefinition
from .core import analyze_appfreeze


class AppFreezeDiagnosisTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name="appfreeze_diagnosis", description="Diagnose HarmonyOS AppFreeze using freeze type, multi-sample stacks, EventHandler/Binder evidence and FFRT dependency cycles.", input_schema={"type": "object", "properties": {"parse_result": {"type": "object"}, "raw_content": {"type": "string"}, "samples": {"type": "array"}, "ffrt_edges": {"type": "array"}}, "additionalProperties": True}, output_schema={"type": "object"}, category="analyzer")

    def validate_input(self, input_data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        if not isinstance(input_data, dict) or not any(key in input_data for key in ("parse_result", "raw_content", "samples", "freeze_type", "freeze_reason")):
            return False, "parse_result, raw_content, samples, or freeze_type is required"
        return True, None

    def execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        return analyze_appfreeze(input_data, raw_content=str(input_data.get("raw_content") or ""), samples=input_data.get("samples"), ffrt_edges=input_data.get("ffrt_edges"))
