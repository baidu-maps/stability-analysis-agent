#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
代码片段提取工具（独立于 code_content_provider）。

用途：
- 按 file + line（可选 function_name）提取完整函数代码片段；
- 返回提取策略与行号范围，方便调试提取准确性。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from tool_system.tool import BaseTool, ToolDefinition
from services.code_locator import CodeLocatorService, LocatorConfig

try:
    from tree_sitter_languages import get_parser as _ts_get_parser
except Exception:
    _ts_get_parser = None


class SnippetExtractorTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="snippet_extractor",
            description="按文件+行号提取源码函数片段，返回策略、片段和行号范围。",
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "源码文件绝对路径"},
                    "line_number": {"type": "integer", "description": "1-based 目标行号"},
                    "function_name": {"type": "string", "description": "可选，目标函数名 token（如 GetLegSize）"},
                    "max_code_length": {"type": "integer", "description": "最大保留行数，<=0 表示不截断", "default": 0},
                },
                "required": ["file_path", "line_number"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "line_number": {"type": "integer"},
                    "function_name": {"type": "string"},
                    "strategy": {"type": "string"},
                    "backend": {"type": "string"},
                    "is_complete_function": {"type": "boolean"},
                    "incomplete_reason": {"type": "string"},
                    "snippet_start_line": {"type": "integer"},
                    "snippet_end_line": {"type": "integer"},
                    "snippet": {"type": "array", "items": {"type": "string"}},
                    "error": {"type": "string"},
                },
            },
            category="provider",
            version="1.0.0",
            risk="read_only",
            side_effect=False,
            idempotent=True,
            requires_approval=False,
            cost_class="low",
        )

    def validate_input(self, input_data: Dict[str, Any]) -> "tuple[bool, Optional[str]]":
        if "file_path" not in input_data:
            return False, "缺少 required 字段: file_path"
        if "line_number" not in input_data:
            return False, "缺少 required 字段: line_number"
        return True, None

    def _build_locator(self) -> CodeLocatorService:
        ts_parser = None
        if _ts_get_parser is not None:
            try:
                ts_parser = _ts_get_parser("cpp")
            except Exception:
                ts_parser = None
        cfg = LocatorConfig(max_code_length=0)
        return CodeLocatorService(config=cfg, ts_parser=ts_parser)

    def execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        file_path_raw = str(input_data.get("file_path") or "").strip()
        line_number_raw = input_data.get("line_number")
        function_name = str(input_data.get("function_name") or "").strip()
        max_code_length = int(input_data.get("max_code_length") or 0)

        file_path = str(Path(file_path_raw).expanduser().resolve()) if file_path_raw else ""
        try:
            line_number = int(line_number_raw)
        except Exception:
            return {"error": "line_number 必须为正整数"}
        if not file_path:
            return {"error": "file_path 不能为空"}
        if line_number <= 0:
            return {"error": "line_number 必须 > 0"}
        if not Path(file_path).is_file():
            return {"error": f"文件不存在: {file_path}"}

        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.read().splitlines()
        except Exception as exc:
            return {"error": f"读取文件失败: {exc}"}

        if not lines:
            return {
                "file_path": file_path,
                "line_number": line_number,
                "function_name": function_name,
                "strategy": "empty_file",
                "snippet_start_line": 1,
                "snippet_end_line": 1,
                "snippet": [],
            }

        idx = min(max(0, line_number - 1), len(lines) - 1)
        locator = self._build_locator()
        symbol = locator.symbol_locator

        candidate_name = function_name or symbol.extract_function_name_at_line(lines, line_number) or ""
        body = symbol.extract_full_function_code(lines, idx, candidate_name or None)
        strategy = "function_body"
        backend = getattr(symbol, "_last_extract_backend", "unknown")

        if body:
            snippet = [ln.rstrip() for ln in body.split("\n")]
            start_idx = idx
            if snippet:
                loc = self._locate_snippet(lines, snippet, idx)
                if loc is not None:
                    start_idx, end_idx = loc
                else:
                    end_idx = min(len(lines) - 1, start_idx + len(snippet) - 1)
            else:
                end_idx = idx
        else:
            strategy = "line_window"
            backend = "line_window"
            win = 20
            start_idx = max(0, idx - win)
            end_idx = min(len(lines) - 1, idx + win)
            snippet = [lines[i].rstrip() for i in range(start_idx, end_idx + 1)]

        if max_code_length > 0 and len(snippet) > max_code_length:
            snippet = locator.ctx.truncate_snippet(snippet, max_code_length)
            strategy = f"{strategy}_truncated"

        is_complete, incomplete_reason = self._check_complete_function(snippet)

        return {
            "file_path": file_path,
            "line_number": line_number,
            "function_name": candidate_name,
            "strategy": strategy,
            "backend": backend,
            "is_complete_function": is_complete,
            "incomplete_reason": incomplete_reason,
            "snippet_start_line": start_idx + 1,
            "snippet_end_line": (start_idx + len(snippet)) if snippet else (end_idx + 1),
            "snippet": snippet,
        }

    @staticmethod
    def _locate_snippet(lines: List[str], snippet: List[str], anchor_idx: int) -> Optional[tuple[int, int]]:
        if not lines or not snippet:
            return None
        first = snippet[0].strip()
        if not first:
            return None
        n = len(lines)
        span = len(snippet)
        lo = max(0, anchor_idx - 200)
        hi = min(n - span, anchor_idx + 200)
        best: Optional[tuple[int, int]] = None
        for i in range(lo, hi + 1):
            if lines[i].strip() != first:
                continue
            ok = True
            for k in range(span):
                if i + k >= n or lines[i + k].strip() != snippet[k].strip():
                    ok = False
                    break
            if not ok:
                continue
            s, e = i, i + span - 1
            if s <= anchor_idx <= e:
                return s, e
            if best is None:
                best = (s, e)
        return best

    @staticmethod
    def _check_complete_function(snippet: List[str]) -> tuple[bool, str]:
        if not snippet:
            return False, "empty_snippet"
        seen_open = False
        depth = 0
        for line in snippet:
            for ch in str(line):
                if ch == "{":
                    depth += 1
                    seen_open = True
                elif ch == "}":
                    if seen_open:
                        depth -= 1
                        if depth == 0:
                            return True, ""
        if not seen_open:
            return False, "missing_open_brace"
        return False, "missing_closing_brace"

