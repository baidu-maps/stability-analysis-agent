"""Shared parsers for structured model outputs used by workflows and tests."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from services.agent_schema import CONTEXT_REQUEST_TYPES, AgentDecision, AnalysisReport, ContextRequest


def extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Best-effort JSON object extraction from model text."""
    raw = str(text or "").strip()
    if not raw:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.DOTALL | re.I)
    if fence:
        raw = fence.group(1).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(raw[start : end + 1])
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError):
        return None


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
    payload = extract_json_object(analysis_text)
    if not payload:
        return None, "no_json_object"
    nested = payload.get("analysis_report")
    if isinstance(nested, dict):
        payload = nested
    return AnalysisReport.from_mapping(payload)
