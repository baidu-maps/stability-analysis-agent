#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 AI 分析文本提取可应用修复代码（edits）的工具。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from tool_system.tool import BaseTool, ToolDefinition
from services.code_fixer import (
    CodeFixer,
    extract_candidate_nodes,
    _extract_include_directive_edits,
    _extract_member_declaration_edits,
    _extract_required_function_names_from_analysis,
    _ensure_owner_class_methods_in_targets,
    _select_required_targets,
)


class FixCodeExtractorTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="fix_code_extractor",
            description="从 AI 分析文本中提取结构化修复 edits，并输出覆盖率统计。",
            input_schema={
                "type": "object",
                "properties": {
                    "analysis_text": {"type": "string", "description": "AI 分析文本（通常是 06_ai_gen_res.md 内容）"},
                    "code_context": {"type": "object", "description": "03_code_content_provider.json 对应对象"},
                    "required_targets": {
                        "type": "array",
                        "description": "可选，显式指定目标函数列表 [{file,function_signature}]",
                    },
                    "strict_required": {
                        "type": "boolean",
                        "description": "为 true 时要求 required_targets 全覆盖，否则返回错误",
                        "default": False,
                    },
                },
                "required": ["analysis_text", "code_context"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "fix_plan": {"type": "object"},
                    "candidate_count": {"type": "integer"},
                    "required_target_count": {"type": "integer"},
                    "extracted_count": {"type": "integer"},
                    "missing_required": {"type": "array"},
                    "error": {"type": "string"},
                },
            },
            category="provider",
            version="1.0.0",
            risk="read_only",
            side_effect=False,
            idempotent=True,
            requires_approval=False,
            cost_class="medium",
        )

    def validate_input(self, input_data: Dict[str, Any]) -> "tuple[bool, Optional[str]]":
        if "analysis_text" not in input_data:
            return False, "缺少 required 字段: analysis_text"
        if "code_context" not in input_data:
            return False, "缺少 required 字段: code_context"
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
        analysis_text = str(input_data.get("analysis_text") or "").strip()
        code_context = input_data.get("code_context") or {}
        strict_required = bool(input_data.get("strict_required", False))
        if not analysis_text:
            return {"success": False, "error": "analysis_text 为空"}
        if not isinstance(code_context, dict):
            return {"success": False, "error": "code_context 必须为 object"}

        candidate_nodes = extract_candidate_nodes(code_context)
        if not candidate_nodes:
            return {"success": False, "error": "code_context 未提供可替换函数候选", "candidate_count": 0}

        explicit_targets = self._normalize_required_targets(input_data.get("required_targets"))
        if explicit_targets:
            required_targets = explicit_targets
        else:
            required_names = _extract_required_function_names_from_analysis(analysis_text)
            required_targets = _select_required_targets(candidate_nodes, required_names)
            required_targets = _ensure_owner_class_methods_in_targets(
                code_context, required_names, required_targets
            )

        fixer = CodeFixer(llm_adapter=None)
        fix_plan = fixer._try_extract_fix_plan_from_analysis(
            analysis_text=analysis_text,
            candidate_nodes=candidate_nodes,
            required_targets=required_targets,
        )
        if not fix_plan:
            return {
                "success": False,
                "error": "未能从分析文本中提取到可执行 edits",
                "candidate_count": len(candidate_nodes),
                "required_target_count": len(required_targets),
                "extracted_count": 0,
                "missing_required": required_targets,
            }

        edits = fix_plan.get("edits", []) if isinstance(fix_plan, dict) else []
        extra_edits = _extract_include_directive_edits(analysis_text, code_context)
        extra_edits.extend(_extract_member_declaration_edits(analysis_text, code_context))
        if extra_edits:
            edits_list = edits if isinstance(edits, list) else []
            edits_list.extend(extra_edits)
            fix_plan["edits"] = edits_list
            edits = edits_list
        extracted_count = len(edits) if isinstance(edits, list) else 0

        missing_required: List[Dict[str, str]] = []
        if required_targets:
            for t in required_targets:
                matched = False
                for e in edits:
                    if not isinstance(e, dict):
                        continue
                    if str(e.get("file") or "") == t["file"] and str(e.get("function_signature") or "") == t["function_signature"]:
                        matched = True
                        break
                if not matched:
                    missing_required.append(t)

        if strict_required and missing_required:
            return {
                "success": False,
                "error": "strict_required=true 且 required_targets 未全覆盖",
                "fix_plan": fix_plan,
                "candidate_count": len(candidate_nodes),
                "required_target_count": len(required_targets),
                "extracted_count": extracted_count,
                "missing_required": missing_required,
            }

        return {
            "success": True,
            "fix_plan": fix_plan,
            "candidate_count": len(candidate_nodes),
            "required_target_count": len(required_targets),
            "extracted_count": extracted_count,
            "missing_required": missing_required,
        }

