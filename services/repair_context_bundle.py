"""Context package passed to the repair planner.

The bundle is a bounded, provenance-preserving view of evidence already
collected by the crash workflow.  It does not perform new searches or grant
write permission; it prevents the repair model from seeing only the crashing
line while omitting the surrounding ownership and call-chain evidence.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Mapping, Optional


@dataclass
class RepairContextBundle:
    target: Dict[str, Any] = field(default_factory=dict)
    related_symbols: List[Dict[str, Any]] = field(default_factory=list)
    call_chain: List[Dict[str, Any]] = field(default_factory=list)
    field_lifecycle: List[Dict[str, Any]] = field(default_factory=list)
    tests: List[Dict[str, Any]] = field(default_factory=list)
    history: List[Dict[str, Any]] = field(default_factory=list)
    current_diff: Dict[str, Any] = field(default_factory=dict)
    authorized_scope: Dict[str, Any] = field(default_factory=dict)
    provenance: Dict[str, Any] = field(default_factory=dict)
    evidence_graph: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"schema_version": 1, **asdict(self)}


def _bounded_rows(value: Any, limit: int = 24) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value[:limit] if isinstance(item, Mapping)]


def build_repair_context_bundle(
    result: Mapping[str, Any],
    *,
    context_session: Optional[Mapping[str, Any]] = None,
    authorized_scope: Optional[Mapping[str, Any]] = None,
    max_chars: int = 24000,
) -> Dict[str, Any]:
    value = result if isinstance(result, Mapping) else {}
    code_context = value.get("code_context") if isinstance(value.get("code_context"), Mapping) else {}
    diagnosis = value.get("crash_diagnosis") if isinstance(value.get("crash_diagnosis"), Mapping) else {}
    session = context_session if isinstance(context_session, Mapping) else {}

    graph = code_context.get("graph") if isinstance(code_context.get("graph"), Mapping) else {}
    candidates = (code_context.get("candidate_nodes") or code_context.get("nodes")
                  or code_context.get("items") or graph.get("nodes"))
    candidates = _bounded_rows(candidates)
    stack = diagnosis.get("stack") or diagnosis.get("frames") or value.get("resolved_stack")
    stack_rows = _bounded_rows(stack)
    fields = code_context.get("field_references") or code_context.get("fields") or []
    callers = code_context.get("callers") or code_context.get("call_chain") or []
    if not callers and isinstance(graph.get("edges"), list):
        callers = [edge for edge in graph.get("edges") if isinstance(edge, Mapping)
                   and str(edge.get("type") or "").startswith("calls")]
    tests = code_context.get("tests") or []
    history = code_context.get("history") or []
    crash_summary = code_context.get("crash_summary") if isinstance(code_context.get("crash_summary"), Mapping) else {}
    target = {
        "file": diagnosis.get("file") or diagnosis.get("crash_file") or "",
        "line": diagnosis.get("line") or diagnosis.get("crash_line") or 0,
        "function": diagnosis.get("function") or diagnosis.get("symbol") or crash_summary.get("function") or "",
        "root_cause": diagnosis.get("root_cause") or diagnosis.get("category") or "",
    }
    if not target["function"] and candidates:
        target["function"] = (candidates[0].get("function_signature")
                               or candidates[0].get("signature")
                               or candidates[0].get("symbol") or "")
    bundle = RepairContextBundle(
        target=target,
        related_symbols=candidates,
        call_chain=_bounded_rows(callers),
        field_lifecycle=_bounded_rows(fields),
        tests=_bounded_rows(tests),
        history=_bounded_rows(history),
        current_diff=dict(value.get("diff_review") or {}) if isinstance(value.get("diff_review"), Mapping) else {},
        authorized_scope=dict(authorized_scope or {}),
        provenance={
            "context_session_hash": session.get("context_session_hash") or session.get("hash"),
            "workspace_revision": value.get("workspace_revision") or session.get("workspace_revision"),
            "source": "crash_analysis_evidence",
        },
        evidence_graph=dict(session.get("evidence_graph") or {}) if isinstance(session.get("evidence_graph"), Mapping) else {},
    )
    payload = bundle.to_dict()
    # Keep the bundle bounded when diagnostic artifacts contain large snippets.
    encoded = str(payload)
    if len(encoded) > max(4000, int(max_chars)):
        payload["related_symbols"] = payload["related_symbols"][:8]
        payload["call_chain"] = payload["call_chain"][:8]
        payload["field_lifecycle"] = payload["field_lifecycle"][:8]
        payload["tests"] = payload["tests"][:8]
        payload["history"] = payload["history"][:8]
    return payload
