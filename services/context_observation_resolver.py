"""Resolvers for observation, memory, and trace context requests."""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from services.context_engine import CallableContextResolver, ContextResolverRegistry
from services.context_repo_search_resolver import build_repo_search_resolvers


def _snippet_lines(text: str, *, limit: int = 6000) -> List[str]:
    lines = str(text or "").splitlines()
    if not lines:
        return []
    joined = "\n".join(lines)
    if len(joined) <= limit:
        return lines
    marker = "...[truncated]..."
    return (joined[: limit - len(marker)] + marker).splitlines()


class MemoryPatternResolver:
    request_type = "memory_pattern"

    def __init__(
        self,
        *,
        prepare: Dict[str, Any],
        problem: Optional[Dict[str, Any]],
    ):
        self.prepare = prepare if isinstance(prepare, dict) else {}
        self.problem = problem if isinstance(problem, dict) else {}

    def resolve(self, request: Dict[str, Any]) -> Dict[str, Any]:
        symbol = str(request.get("symbol") or "").strip()
        if not symbol:
            return {
                "request": dict(request),
                "success": False,
                "error": "memory_pattern requires symbol query",
            }
        parse_result = self.prepare.get("parse_result") or self.problem.get("parse_result")
        resolved_stack = (
            self.prepare.get("resolved_stack")
            or self.prepare.get("symbolized_stack")
            or self.problem.get("resolved_stack")
        )
        code_context = self.prepare.get("code_context") or self.problem.get("code_context") or {}
        if not isinstance(parse_result, dict) or not isinstance(resolved_stack, dict):
            return {
                "request": dict(request),
                "success": False,
                "lookup_exhausted": True,
                "error": "parse/symbolize artifacts unavailable for memory lookup",
            }
        try:
            from rag.memory_retriever import collect_memory_context

            payload = collect_memory_context(
                parse_result=parse_result,
                resolved_stack=resolved_stack,
                code_context=code_context if isinstance(code_context, dict) else {},
                vector_db_path=str(self.problem.get("vector_db_path") or "./vector_db"),
                rule_confidence_threshold=float(self.problem.get("rule_confidence_threshold") or 0.85),
                vector_db_max_results=int(self.problem.get("vector_db_max_results") or 3),
                vector_db_readonly=bool(self.problem.get("vector_db_readonly", True)),
                crash_log_content=str(self.problem.get("crash_log") or ""),
            )
            if payload.get("skipped"):
                return {
                    "request": dict(request),
                    "success": False,
                    "lookup_exhausted": True,
                    "error": str(payload.get("skip_reason") or "memory retrieval skipped"),
                }
            text = str(payload.get("memory_context") or "").strip()
            if symbol.lower() not in text.lower() and text:
                text = f"Query: {symbol}\n\n{text}"
            if not text:
                return {
                    "request": dict(request),
                    "success": False,
                    "lookup_exhausted": True,
                    "error": "no memory patterns matched query",
                }
            return {
                "request": dict(request),
                "success": True,
                "snippet": _snippet_lines(text),
                "memory_hits": payload.get("pattern_hits") or [],
            }
        except Exception as exc:
            return {
                "request": dict(request),
                "success": False,
                "lookup_exhausted": True,
                "error": f"memory retrieval unavailable: {exc}",
            }


class VerificationLogResolver:
    request_type = "verification_log"

    def __init__(
        self,
        *,
        observation_store: Any = None,
        verification: Optional[Dict[str, Any]] = None,
    ):
        self.observation_store = observation_store
        self.verification = verification if isinstance(verification, dict) else {}

    def resolve(self, request: Dict[str, Any]) -> Dict[str, Any]:
        provider = str(request.get("symbol") or "").strip().lower()
        lines: List[str] = []
        if self.verification:
            status = str(self.verification.get("status") or "unknown")
            lines.append(f"verification.status={status}")
            if self.verification.get("error"):
                lines.append(f"error: {self.verification.get('error')}")
            if self.verification.get("output"):
                lines.append(str(self.verification.get("output"))[:4000])
        if self.observation_store is not None and hasattr(self.observation_store, "items"):
            for item in self.observation_store.items():
                if item.get("kind") != "verification":
                    continue
                source = str(item.get("source") or "").lower()
                if provider and provider not in source:
                    continue
                lines.append(f"[{item.get('status')}] {item.get('source')}: {item.get('summary')}")
        if not lines:
            return {
                "request": dict(request),
                "success": False,
                "lookup_exhausted": True,
                "error": "no verification log available",
            }
        return {
            "request": dict(request),
            "success": True,
            "snippet": _snippet_lines("\n".join(lines)),
        }


