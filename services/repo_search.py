#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 code_roots 内的轻量仓库检索（grep / 读文件 / 符号 / 引用）。"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from services.code_locator import LocatorConfig, LocatorContext

_DEFAULT_EXCLUDE = frozenset({
    "test",
    "tests",
    "testing",
    "third_party",
    "third-party",
    "thirdparty",
    "vendor",
    "external",
    "build",
    "builds",
    "out",
    "output",
    "bin",
    "obj",
    "generated",
    "node_modules",
    ".git",
    ".svn",
    ".hg",
    "docs",
    "doc",
})

_SUPPORTED_EXT = frozenset({
    ".c",
    ".cpp",
    ".cc",
    ".cxx",
    ".h",
    ".hpp",
    ".hxx",
    ".m",
    ".mm",
    ".java",
    ".kt",
    ".swift",
    ".py",
})


def normalize_code_roots(code_roots: Any) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for r in code_roots or []:
        if not r:
            continue
        try:
            p = str(Path(str(r)).expanduser().resolve())
        except Exception:
            continue
        if p in seen or not os.path.isdir(p):
            continue
        seen.add(p)
        out.append(p)
    return out


def path_under_code_roots(file_path: str, code_roots: List[str]) -> bool:
    if not file_path or not code_roots:
        return False
    try:
        abs_fp = os.path.abspath(file_path)
    except Exception:
        return False
    for root in code_roots:
        try:
            abs_root = os.path.abspath(root)
            if os.path.commonpath([abs_fp, abs_root]) == abs_root:
                return True
        except (ValueError, OSError):
            continue
    return False


def render_repo_search_context(payload: Dict[str, Any], max_lines: int = 40) -> str:
    """将单次 repo_search 结果渲染为可拼入提示词的 Markdown。"""
    if not isinstance(payload, dict) or not payload.get("success"):
        return ""
    mode = str(payload.get("mode") or "")
    parts: List[str] = [f"### repo_search ({mode})"]
    if payload.get("query"):
        parts.append(f"- 查询: `{payload.get('query')}`")
    if payload.get("symbol_name"):
        parts.append(f"- 符号: `{payload.get('symbol_name')}`")

    matches = payload.get("matches") or []
    if isinstance(matches, list) and matches:
        parts.append("匹配:")
        for m in matches[:max_lines]:
            if not isinstance(m, dict):
                continue
            fp = m.get("file", "")
            ln = m.get("line", "")
            text = str(m.get("line_text") or m.get("text") or "").strip()
            if len(text) > 200:
                text = text[:200] + "..."
            parts.append(f"- `{fp}:{ln}` {text}")
        if len(matches) > max_lines:
            parts.append(f"- ... 另有 {len(matches) - max_lines} 条（已截断）")

    content = payload.get("content")
    if isinstance(content, str) and content.strip():
        snippet = content.strip()
        if len(snippet) > 4000:
            snippet = snippet[:4000] + "\n... (已截断)"
        parts.append("文件片段:")
        parts.append("```")
        parts.append(snippet)
        parts.append("```")

    definitions = payload.get("definitions") or []
    if isinstance(definitions, list) and definitions:
        parts.append("定义:")
        for d in definitions[:20]:
            if isinstance(d, dict):
                parts.append(f"- `{d.get('file')}:{d.get('line')}` {d.get('name', '')}")

    if payload.get("truncated"):
        parts.append("- （结果已截断）")
    return "\n".join(parts).strip()


def merge_repo_search_context(blocks: List[str]) -> str:
    lines = [b for b in blocks if isinstance(b, str) and b.strip()]
    if not lines:
        return ""
    return "## 仓库检索补充\n\n" + "\n\n".join(lines)


