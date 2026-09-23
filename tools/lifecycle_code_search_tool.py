"""围绕崩溃符号收集定义、赋值、释放、读写和调用点证据。"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from tool_system.tool import BaseTool, ToolDefinition


_SYMBOL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)+|m_[A-Za-z0-9_]+")
_CATEGORY_PATTERNS = {
    "release": re.compile(r"\b(delete|free|release|destroy|reset|close|quit|stop|cancel)\b", re.I),
    "initialize": re.compile(r"\b(new|create|init|open|start|register|addref)\b", re.I),
    "assignment": re.compile(r"(^|[^=!<>])=([^=]|$)"),
    "guard": re.compile(r"\b(if|assert|lock|unlock|mutex|atomic)\b", re.I),
}


class LifecycleCodeSearchTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="lifecycle_code_search",
            description="搜索崩溃符号的定义、初始化、赋值、释放、读写和调用点",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            category="analysis",
            risk="read_only",
            side_effect=False,
            idempotent=True,
            cost_class="medium",
        )

    def execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        roots = [
            Path(str(item)).expanduser().resolve()
            for item in input_data.get("code_roots") or []
            if Path(str(item)).expanduser().is_dir()
        ]
        symbols = self._symbols(input_data)
        max_results = max(10, min(int(input_data.get("max_results") or 120), 500))
        findings: List[Dict[str, Any]] = []
        for symbol in symbols[:12]:
            command = [
                "rg",
                "-n",
                "--no-heading",
                "--color",
                "never",
                "--glob",
                "*.{c,cc,cpp,cxx,h,hpp,mm,m}",
                "--fixed-strings",
                symbol,
                *[str(root) for root in roots],
            ]
            try:
                proc = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
            except (OSError, subprocess.TimeoutExpired):
                continue
            for line in proc.stdout.splitlines():
                category = "reference"
                for name, pattern in _CATEGORY_PATTERNS.items():
                    if pattern.search(line):
                        category = name
                        break
                findings.append({"symbol": symbol, "category": category, "location": line[:1200]})
                if len(findings) >= max_results:
                    break
            if len(findings) >= max_results:
                break
        category_counts: Dict[str, int] = {}
        for finding in findings:
            category = str(finding["category"])
            category_counts[category] = category_counts.get(category, 0) + 1
        return {
            "success": True,
            "symbols": symbols,
            "findings": findings,
            "category_counts": category_counts,
            "evidence_priority": "current_code",
        }

    @staticmethod
    def _symbols(input_data: Dict[str, Any]) -> List[str]:
        text = json.dumps(
            {
                "resolved_stack": input_data.get("resolved_stack") or {},
                "code_context": input_data.get("code_context") or {},
            },
            ensure_ascii=False,
        )
        seen = set()
        symbols = []
        for symbol in _SYMBOL_RE.findall(text):
            if symbol not in seen:
                seen.add(symbol)
                symbols.append(symbol)
        return symbols
