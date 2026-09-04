#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reproduce a crash scenario in the workspace after applying fixes."""

from __future__ import annotations

import shlex
from typing import Any, Dict, Optional

from tool_system.tool import BaseTool, ToolDefinition
from tool_system.tool_gateway import RuntimeAuthorization
from services.verification import CommandVerificationProvider, VerificationRequest


class ReproduceCrashTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="reproduce_crash",
            description="在 workspace 内执行 reproduce 命令，可选对比 crash_log。",
            input_schema={
                "type": "object",
                "properties": {
                    "workspace": {"type": "string"},
                    "command": {"type": "array", "items": {"type": "string"}},
                    "crash_log": {"type": "string", "description": "可选，用于对比的原始 crash log 路径"},
                    "timeout_sec": {"type": "number", "default": 300},
                },
                "required": ["workspace", "command"],
            },
            output_schema={"type": "object"},
            category="verification",
            version="1.0.0",
            risk="execute",
            side_effect=True,
            requires_approval=True,
            idempotent=False,
            cost_class="high",
            timeout_enforcement="subprocess",
        )

    def validate_input(self, input_data: Dict[str, Any]) -> "tuple[bool, Optional[str]]":
        if not str(input_data.get("workspace") or "").strip():
            return False, "缺少 workspace"
        command = input_data.get("command")
        if not command:
            return False, "缺少 command"
        return True, None

    def execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        command = input_data.get("command")
        if isinstance(command, str):
            command = shlex.split(command)
        approved = isinstance(input_data.get("_runtime_authorization"), RuntimeAuthorization)
        provider = CommandVerificationProvider(list(command), modes=["reproduce", "auto"], approved=approved)
        request = VerificationRequest(
            workspace=str(input_data.get("workspace")),
            mode="reproduce",
            timeout_sec=float(input_data.get("timeout_sec") or 300),
        )
        result = provider.verify(request).to_dict()
        crash_log = str(input_data.get("crash_log") or "").strip()
        if crash_log:
            result["crash_log"] = crash_log
        return result
