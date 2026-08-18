#!/usr/bin/env python3
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
from tool_system import BaseTool, ToolDefinition
from .core import diagnose_api_fault


class ApiFaultDiagnosisTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name="api_fault_diagnosis", description="Diagnose API errors and BusinessError using normalized error fields, module classification, knowledge matches and project usage evidence.", input_schema={"type": "object", "properties": {"error_code": {"type": ["string", "number"]}, "error_name": {"type": "string"}, "message": {"type": "string"}, "api": {"type": "string"}, "module": {"type": "string"}, "raw_log": {"type": "string"}, "project_root": {"type": "string"}}, "additionalProperties": True}, output_schema={"type": "object"}, category="analyzer")

    def validate_input(self, input_data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        if not isinstance(input_data, dict) or not any(key in input_data for key in ("error_code", "error_name", "message", "api", "raw_log", "error")):
            return False, "error_code, error_name, message, api, raw_log, or error is required"
        return True, None

    def execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        return diagnose_api_fault(input_data, project_root=input_data.get("project_root"))
