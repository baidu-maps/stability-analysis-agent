#!/usr/bin/env python3
"""Stable, JSON-friendly contracts shared by diagnosis modules."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Mapping, Optional


@dataclass
class EvidenceItem:
    type: str
    value: Any
    source: Optional[str] = None
    confidence: Optional[float] = None
    tier: int = 4  # 1=Detector, 2=Direct, 3=CrossVerified, 4=Pattern


@dataclass
class KnowledgeEntry:
    id: str
    domain: str
    module: str
    root_cause: str
    evidence_patterns: List[str] = field(default_factory=list)
    guidance: List[str] = field(default_factory=list)
    source: str = "local"
    api_version: Optional[str] = None
    last_verified: Optional[str] = None


@dataclass
class DiagnosisResult:
    domain: str
    status: str = "success"
    diagnosis_status: str = "preliminary"
    confidence: float = 0.0
    facts: Dict[str, Any] = field(default_factory=dict)
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    fault_modes: List[Dict[str, Any]] = field(default_factory=list)
    root_cause: Dict[str, Any] = field(default_factory=dict)
    missing_evidence: List[Any] = field(default_factory=list)
    repair_guidance: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def normalize_diagnosis_result(result: Mapping[str, Any], domain: str = "unknown") -> Dict[str, Any]:
    """Adapt legacy specialist output without discarding its original fields."""
    payload = dict(result)
    status = str(payload.get("diagnosis_status") or payload.get("status") or "preliminary")
    confidence = payload.get("confidence")
    if confidence is None:
        confidence = ((payload.get("diagnosis") or {}).get("confidence") if isinstance(payload.get("diagnosis"), Mapping) else None) or ((payload.get("root_cause") or {}).get("confidence") if isinstance(payload.get("root_cause"), Mapping) else None) or 0.0
    try:
        confidence = round(float(confidence), 3)
    except (TypeError, ValueError):
        confidence = 0.0
    evidence = payload.get("evidence") or payload.get("evidence_chain") or []
    modes = payload.get("fault_modes") or payload.get("fault_mode_matches") or payload.get("knowledge_matches") or []
    normalized = {
        "domain": domain,
        "status": str(payload.get("status") or "success"),
        "diagnosis_status": status,
        "confidence": confidence,
        "facts": payload.get("facts") or payload.get("error") or payload.get("freeze") or payload.get("evidence") or {},
        "evidence": evidence if isinstance(evidence, list) else [evidence],
        "fault_modes": modes if isinstance(modes, list) else [modes],
        "root_cause": payload.get("root_cause") or payload.get("diagnosis") or {},
        "missing_evidence": payload.get("missing_evidence") or [],
        "repair_guidance": payload.get("repair_guidance") or {},
        "metadata": {"legacy_fields_preserved": True},
    }
    normalized["legacy_result"] = payload
    return normalized
