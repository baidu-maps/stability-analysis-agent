#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
在 code_roots 内按需检索：grep、读文件片段、查符号定义、查引用（调用点）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from tool_system.tool import BaseTool, ToolDefinition
from services.repo_search import RepoSearchService, normalize_code_roots


class RepoSearchTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="repo_search",
            description=(
                "在源码根目录内轻量检索：grep 文本、read_file 读片段、"
                "find_symbol 查定义、find_references 查调用点。用于跨文件根因与补上下文。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "code_roots": {
                        "type": "array",
                        "description": "源码根目录列表（必填）",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["grep", "read_file", "find_symbol", "find_references", "history", "find_tests"],
                        "description": "检索模式",
                        "default": "grep",
                    },
                    "query": {"type": "string", "description": "grep 模式或符号名"},
                    "pattern": {"type": "string", "description": "query 别名"},
                    "symbol_name": {"type": "string", "description": "符号名（find_symbol/find_references）"},
                    "file_path": {"type": "string", "description": "相对 code_root 的路径（read_file）"},
                    "line_start": {"type": "integer", "description": "read_file 起始行（1-based）"},
                    "line_end": {"type": "integer", "description": "read_file 结束行"},
                    "path_glob": {"type": "string", "description": "可选，缩小 grep 范围"},
                    "max_matches": {"type": "integer", "default": 80},
                    "resolved_stack": {
                        "type": "string",
                        "description": "可选 JSON，find_references 推断符号时用",
                    },
                    "use_ctags_index": {
                        "type": "boolean",
                        "description": "find_symbol 时是否使用 ctags 索引",
                        "default": False,
                    },
                    "use_repo_map": {"type": "boolean", "description": "使用崩溃 anchor 个性化代码地图排序"},
                    "stack_files": {"type": "array"},
                    "stack_symbols": {"type": "array"},
                    "fields": {"type": "array"},
                    "callers": {"type": "array"},
                    "include_tests": {"type": "boolean"},
                },
                "required": ["code_roots", "mode"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "mode": {"type": "string"},
                    "matches": {"type": "array"},
                    "definitions": {"type": "array"},
                    "content": {"type": "string"},
                    "error": {"type": "string"},
                    "stats": {"type": "object"},
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
        if "code_roots" not in input_data:
            return False, "缺少 required 字段: code_roots"
        mode = str(input_data.get("mode") or "").strip().lower()
        if mode not in ("grep", "read_file", "find_symbol", "find_references", "history", "find_tests"):
            return False, f"无效 mode: {mode}"
        if mode in ("grep", "find_symbol", "find_tests"):
            q = (
                input_data.get("query")
                or input_data.get("pattern")
                or input_data.get("symbol_name")
                or input_data.get("symbol")
            )
            if not str(q or "").strip():
                return False, f"{mode} 需要 query 或 symbol_name"
        if mode == "find_references":
            q = (
                input_data.get("symbol_name")
                or input_data.get("symbol")
                or input_data.get("query")
                or input_data.get("pattern")
                or input_data.get("resolved_stack")
            )
            if not str(q or "").strip():
                return False, "find_references 需要 symbol_name/query 或 resolved_stack"
        if mode == "read_file" and not str(input_data.get("file_path") or input_data.get("path") or "").strip():
            return False, "read_file 需要 file_path"
        if mode == "history" and not str(input_data.get("file_path") or input_data.get("path") or "").strip():
            return False, "history 需要 file_path"
        return True, None

    def execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        roots = normalize_code_roots(input_data.get("code_roots"))
        try:
            max_matches = int(input_data.get("max_matches", 80) or 80)
        except (TypeError, ValueError):
            max_matches = 80
        svc = RepoSearchService(
            roots,
            use_ctags_index=bool(input_data.get("use_ctags_index", False)),
            max_matches=max_matches,
            trace=input_data.get("_runtime_trace"),
        )
        return svc.execute(input_data)
