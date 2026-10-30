#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 AI 分析文本提取可应用修复代码（edits）的工具。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from tool_system.tool import BaseTool, ToolDefinition
from services.code_fixer import (
    CodeFixer,
    extract_candidate_nodes,
    _extract_include_directive_edits,
    _extract_member_declaration_edits,
    _extract_global_declaration_edits,
    _extract_required_function_names_from_analysis,
    _ensure_owner_class_methods_in_targets,
    _select_required_targets,
    _contains_placeholder_code,
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
                    "fix_source": {"type": "string"},
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

    @staticmethod
    def _scan_structured_fix_edits(analysis_text: str) -> List[Dict[str, str]]:
        """扫描 ```json 围栏里的权威 fix_edits（机器可读修复通道）。

        丢弃缺 file/replacement_code 或 replacement_code 含占位的条目。
        """
        out: List[Dict[str, str]] = []
        markers = ("省略", "其他逻辑保持不变", "// ...", "/* ...")
        for match in re.finditer(r"```json\s*(.*?)```", analysis_text, re.DOTALL | re.IGNORECASE):
            block = match.group(1).strip()
            if '"fix_edits"' not in block:
                continue
            try:
                payload = json.loads(block)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            fix_edits = payload.get("fix_edits")
            if not isinstance(fix_edits, list):
                continue
            for entry in fix_edits:
                if not isinstance(entry, dict):
                    continue
                file_name = str(entry.get("file") or "").strip()
                replacement = str(entry.get("replacement_code") or "").strip()
                signature = str(entry.get("function_signature") or "").strip()
                if not file_name or not replacement:
                    continue
                if any(m in replacement for m in markers) or _contains_placeholder_code(replacement):
                    continue
                out.append(
                    {
                        "file": file_name,
                        "function_signature": signature,
                        "replacement_code": replacement,
                        "reason": "structured_fix_edits",
                    }
                )
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
        # 机器可读通道优先：先解析 ```json {"fix_edits": [...]} ```，作为权威修复来源。
        structured_edits = self._scan_structured_fix_edits(analysis_text)
        text_fix_plan = fixer._try_extract_fix_plan_from_analysis(
            analysis_text=analysis_text,
            candidate_nodes=candidate_nodes,
            required_targets=required_targets,
        )
        if structured_edits:
            primary_edits: List[Dict[str, Any]] = list(structured_edits)
            seen_keys = {
                (e.get("file") or "", e.get("function_signature") or "")
                for e in primary_edits
            }
            text_edits = (
                text_fix_plan.get("edits")
                if isinstance(text_fix_plan, dict) and isinstance(text_fix_plan.get("edits"), list)
                else []
            )
            text_added = 0
            for e in text_edits:
                if not isinstance(e, dict):
                    continue
                key = (str(e.get("file") or ""), str(e.get("function_signature") or ""))
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                primary_edits.append(e)
                text_added += 1
            summary = str(text_fix_plan.get("summary", "")) if isinstance(text_fix_plan, dict) else ""
            fix_plan: Any = {"summary": summary, "edits": primary_edits}
            fix_source = "mixed" if text_added > 0 else "structured"
        else:
            fix_plan = text_fix_plan
            fix_source = "text"
        # 声明级/头文件级的 edit（include、成员/全局限定符）不依赖函数体，
        # 先于函数 fix_plan 的空判断计算，使“只有声明级改动”的分析也能成功。
        extra_edits = _extract_include_directive_edits(analysis_text, code_context)
        extra_edits.extend(_extract_member_declaration_edits(analysis_text, code_context))
        extra_edits.extend(_extract_global_declaration_edits(analysis_text, code_context))
        # 知识域确定性修复配方：命中即产出精确 old_text→new_text 的声明级 edit，
        # 不依赖大模型自行推理；候选文件里无法精确定位 old_text 时自动跳过。
        try:
            from knowledge_plane.fix_recipe import collect_recipe_edits as _collect_recipe
            from services.code_fixer import _collect_code_context_source_files as _cc_files
            recipe_files = [str(p) for p in _cc_files(code_context)]
            recipe_edits = _collect_recipe(
                trigger_text="\n".join([analysis_text, "\n".join(recipe_files)]),
                candidate_files=recipe_files,
            )
            extra_edits.extend(recipe_edits)
        except Exception as _recipe_exc:
            pass

        if not fix_plan and not extra_edits:
            return {
                "success": False,
                "error": "未能从分析文本中提取到可执行 edits",
                "candidate_count": len(candidate_nodes),
                "required_target_count": len(required_targets),
                "extracted_count": 0,
                "missing_required": required_targets,
                "fix_source": fix_source,
            }
        if not isinstance(fix_plan, dict):
            fix_plan = {"edits": []}

        edits = fix_plan.get("edits", []) if isinstance(fix_plan, dict) else []
        edits_list = edits if isinstance(edits, list) else []
        if extra_edits:
            edits_list.extend(extra_edits)
        # 合并后按 (file, function_signature) 去重（仅对带签名的函数级 edit，
        # 声明级 edit 无签名、保持原样）。结构化条目在前，故优先保留。
        deduped: List[Dict[str, Any]] = []
        seen_sig = set()
        for e in edits_list:
            if not isinstance(e, dict):
                continue
            sig = str(e.get("function_signature") or "").strip()
            if sig:
                key = (str(e.get("file") or ""), sig)
                if key in seen_sig:
                    continue
                seen_sig.add(key)
            deduped.append(e)
        fix_plan["edits"] = deduped
        edits = deduped
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
                "fix_source": fix_source,
            }

        return {
            "success": True,
            "fix_plan": fix_plan,
            "candidate_count": len(candidate_nodes),
            "required_target_count": len(required_targets),
            "extracted_count": extracted_count,
            "missing_required": missing_required,
            "fix_source": fix_source,
        }

