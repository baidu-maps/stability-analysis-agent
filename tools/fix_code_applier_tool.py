#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
将结构化 fix_plan 应用到源码目录的工具。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from tool_system.tool import BaseTool, ToolDefinition
from services.code_fixer import CodeFixer, extract_candidate_nodes


class FixCodeApplierTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="fix_code_applier",
            description="将 fix_plan.edits 应用到 code_root 源码目录，返回应用明细。",
            input_schema={
                "type": "object",
                "properties": {
                    "fix_plan": {"type": "object", "description": "结构化修复计划，包含 edits"},
                    "code_context": {"type": "object", "description": "03_code_content_provider.json 对应对象"},
                    "code_roots": {"type": "array", "description": "源码根目录列表（绝对路径）"},
                    "required_targets": {
                        "type": "array",
                        "description": "可选，限制仅允许修改这些目标 [{file,function_signature}]",
                    },
                    "report_dir": {"type": "string", "description": "可选，报告目录（用于备份 original_sources）"},
                    "backup_original_sources": {"type": "boolean", "default": True},
                },
                "required": ["fix_plan", "code_context", "code_roots"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "summary": {"type": "string"},
                    "applied": {"type": "array"},
                    "error": {"type": "string"},
                },
            },
            category="provider",
            version="1.0.0",
            risk="workspace_write",
            side_effect=True,
            requires_approval=True,
            idempotent=False,
            cost_class="medium",
        )

    def validate_input(self, input_data: Dict[str, Any]) -> "tuple[bool, Optional[str]]":
        for field in ("fix_plan", "code_context", "code_roots"):
            if field not in input_data:
                return False, f"缺少 required 字段: {field}"
        return True, None

    @staticmethod
    def _normalize_required_targets(raw_targets: Any) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        if not isinstance(raw_targets, list):
            return out
        for t in raw_targets:
            if not isinstance(t, dict):
                continue
            f = str(t.get("file") or "").strip()
            s = str(t.get("function_signature") or "").strip()
            if f and s:
                out.append({"file": f, "function_signature": s})
        return out

    def execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        fix_plan = input_data.get("fix_plan") or {}
        code_context = input_data.get("code_context") or {}
        code_roots_raw = input_data.get("code_roots") or []
        required_targets = self._normalize_required_targets(input_data.get("required_targets"))
        backup_original_sources = bool(input_data.get("backup_original_sources", True))

        if not isinstance(fix_plan, dict):
            return {"success": False, "error": "fix_plan 必须为 object"}
        if not isinstance(code_context, dict):
            return {"success": False, "error": "code_context 必须为 object"}
        if not isinstance(code_roots_raw, list):
            return {"success": False, "error": "code_roots 必须为 array"}

        code_roots = [str(Path(str(p)).expanduser().resolve()) for p in code_roots_raw if str(p).strip()]
        if not code_roots:
            return {"success": False, "error": "code_roots 为空"}

        candidate_nodes = extract_candidate_nodes(code_context)
        if not candidate_nodes:
            return {"success": False, "error": "code_context 未提供可替换函数候选"}

        report_dir_raw = str(input_data.get("report_dir") or "").strip()
        report_dir = Path(report_dir_raw).expanduser().resolve() if report_dir_raw else None

        fixer = CodeFixer(llm_adapter=None)
        result = fixer.apply_fix_plan(
            fix_plan=fix_plan,
            candidate_nodes=candidate_nodes,
            code_roots=code_roots,
            report_dir=report_dir,
            backup_original_sources=backup_original_sources,
            required_targets=required_targets or None,
            code_context=code_context,
        )
        out = result.to_dict()
        out["summary"] = result.summary
        return out