class TraceSnippetResolver:
    request_type = "trace_snippet"

    def __init__(self, *, trace: Any = None):
        self.trace = trace

    def resolve(self, request: Dict[str, Any]) -> Dict[str, Any]:
        prefix = str(request.get("symbol") or "recent").strip().lower()
        events: List[Dict[str, Any]] = []
        if self.trace is not None and hasattr(self.trace, "snapshot"):
            snapshot = self.trace.snapshot()
            raw = snapshot.get("events") if isinstance(snapshot, dict) else []
            if isinstance(raw, list):
                events = [x for x in raw if isinstance(x, dict)]
        elif isinstance(self.trace, dict):
            raw = self.trace.get("events")
            if isinstance(raw, list):
                events = [x for x in raw if isinstance(x, dict)]
        filtered: List[str] = []
        for event in reversed(events):
            name = str(event.get("event") or event.get("name") or "")
            status = str(event.get("status") or "")
            if prefix not in {"", "recent"} and prefix not in name.lower():
                continue
            if prefix == "recent" and status not in {"failed", "denied", "error"} and event.get("kind") != "policy":
                if not name.endswith(".failed") and event.get("status") != "denied":
                    continue
            filtered.append(f"{name} status={status} reason={event.get('reason') or event.get('error') or ''}")
            if len(filtered) >= 12:
                break
        if not filtered:
            return {
                "request": dict(request),
                "success": False,
                "lookup_exhausted": True,
                "error": "no matching trace events",
            }
        return {
            "request": dict(request),
            "success": True,
            "snippet": _snippet_lines("\n".join(reversed(filtered))),
        }


def build_observation_resolvers(
    *,
    prepare: Dict[str, Any],
    problem: Optional[Dict[str, Any]],
    context: Any = None,
    trace: Any = None,
) -> List[CallableContextResolver]:
    verification = {}
    if isinstance(problem, dict):
        ver = problem.get("verification")
        if isinstance(ver, dict):
            verification = ver
    observation_store = getattr(context, "observations", None) if context is not None else None
    resolvers = [
        MemoryPatternResolver(prepare=prepare, problem=problem),
        VerificationLogResolver(observation_store=observation_store, verification=verification),
        TraceSnippetResolver(trace=trace),
    ]
    return [CallableContextResolver(item.request_type, item.resolve) for item in resolvers]


def build_context_resolver_registry(
    *,
    prepare: Dict[str, Any],
    problem: Optional[Dict[str, Any]],
    context: Any,
    trace: Any,
    code_resolver: Callable[[Dict[str, Any]], Dict[str, Any]],
    code_roots: Optional[List[str]] = None,
) -> ContextResolverRegistry:
    registry = ContextResolverRegistry()
    for request_type in ("function", "field", "references", "callers"):
        registry.register(CallableContextResolver(request_type, code_resolver))
    roots = list(code_roots or [])
    if not roots and isinstance(prepare, dict):
        roots = list(prepare.get("code_roots") or [])
    if not roots and isinstance(problem, dict):
        roots = list(problem.get("code_roots") or [])
    for resolver in build_repo_search_resolvers(code_roots=roots, trace=trace):
        registry.register(CallableContextResolver(resolver.request_type, resolver.resolve))
    for resolver in build_observation_resolvers(
        prepare=prepare,
        problem=problem,
        context=context,
        trace=trace,
    ):
        registry.register(resolver)
    return registry


def supported_context_request_types_doc() -> str:
    return (
        "`function`→函数完整源码；`field`→成员声明（优先 .h）；"
        "`references`→读写/引用位置；`callers`→调用方片段；"
        "`grep`→仓库文本/正则搜索（symbol=pattern，可选 file=path_glob）；"
        "`read_file`→按路径读取源码片段（file 必填，line_number/line_end 可选）；"
        "`memory_pattern`→相似 crash 模式/经验（symbol 为查询词）；"
        "`verification_log`→验证输出（symbol 可选 provider 过滤）；"
        "`trace_snippet`→运行 trace 片段（symbol=`recent` 或 event 前缀）。"
    )
