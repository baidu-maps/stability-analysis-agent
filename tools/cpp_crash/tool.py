#!/usr/bin/env python3
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
from tool_system import BaseTool, ToolDefinition
from .core import diagnose_cpp_crash


class CppCrashDiagnosisTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name="cpp_crash_diagnosis", description="Diagnose C/C++ crashes using signal, fault address, registers, memory sections, native stack and existing address analysis.", input_schema={"type": "object", "properties": {"parse_result": {"type": "object"}, "crash_info": {"type": "object"}, "raw_content": {"type": "string"}}, "additionalProperties": True}, output_schema={"type": "object"}, category="analyzer", risk="read_only", side_effect=False, idempotent=True, requires_approval=False, cost_class="low")

    def validate_input(self, input_data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        if not isinstance(input_data, dict): return False, "input must be an object"
        if not any(key in input_data for key in ("parse_result", "crash_info", "raw_content", "signal", "fault_addr")): return False, "parse_result, crash_info, or raw_content is required"
        return True, None

    def execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        return diagnose_cpp_crash(input_data)
