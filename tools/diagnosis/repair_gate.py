#!/usr/bin/env python3
"""Automatic-fix safety gate shared by specialist diagnostics."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


@dataclass
class RepairDecision:
    allowed: bool
    reason: str
    status: str
    confidence: float

    def to_dict(self) -> dict:
        return asdict(self)


def evaluate_repair_gate(result: Mapping[str, Any], *, minimum_confidence: float = 0.85, allow_probable: bool = False) -> RepairDecision:
    status = str(result.get("diagnosis_status") or "preliminary")
    try:
        confidence = float(result.get("confidence") or ((result.get("diagnosis") or {}).get("confidence") if isinstance(result.get("diagnosis"), Mapping) else 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    if status == "confirmed" and confidence >= minimum_confidence:
        return RepairDecision(True, "confirmed diagnosis meets confidence threshold", status, confidence)
    if status == "probable" and allow_probable and confidence >= minimum_confidence:
        return RepairDecision(True, "probable diagnosis explicitly allowed", status, confidence)
    return RepairDecision(False, "preliminary/low-confidence diagnosis requires more evidence before auto-fix", status, confidence)
