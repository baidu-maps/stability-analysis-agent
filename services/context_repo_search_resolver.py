"""Context-loop resolvers backed by RepoSearchService (grep / read_file)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from services.repo_search import RepoSearchService, normalize_code_roots, render_repo_search_context


class GrepContextResolver:
    request_type = "grep"

    def __init__(
        self,
        *,
        code_roots: List[str],
        trace: Any = None,
    ):
        self.code_roots = normalize_code_roots(code_roots)
        self.trace = trace

    def resolve(self, request: Dict[str, Any]) -> Dict[str, Any]:
        pattern = str(request.get("symbol") or "").strip()
        if not pattern:
            return {
                "request": dict(request),
                "success": False,
                "error": "grep requires symbol (search pattern)",
            }
        if not self.code_roots:
            return {
                "request": dict(request),
                "success": False,
                "lookup_exhausted": True,
                "error": "code_roots unavailable for repo search",
            }
        service = RepoSearchService(code_roots=self.code_roots, trace=self.trace)
        path_glob = str(request.get("file") or "").strip()
        payload: Dict[str, Any] = {
            "mode": "grep",
            "code_roots": self.code_roots,
            "query": pattern,
        }
        if path_glob:
            payload["path_glob"] = path_glob
        out = service.execute(payload)
        matches = out.get("matches") or []
        if not matches:
            return {
                "request": dict(request),
                "success": False,
                "lookup_exhausted": True,
                "error": str(out.get("error") or "grep returned no matches"),
                "matches": [],
            }
        snippet_text = render_repo_search_context(out)
        snippet = [line for line in snippet_text.splitlines() if line.strip()]
        return {
            "request": dict(request),
            "success": True,
            "context_type": "grep",
            "matches": out.get("matches") or [],
            "snippet": snippet,
            "repo_search": out,
        }


class ReadFileContextResolver:
    request_type = "read_file"

    def __init__(
        self,
        *,
        code_roots: List[str],
        trace: Any = None,
    ):
        self.code_roots = normalize_code_roots(code_roots)
        self.trace = trace

    def resolve(self, request: Dict[str, Any]) -> Dict[str, Any]:
        file_path = str(request.get("file") or request.get("file_path") or "").strip()
        if not file_path:
            return {
                "request": dict(request),
                "success": False,
                "error": "read_file requires file path",
            }
        if not self.code_roots:
            return {
                "request": dict(request),
                "success": False,
                "lookup_exhausted": True,
                "error": "code_roots unavailable for repo search",
            }
        try:
            line_start = int(request.get("line_number") or request.get("line") or 1)
        except (TypeError, ValueError):
            line_start = 1
        try:
            line_end = int(request.get("line_end") or 0)
        except (TypeError, ValueError):
            line_end = 0
        if line_end <= 0:
            line_end = line_start + 79
        service = RepoSearchService(code_roots=self.code_roots, trace=self.trace)
        out = service.execute({
            "mode": "read_file",
            "code_roots": self.code_roots,
            "file_path": file_path,
            "line_start": max(1, line_start),
            "line_end": max(line_start, line_end),
        })
        if not out.get("success"):
            return {
                "request": dict(request),
                "success": False,
                "lookup_exhausted": True,
                "error": str(out.get("error") or "read_file failed"),
            }
        content = str(out.get("content") or "")
        snippet = content.splitlines() if content else []
        return {
            "request": dict(request),
            "success": True,
            "context_type": "read_file",
            "file": out.get("file_path") or file_path,
            "snippet_start_line": out.get("line_start"),
            "snippet_end_line": out.get("line_end"),
            "snippet": snippet,
            "repo_search": out,
        }


def build_repo_search_resolvers(
    *,
    code_roots: List[str],
    trace: Any = None,
) -> List[Any]:
    roots = normalize_code_roots(code_roots)
    return [
        GrepContextResolver(code_roots=roots, trace=trace),
        ReadFileContextResolver(code_roots=roots, trace=trace),
    ]
