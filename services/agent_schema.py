"""Strict, dependency-free schemas for model decisions and context requests."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


CONTEXT_REQUEST_TYPES = frozenset({
    "function",
    "field",
    "references",
    "callers",
    "grep",
    "read_file",
    "memory_pattern",
    "verification_log",
    "trace_snippet",
})
CONTEXT_OBSERVATION_REQUEST_TYPES = frozenset({"memory_pattern", "verification_log", "trace_snippet"})
CONTEXT_REPO_SEARCH_REQUEST_TYPES = frozenset({"grep", "read_file"})
CONTEXT_PRIORITIES = frozenset({"low", "normal", "high", "critical"})


@dataclass(frozen=True)
class ContextRequest:
    type: str
    symbol: str = ""
    file: str = ""
    line_number: int = 0
    line_end: int = 0
    reason: str = ""
    priority: str = "normal"
    expected_return_form: str = ""
    fulfillment_note: str = ""

    @classmethod
    def from_mapping(cls, value: Any) -> Tuple[Optional["ContextRequest"], Optional[str]]:
        if not isinstance(value, dict):
            return None, "request must be an object"
        req_type = str(value.get("type") or "function").strip().lower()
        if req_type not in CONTEXT_REQUEST_TYPES:
            return None, f"unsupported request type: {req_type}"
        symbol = str(value.get("symbol") or value.get("function") or value.get("function_name") or value.get("name") or "").strip()
        file_path = str(value.get("file") or value.get("file_path") or "").strip()
        try:
            line = int(value.get("line") or value.get("line_number") or 0)
        except (TypeError, ValueError):
            return None, "line_number must be an integer"
        try:
            line_end = int(value.get("line_end") or 0)
        except (TypeError, ValueError):
            return None, "line_end must be an integer"
        if req_type == "grep":
            if not symbol:
                return None, "grep requires symbol (search pattern)"
        elif req_type == "read_file":
            if not file_path:
                return None, "read_file requires file path"
        elif not symbol and not (file_path and line > 0):
            if req_type == "memory_pattern":
                return None, "symbol query is required for memory_pattern"
            if req_type not in CONTEXT_OBSERVATION_REQUEST_TYPES:
                return None, "symbol or file+line is required"
        if "\x00" in file_path or any(part == ".." for part in Path(file_path).parts):
            return None, "file path is unsafe"
        priority = str(value.get("priority") or "normal").strip().lower()
        if priority not in CONTEXT_PRIORITIES:
            return None, "priority must be low/normal/high/critical"
        reason = str(value.get("reason") or "").strip()
        if len(reason) > 500:
            return None, "reason exceeds 500 characters"
        return cls(req_type, symbol, file_path, max(0, line), max(0, line_end), reason, priority,
                   str(value.get("expected_return_form") or value.get("return_form") or value.get("expected_form") or "").strip(),
                   str(value.get("fulfillment_note") or value.get("request_type_note") or value.get("return_form_note") or "").strip()), None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "type": self.type,
            "symbol": self.symbol,
            "file": self.file,
            "line_number": self.line_number,
            "reason": self.reason,
            "priority": self.priority,
            "expected_return_form": self.expected_return_form,
            "fulfillment_note": self.fulfillment_note,
        }
        if self.line_end > 0:
            payload["line_end"] = self.line_end
        return payload


@dataclass(frozen=True)
class AgentDecision:
    agent_can_fetch_more: bool
    context_requests: tuple[ContextRequest, ...] = ()
    hypotheses: tuple[Dict[str, Any], ...] = ()
    next_action: Optional[Dict[str, Any]] = None

    @classmethod
    def from_mapping(cls, value: Any) -> Tuple[Optional["AgentDecision"], list[Dict[str, Any]]]:
        if not isinstance(value, dict) or not isinstance(value.get("agent_can_fetch_more"), bool):
            return cls(False), [{"error": "agent_can_fetch_more must be boolean"}]
        raw = value.get("context_requests")
        if not isinstance(raw, list):
            invalid = [{"error": "context_requests must be a list"}]
            raw = []
        else:
            invalid = []
        requests = []
        for item in raw:
            request, error = ContextRequest.from_mapping(item)
            if request is None:
                invalid.append({"request": item, "error": error})
            else:
                requests.append(request)
        if value["agent_can_fetch_more"] and not requests:
            invalid.append({"error": "agent_can_fetch_more=true requires context_requests"})
        hypotheses = value.get("hypotheses")
        if hypotheses is None:
            hypotheses = []
        if not isinstance(hypotheses, list):
            invalid.append({"error": "hypotheses must be a list"})
            hypotheses = []
        action = value.get("next_action")
        if action is not None and not isinstance(action, dict):
            invalid.append({"error": "next_action must be an object"})
            action = None
        return cls(bool(value["agent_can_fetch_more"] and requests), tuple(requests),
                   tuple(item for item in hypotheses if isinstance(item, dict)),
                   dict(action) if isinstance(action, dict) else None), invalid


@dataclass(frozen=True)
class RepairPlan:
    """Strict executable repair plan produced by model/extractor output."""

    summary: str
    edits: tuple[Dict[str, Any], ...]

    @classmethod
    def from_mapping(cls, value: Any) -> Tuple[Optional["RepairPlan"], List[Dict[str, Any]]]:
        if not isinstance(value, dict):
            return None, [{"error": "repair plan must be an object"}]
        raw_edits = value.get("edits")
        if not isinstance(raw_edits, list):
            return None, [{"error": "repair plan edits must be a list"}]
        invalid: List[Dict[str, Any]] = []
        edits: List[Dict[str, Any]] = []
        for index, raw in enumerate(raw_edits):
            if not isinstance(raw, dict):
                invalid.append({"index": index, "error": "edit must be an object"})
                continue
            file_path = str(raw.get("file") or "").strip()
            if not file_path or "\x00" in file_path or any(part == ".." for part in Path(file_path).parts):
                invalid.append({"index": index, "error": "edit file path is missing or unsafe"})
                continue
            edit_type = str(raw.get("edit_type") or "function_replacement").strip()
            if edit_type not in {"function_replacement", "include_directive", "member_declaration"}:
                invalid.append({"index": index, "error": f"unsupported edit_type: {edit_type}"})
                continue
            if edit_type == "include_directive":
                valid_body = bool(str(raw.get("include") or "").strip())
            elif edit_type == "member_declaration":
                valid_body = bool(str(raw.get("old_text") or "").strip() and str(raw.get("new_text") or "").strip())
            else:
                valid_body = bool(
                    str(raw.get("function_signature") or "").strip()
                    and str(raw.get("replacement_code") or "").strip()
                )
            if not valid_body:
                invalid.append({"index": index, "error": "edit body is incomplete"})
                continue
            edits.append(dict(raw))
        if not edits:
            invalid.append({"error": "repair plan requires at least one valid edit"})
        if invalid:
            return None, invalid
        summary = str(value.get("summary") or "").strip()
        if len(summary) > 2000:
            return None, [{"error": "repair plan summary exceeds 2000 characters"}]
        return cls(summary=summary, edits=tuple(edits)), []

    def to_dict(self) -> Dict[str, Any]:
        return {"summary": self.summary, "edits": [dict(item) for item in self.edits]}


@dataclass(frozen=True)
class VerificationDecision:
    """Normalized provider decision consumed by Runtime and reports."""

    status: str
    provider: str
    mode: str
    command_fingerprint: str = ""

    @classmethod
    def from_mapping(cls, value: Any) -> Tuple[Optional["VerificationDecision"], Optional[str]]:
        if not isinstance(value, dict):
            return None, "verification decision must be an object"
        status = str(value.get("status") or "").strip()
        if status not in {"passed", "failed", "pending", "unavailable", "timeout", "skipped",
                          "not_configured", "configured_but_unavailable", "not_triggered",
                          "harness_invalid", "compile_verified", "native_verified",
                          "integration_verified", "strongly_supported", "inconclusive", "contradicted"}:
            return None, f"invalid verification status: {status}"
        provider = str(value.get("provider") or "").strip()
        mode = str(value.get("mode") or "").strip()
        if not provider or not mode:
            return None, "verification provider and mode are required"
        return cls(status, provider, mode, str(value.get("command_fingerprint") or "")), None

    def to_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "provider": self.provider, "mode": self.mode,
                "command_fingerprint": self.command_fingerprint}


@dataclass(frozen=True)
class AnalysisReport:
    """Optional structured final analysis payload (metadata only)."""

    summary_zh: str
    root_cause: str
    confidence: float
    suggested_fixes: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: Any) -> Tuple[Optional["AnalysisReport"], Optional[str]]:
        if not isinstance(value, dict):
            return None, "analysis report must be an object"
        summary = str(value.get("summary_zh") or value.get("summary") or "").strip()
        root_cause = str(value.get("root_cause") or value.get("root_cause_zh") or "").strip()
        if not summary and not root_cause:
            return None, "summary_zh or root_cause is required"
        try:
            confidence = float(value.get("confidence") or 0.0)
        except (TypeError, ValueError):
            return None, "confidence must be numeric"
        confidence = max(0.0, min(1.0, confidence))
        fixes_raw = value.get("suggested_fixes")
        refs_raw = value.get("evidence_refs")
        fixes = tuple(str(x).strip() for x in fixes_raw if str(x).strip()) if isinstance(fixes_raw, list) else ()
        refs = tuple(str(x).strip() for x in refs_raw if str(x).strip()) if isinstance(refs_raw, list) else ()
        return cls(summary, root_cause, confidence, fixes, refs), None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "summary_zh": self.summary_zh,
            "root_cause": self.root_cause,
            "confidence": self.confidence,
            "suggested_fixes": list(self.suggested_fixes),
            "evidence_refs": list(self.evidence_refs),
        }
