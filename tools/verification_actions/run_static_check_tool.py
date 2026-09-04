#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run a static check / lint verification command in the workspace."""

from __future__ import annotations

import shlex
from typing import Any, Dict, Optional

from tool_system.tool import BaseTool, ToolDefinition
from services.verification import CommandVerificationProvider, VerificationRequest
from tool_system.tool_gateway import RuntimeAuthorization


class RunStaticCheckTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="run_static_check",
            description="在 workspace 内执行 syntax/static/lint 验证命令。",
            input_schema={
                "type": "object",
                "properties": {
                    "workspace": {"type": "string"},
                    "command": {"type": "array", "items": {"type": "string"}},
                    "changed_files": {"type": "array", "items": {"type": "string"}},
                    "timeout_sec": {"type": "number", "default": 300},
                },
                "required": ["workspace"],
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
        return True, None

    def execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        command = input_data.get("command") or ["python3", "-m", "compileall", "-q", "."]
        if isinstance(command, str):
            command = shlex.split(command)
        approved = isinstance(input_data.get("_runtime_authorization"), RuntimeAuthorization)
        provider = CommandVerificationProvider(list(command), modes=["syntax", "static", "auto"], approved=approved)
        request = VerificationRequest(
            workspace=str(input_data.get("workspace")),
            changed_files=[str(x) for x in (input_data.get("changed_files") or []) if x],
            mode="syntax",
            timeout_sec=float(input_data.get("timeout_sec") or 300),
        )
        return provider.verify(request).to_dict()
