#!/usr/bin/env python3
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
from tool_system import BaseTool, ToolDefinition
from .core import diagnose_js_crash


class JsCrashDiagnosisTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name="js_crash_diagnosis", description="Diagnose HarmonyOS JS/ArkTS crashes using Error fields, message patterns, stack frames, and HybridStack evidence.", input_schema={"type": "object", "properties": {"parse_result": {"type": "object"}, "crash_info": {"type": "object"}, "raw_content": {"type": "string"}}, "additionalProperties": True}, output_schema={"type": "object"}, category="analyzer")

    def validate_input(self, input_data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        if not isinstance(input_data, dict): return False, "input must be an object"
        if not any(key in input_data for key in ("parse_result", "crash_info", "raw_content", "reason", "error_name")): return False, "parse_result, crash_info, or raw_content is required"
        return True, None

    def execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        payload = input_data.get("parse_result") if isinstance(input_data.get("parse_result"), dict) else input_data
        return diagnose_js_crash(payload)