class RepoSearchService:
    def __init__(
        self,
        code_roots: List[str],
        *,
        use_ctags_index: bool = False,
        max_matches: int = 80,
    ) -> None:
        self.code_roots = normalize_code_roots(code_roots)
        self.max_matches = max(1, min(int(max_matches), 500))
        cfg = LocatorConfig(
            exclude_dirs=_DEFAULT_EXCLUDE,
            supported_extensions=_SUPPORTED_EXT,
            use_ctags_index=bool(use_ctags_index),
        )
        self._ctx = LocatorContext(cfg)

    def execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        t0 = time.time()
        if not self.code_roots:
            return {
                "success": False,
                "error": "code_roots 为空或无效",
                "mode": input_data.get("mode"),
            }

        mode = str(input_data.get("mode") or "grep").strip().lower()
        query = str(input_data.get("query") or input_data.get("pattern") or "").strip()
        symbol = str(input_data.get("symbol_name") or input_data.get("symbol") or query).strip()

        try:
            if mode == "read_file":
                out = self._read_file(input_data)
            elif mode == "find_symbol":
                out = self._find_symbol(symbol or query)
            elif mode == "find_references":
                out = self._find_references(symbol or query, input_data)
            elif mode == "grep":
                out = self._grep(query or symbol, input_data)
            else:
                return {
                    "success": False,
                    "error": f"不支持的 mode: {mode}",
                    "mode": mode,
                }
        except Exception as exc:
            return {
                "success": False,
                "error": str(exc),
                "mode": mode,
                "query": query or symbol,
            }

        out["success"] = True
        out["mode"] = mode
        out["stats"] = {
            "elapsed_ms": int((time.time() - t0) * 1000),
            "roots_count": len(self.code_roots),
            "rg_used": LocatorContext._rg_available is not False,
        }
        if query:
            out["query"] = query
        if symbol and mode != "grep":
            out["symbol_name"] = symbol
        return out

    def _grep(self, pattern: str, input_data: Dict[str, Any]) -> Dict[str, Any]:
        if not pattern:
            return {"success": False, "error": "grep 需要 query/pattern"}
        max_matches = self._int_cap(input_data.get("max_matches"), self.max_matches)
        paths = self._search_paths(input_data)
        rows = self._ctx.rg_grep_lines(pattern, paths, max_matches=max_matches)
        matches = self._rows_to_matches(rows, max_matches)
        if matches is None:
            matches = self._python_grep(pattern, paths, max_matches)
        truncated = len(matches) >= max_matches
        return {"matches": matches, "truncated": truncated}

    def _find_references(self, symbol_name: str, input_data: Dict[str, Any]) -> Dict[str, Any]:
        if not symbol_name:
            return {"success": False, "error": "find_references 需要 symbol_name/query"}
        from tools.symbol_callsite_finder_tool import SymbolCallsiteFinderTool

        max_results = self._int_cap(input_data.get("max_matches"), self.max_matches)
        tool = SymbolCallsiteFinderTool()
        raw = tool.execute(
            {
                "symbol_name": symbol_name,
                "code_roots": self.code_roots,
                "resolved_stack": input_data.get("resolved_stack"),
                "max_results": max_results,
            }
        )
        candidates = raw.get("callsite_candidates") or []
        matches = []
        for c in candidates:
            if not isinstance(c, dict):
                continue
            matches.append(
                {
                    "file": c.get("file"),
                    "line": c.get("line"),
                    "line_text": c.get("line_text"),
                    "confidence": c.get("confidence", "text_match"),
                }
            )
        return {
            "matches": matches,
            "candidate_files": raw.get("candidate_files") or [],
            "truncated": len(matches) >= max_results,
            "stats_extra": raw.get("stats"),
        }

    def _find_symbol(self, name: str) -> Dict[str, Any]:
        if not name:
            return {"success": False, "error": "find_symbol 需要 symbol_name/query"}
        definitions: List[Dict[str, Any]] = []
        if self._ctx.config.use_ctags_index:
            self._ctx.ensure_ctags_index(self.code_roots)
            if self._ctx._ctags_index is not None:
                loc = self._ctx._ctags_index.lookup(name, self.code_roots)
                if loc:
                    fp, ln = loc
                    definitions.append({"name": name, "file": fp, "line": ln, "source": "ctags"})
        if not definitions:
            pat = re.compile(rf"\b{re.escape(name)}\s*\([^;{{]*\)\s*(?:const\s*)?", re.MULTILINE)
            max_matches = min(self.max_matches, 30)
            rows = self._ctx.rg_grep_lines(
                rf"\b{re.escape(name)}\s*\(",
                self.code_roots,
                max_matches=max_matches * 3,
            )
            if rows:
                for fp, ln, text in rows:
                    if not path_under_code_roots(fp, self.code_roots):
                        continue
                    if pat.search(text.strip()):
                        definitions.append(
                            {"name": name, "file": fp, "line": ln, "line_text": text.strip(), "source": "rg"}
                        )
                    if len(definitions) >= max_matches:
                        break
        return {"definitions": definitions, "truncated": len(definitions) >= self.max_matches}

    def _read_file(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        rel = str(input_data.get("file_path") or input_data.get("path") or "").strip()
        if not rel:
            return {"success": False, "error": "read_file 需要 file_path"}
        target: Optional[str] = None
        for root in self.code_roots:
            cand = os.path.join(root, rel.lstrip("/"))
            if os.path.isfile(cand):
                target = os.path.abspath(cand)
                break
        if not target:
            abs_try = os.path.abspath(rel)
            if os.path.isfile(abs_try) and path_under_code_roots(abs_try, self.code_roots):
                target = abs_try
        if not target:
            return {"success": False, "error": f"文件不在 code_roots 内或不存在: {rel}"}

        try:
            line_start = int(input_data.get("line_start", 1))
            line_end = int(input_data.get("line_end", line_start + 79))
        except (TypeError, ValueError):
            line_start, line_end = 1, 80
        line_start = max(1, line_start)
        line_end = max(line_start, min(line_end, line_start + 199))

        lines: List[str] = []
        with open(target, "r", encoding="utf-8", errors="ignore") as f:
            for idx, ln in enumerate(f, start=1):
                if idx < line_start:
                    continue
                if idx > line_end:
                    break
                lines.append(ln.rstrip("\n\r"))
        content = "\n".join(lines)
        max_bytes = 32_000
        truncated = len(content.encode("utf-8", errors="ignore")) > max_bytes
        if truncated:
            content = content[:max_bytes] + "\n... (已截断)"
        return {
            "file_path": target,
            "line_start": line_start,
            "line_end": line_end,
            "content": content,
            "truncated": truncated,
        }

    def _search_paths(self, input_data: Dict[str, Any]) -> List[str]:
        glob_hint = str(input_data.get("path_glob") or "").strip()
        if not glob_hint:
            return list(self.code_roots)
        paths: List[str] = []
        for root in self.code_roots:
            try:
                for p in Path(root).rglob(glob_hint.lstrip("./")):
                    if p.is_file() and path_under_code_roots(str(p), self.code_roots):
                        paths.append(str(p.resolve()))
            except Exception:
                continue
        return paths or list(self.code_roots)

    @staticmethod
    def _int_cap(value: Any, default: int) -> int:
        try:
            n = int(value)
        except (TypeError, ValueError):
            n = default
        return max(1, min(n, 500))

    def _rows_to_matches(
        self,
        rows: Optional[List[Tuple[str, int, str]]],
        max_matches: int,
    ) -> Optional[List[Dict[str, Any]]]:
        if rows is None:
            return None
        out: List[Dict[str, Any]] = []
        for fp, ln, text in rows:
            if not path_under_code_roots(fp, self.code_roots):
                continue
            out.append({"file": fp, "line": ln, "line_text": text.strip()})
            if len(out) >= max_matches:
                break
        return out

    def _python_grep(self, pattern: str, search_paths: List[str], max_matches: int) -> List[Dict[str, Any]]:
        try:
            rx = re.compile(pattern)
        except re.error:
            rx = re.compile(re.escape(pattern))
        out: List[Dict[str, Any]] = []
        for base in search_paths:
            if os.path.isfile(base):
                files = [base]
                walk_root = None
            else:
                files = []
                walk_root = base
            if walk_root:
                for dirpath, dirnames, filenames in os.walk(walk_root):
                    dirnames[:] = [
                        d
                        for d in dirnames
                        if d not in _DEFAULT_EXCLUDE and not d.startswith(".")
                    ]
                    for fn in filenames:
                        if Path(fn).suffix.lower() not in _SUPPORTED_EXT:
                            continue
                        files.append(os.path.join(dirpath, fn))
            for fp in files:
                if len(out) >= max_matches:
                    return out
                if Path(fp).suffix.lower() not in _SUPPORTED_EXT:
                    continue
                try:
                    with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                        for idx, ln in enumerate(f, start=1):
                            if rx.search(ln):
                                out.append(
                                    {"file": str(Path(fp).resolve()), "line": idx, "line_text": ln.strip()}
                                )
                                if len(out) >= max_matches:
                                    return out
                except Exception:
                    continue
        return out


def infer_symbol_from_resolved_stack(resolved: Dict[str, Any]) -> str:
    from tools.resolve_stack_errors import flatten_resolved_frames_from_stack

    frames = flatten_resolved_frames_from_stack(resolved) or resolved.get("frames") or []
    if not isinstance(frames, list):
        return ""
    for fr in frames:
        if not isinstance(fr, dict):
            continue
        rf = str(fr.get("resolved_function") or fr.get("function") or "").strip()
        if not rf:
            continue
        rf = re.sub(r"<[^<>]*>", "", rf)
        m = re.search(r"([A-Za-z_~]\w*)\s*\(", rf)
        if m:
            return m.group(1)
    return ""
