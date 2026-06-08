#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从规则库 + 向量库检索历史崩溃经验，输出 memory_context（供 05 提示词拼接）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from tool_system.tool import BaseTool, ToolDefinition
from rag.memory_retriever import collect_memory_context


class VectorMemoryRetrieverTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="vector_memory_retriever",
            description=(
                "基于 01/02/03 特征做规则匹配与向量相似案例检索，"
                "生成可拼入 AI 提示词的经验上下文（memory_context）。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "parse_result": {
                        "type": "object",
                        "description": "01_crash_log_parser.json 对应对象",
                    },
                    "resolved_stack": {
                        "type": "object",
                        "description": "02_add2line_resolver.json 对应对象",
                    },
                    "code_context": {
                        "type": "object",
                        "description": "03_code_content_provider.json 对应对象",
                    },
                    "vector_db_path": {
                        "type": "string",
                        "description": "向量库目录，默认 ./vector_db",
                        "default": "./vector_db",
                    },
                    "rule_confidence_threshold": {
                        "type": "number",
                        "description": "规则命中置信度阈值",
                        "default": 0.85,
                    },
                    "vector_db_max_results": {
                        "type": "integer",
                        "description": "向量召回条数上限",
                        "default": 3,
                    },
                    "vector_db_readonly": {
                        "type": "boolean",
                        "description": "只读检索，不更新 hit_count 等元数据（默认 true）",
                        "default": True,
                    },
                },
                "required": ["parse_result", "resolved_stack", "code_context"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "skipped": {"type": "boolean"},
                    "skip_reason": {"type": ["string", "null"]},
                    "memory_context": {"type": "string"},
                    "rule_hits": {"type": "array"},
                    "pattern_hits": {"type": "array"},
                    "evidence_map": {"type": "object"},
                    "strategy_hits": {"type": "array"},
                    "decision_trace": {"type": "array"},
                    "vector_used": {"type": "boolean"},
                    "user_message": {"type": "string"},
                    "error": {"type": "string"},
                },
            },
            category="provider",
            version="1.0.0",
        )

    def validate_input(self, input_data: Dict[str, Any]) -> "tuple[bool, Optional[str]]":
        for field in ("parse_result", "resolved_stack", "code_context"):
            if field not in input_data:
                return False, f"缺少 required 字段: {field}"
        return True, None

    def execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        parse_result = input_data.get("parse_result") or {}
        resolved_stack = input_data.get("resolved_stack") or {}
        code_context = input_data.get("code_context") or {}
        if not isinstance(parse_result, dict):
            parse_result = {}
        if not isinstance(resolved_stack, dict):
            resolved_stack = {}
        if not isinstance(code_context, dict):
            code_context = {}

        vector_db_path = str(input_data.get("vector_db_path") or "./vector_db")
        try:
            rule_threshold = float(input_data.get("rule_confidence_threshold", 0.85))
        except (TypeError, ValueError):
            rule_threshold = 0.85
        try:
            max_results = int(input_data.get("vector_db_max_results", 3))
        except (TypeError, ValueError):
            max_results = 3
        readonly_raw = input_data.get("vector_db_readonly", True)
        if isinstance(readonly_raw, str):
            vector_db_readonly = readonly_raw.strip().lower() not in ("0", "false", "no", "off")
        else:
            vector_db_readonly = bool(readonly_raw) if readonly_raw is not None else True

        return collect_memory_context(
            parse_result=parse_result,
            resolved_stack=resolved_stack,
            code_context=code_context,
            vector_db_path=vector_db_path,
            rule_confidence_threshold=rule_threshold,
            vector_db_max_results=max_results,
            vector_db_readonly=vector_db_readonly,
        )
