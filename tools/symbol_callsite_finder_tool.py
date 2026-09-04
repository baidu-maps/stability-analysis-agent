#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
快速符号调用点发现工具（轻量文本级召回）。

目标：
- 在大 code_root 上先快速找出「可能调用崩溃函数」的文件与行号；
- 输出候选文件列表，供后续 code_content_provider 做深度静态分析时优先扫描。
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from tool_system.tool import BaseTool, ToolDefinition

logger = logging.getLogger(__name__)


class SymbolCallsiteFinderTool(BaseTool):
    _rg_available: Optional[bool] = None  # 类级缓存，只检测一次

    def __init__(self) -> None:
        self.supported_extensions = {".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hxx", ".m", ".mm"}
        if SymbolCallsiteFinderTool._rg_available is None:
            SymbolCallsiteFinderTool._rg_available = self._check_rg_available()
        self.default_exclude_dirs = {
            ".git",
            ".svn",
            ".hg",
            "build",
            "out",
            "output",
            "bin",
            "obj",
            "third_party",
            "third-party",
            "thirdparty",
            "vendor",
            "external",
            "node_modules",
            "docs",
            "doc",
            "test",
            "tests",
            "generated",
        }

    @staticmethod
    def _check_rg_available() -> bool:
        """检测 rg (ripgrep) 是否可用，仅在首次实例化时调用一次。"""
        try:
            subprocess.run(["rg", "--version"], check=False, capture_output=True, timeout=5)
            return True
        except (FileNotFoundError, OSError):
            logger.info(
                "提示: ripgrep (rg) 未安装，调用点搜索将使用较慢的 Python 回退方式。"
                " 安装后可显著提升扫描速度: brew install ripgrep (macOS) / apt install ripgrep (Linux)"
            )
            return False

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="symbol_callsite_finder",
            description="在代码根目录内快速发现函数调用点（轻量文本检索），输出候选文件与行号。",
            input_schema={
                "type": "object",
                "properties": {
                    "symbol_name": {"type": "string", "description": "函数名（可选；为空时可从 resolved_stack 推断）"},
                    "resolved_stack": {"type": "string", "description": "add2line 结果 JSON（可选，用于自动推断 symbol）"},
                    "code_roots": {"type": "array", "description": "代码根目录列表"},
                    "max_results": {"type": "integer", "description": "最大返回调用点条数（默认 300）"},
                },
                "required": ["code_roots"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "symbol_name": {"type": "string"},
                    "callsite_candidates": {"type": "array"},
                    "candidate_files": {"type": "array"},
                    "stats": {"type": "object"},
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

    def _extract_symbol_from_resolved(self, resolved_function: str) -> str:
        s = (resolved_function or "").strip()
        if not s:
            return ""
        s = re.sub(r"<[^<>]*>", "", s)
        m = re.search(r"([A-Za-z_~]\w*)\s*\(", s)
        if m:
            return m.group(1)
        return ""

    def _infer_symbol_from_stack(self, resolved_stack_json: str) -> str:
        try:
            data = json.loads(resolved_stack_json or "{}")
            from tools.resolve_stack_errors import flatten_resolved_frames_from_stack

            frames = flatten_resolved_frames_from_stack(data)
            if not frames:
                return ""
            for fr in frames:
                if not isinstance(fr, dict):
                    continue
                rf = str(fr.get("resolved_function") or "").strip()
                if rf:
                    name = self._extract_symbol_from_resolved(rf)
                    if name:
                        return name
        except Exception:
            return ""
        return ""

    def _normalize_code_roots(self, code_roots: Any) -> List[str]:
        out: List[str] = []
        seen = set()
        for r in code_roots or []:
            if not r:
                continue
            p = str(Path(str(r)).expanduser().resolve())
            if p in seen:
                continue
            if os.path.isdir(p):
                seen.add(p)
                out.append(p)
        return out

    def _is_probable_definition(self, line: str, symbol_name: str) -> bool:
        ln = line.strip()
        if not ln:
            return False
        if f"->{symbol_name}(" in ln or f".{symbol_name}(" in ln or f"::{symbol_name}(" in ln:
            return False
        pat = re.compile(
            rf"^[\w\s:<>,~*&]+?\b{re.escape(symbol_name)}\s*\([^;]*\)\s*(?:const\s*)?(?:noexcept\s*)?(?:\{{)?\s*$"
        )
        return bool(pat.search(ln))

    def _scan_via_rg(self, symbol_name: str, code_roots: List[str], max_results: int) -> List[Dict[str, Any]]:
        if not SymbolCallsiteFinderTool._rg_available:
            return []
        cmd = [
            "rg",
            "-n",
            "--no-heading",
            "--color",
            "never",
            "--glob",
            "*.{c,cc,cpp,cxx,h,hpp,hxx,m,mm}",
            rf"\b{re.escape(symbol_name)}\s*\(",
            *code_roots,
        ]
        try:
            proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
        except Exception as exc:
            logger.debug("symbol_callsite_finder: rg 调用异常: %s", exc)
            SymbolCallsiteFinderTool._rg_available = False
            return []

        if proc.returncode not in (0, 1):
            logger.warning("symbol_callsite_finder: rg 返回异常 code=%s", proc.returncode)
            return []

        rows: List[Dict[str, Any]] = []
        for raw in (proc.stdout or "").splitlines():
            if len(rows) >= max_results:
                break
            m = re.match(r"^(.*?):(\d+):(.*)$", raw)
            if not m:
                continue
            file_path, line_no_s, line_text = m.group(1), m.group(2), m.group(3)
            ext = Path(file_path).suffix.lower()
            if ext not in self.supported_extensions:
                continue
            if self._is_probable_definition(line_text, symbol_name):
                continue
            rows.append(
                {
                    "file": str(Path(file_path).resolve()),
                    "line": int(line_no_s),
                    "line_text": line_text.strip(),
                    "confidence": "text_match",
                }
            )
        return rows

    def _scan_via_python_walk(self, symbol_name: str, code_roots: List[str], max_results: int) -> List[Dict[str, Any]]:
        pat = re.compile(rf"\b{re.escape(symbol_name)}\s*\(")
        rows: List[Dict[str, Any]] = []
        for root in code_roots:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in self.default_exclude_dirs and not d.startswith(".")]
                for fn in filenames:
                    if len(rows) >= max_results:
                        return rows
                    ext = Path(fn).suffix.lower()
                    if ext not in self.supported_extensions:
                        continue
                    fp = os.path.join(dirpath, fn)
                    try:
                        with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                            for idx, ln in enumerate(f, start=1):
                                if not pat.search(ln):
                                    continue
                                if self._is_probable_definition(ln, symbol_name):
                                    continue
                                rows.append(
                                    {
                                        "file": str(Path(fp).resolve()),
                                        "line": idx,
                                        "line_text": ln.strip(),
                                        "confidence": "text_match",
                                    }
                                )
                                if len(rows) >= max_results:
                                    return rows
                    except Exception:
                        continue
        return rows

    def execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        code_roots = self._normalize_code_roots(input_data.get("code_roots"))
        if not code_roots:
            return {"symbol_name": "", "callsite_candidates": [], "candidate_files": [], "stats": {"error": "code_roots 为空"}}

        symbol_name = str(input_data.get("symbol_name") or "").strip()
        if not symbol_name:
            symbol_name = self._infer_symbol_from_stack(str(input_data.get("resolved_stack") or ""))
        if not symbol_name:
            return {
                "symbol_name": "",
                "callsite_candidates": [],
                "candidate_files": [],
                "stats": {"error": "无法从输入推断 symbol_name"},
            }

        try:
            max_results = int(input_data.get("max_results", 300) or 300)
        except Exception:
            max_results = 300
        max_results = max(20, min(max_results, 5000))

        candidates = self._scan_via_rg(symbol_name, code_roots, max_results=max_results)
        if not candidates:
            candidates = self._scan_via_python_walk(symbol_name, code_roots, max_results=max_results)
        files: List[str] = []
        seen = set()
        for it in candidates:
            fp = str(it.get("file") or "")
            if not fp or fp in seen:
                continue
            seen.add(fp)
            files.append(fp)

        return {
            "symbol_name": symbol_name,
            "callsite_candidates": candidates,
            "candidate_files": files,
            "stats": {
                "roots_count": len(code_roots),
                "candidate_count": len(candidates),
                "file_count": len(files),
                "max_results": max_results,
            },
        }

