"""Shared parsers for structured model outputs used by workflows and tests."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Set, Tuple

from services.agent_schema import CONTEXT_REQUEST_TYPES, AgentDecision, AnalysisReport, ContextRequest


def _skip_json_string(text: str, index: int) -> Optional[int]:
    """Return the index after a JSON string starting at ``index``."""
    if index >= len(text) or text[index] != '"':
        return None
    i = index + 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            return i + 1
        i += 1
    return None


def _match_object_end(text: str, start: int) -> Optional[int]:
    """Return the closing brace index for the object that starts at ``start``."""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch == '"':
            nxt = _skip_json_string(text, i)
            if nxt is None:
                return None
            i = nxt
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _json_objects(text: str) -> List[Dict[str, Any]]:
    """Return maximal JSON objects, ignoring braces that do not form JSON."""
    raw = str(text or "")
    found: List[Dict[str, Any]] = []
    i = 0
    while i < len(raw):
        if raw[i] != "{":
            i += 1
            continue
        end = _match_object_end(raw, i)
        if end is None:
            i += 1
            continue
        try:
            value = json.loads(raw[i : end + 1])
        except (TypeError, ValueError):
            value = None
        if isinstance(value, dict):
            found.append(value)
            i = end + 1
            continue
        i += 1
    return found


def extract_json_object(text: str, *, prefer_key: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Pick one JSON object from model text.

    A trailing control contract is preferred when it contains a boolean
    ``agent_can_fetch_more``. ``prefer_key`` selects another object first,
    so a repair plan is not replaced by that contract. Braces in prose or
    ``cpp`` fences are ignored unless they themselves parse as JSON.
    """
    objects = _json_objects(text)
    if not objects:
        return None
    if prefer_key:
        for obj in reversed(objects):
            if prefer_key in obj:
                return obj
    for obj in reversed(objects):
        if isinstance(obj.get("agent_can_fetch_more"), bool):
            return obj
    return objects[-1]


def parse_agent_decision(
    analysis_text: str,
    *,
    allowed_types: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """Parse agent decision JSON from analysis text."""
    payload = extract_json_object(analysis_text) or {}
    decision, schema_invalid = AgentDecision.from_mapping(payload)
    allowed = allowed_types or set(CONTEXT_REQUEST_TYPES)
    invalid_requests: List[Dict[str, Any]] = [
        item for item in schema_invalid if not isinstance(item, dict) or "request" not in item
    ]
    normalized: List[Dict[str, Any]] = []
    seen_keys: Set[Tuple[str, str, str, int]] = set()
    raw_requests = payload.get("context_requests")
    if not isinstance(raw_requests, list):
        raw_requests = []

    for item in raw_requests:
        if not isinstance(item, dict):
            continue
        req, error = ContextRequest.from_mapping(item)
        if req is None:
            invalid_requests.append({"request": dict(item), "error": error})
            continue
        if req.type not in allowed:
            invalid_requests.append({"request": dict(item), "error": f"unsupported request type: {req.type}"})
            continue
        dedupe_key = (req.type, req.symbol, req.file, req.line_number)
        if dedupe_key in seen_keys:
            invalid_requests.append({"request": dict(item), "error": "同一轮重复请求，已去重"})
            continue
        seen_keys.add(dedupe_key)
        normalized.append(req.to_dict())

    # An invalid item must not poison otherwise valid requests.  Only contract-level
    # failures (missing control boolean, malformed collection, etc.) degrade the turn.
    contract_errors = [
        item for item in schema_invalid
        if isinstance(item, dict) and "request" not in item
    ]
    degraded = bool(contract_errors) or (extract_json_object(analysis_text) is None)
    agent_can_fetch_more = bool(decision.agent_can_fetch_more and normalized and not degraded)
    return {
        "agent_can_fetch_more": agent_can_fetch_more,
        "context_requests": normalized,
        "invalid_context_requests": invalid_requests,
        "raw_payload": payload,
        "degraded": degraded,
        "decision": decision,
        "hypotheses": list(decision.hypotheses),
        "next_action": dict(decision.next_action or {}),
    }


def parse_analysis_report(analysis_text: str) -> Tuple[Optional[AnalysisReport], Optional[str]]:
    """Parse optional structured analysis report from final LLM output."""
    report_keys = ("summary_zh", "summary", "root_cause", "root_cause_zh")
    for obj in reversed(_json_objects(analysis_text)):
        nested = obj.get("analysis_report")
        candidate = nested if isinstance(nested, dict) else obj
        if any(candidate.get(key) for key in report_keys):
            return AnalysisReport.from_mapping(candidate)
    return None, "no_json_object"
