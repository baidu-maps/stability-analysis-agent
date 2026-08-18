#!/usr/bin/env python3
"""Evidence tier classification and conflict resolution.

Tier hierarchy (lower number = higher priority):
  Tier 1 — Detector/tool reports (GWP-ASan, ASan, Memory Tracker)
  Tier 2 — Direct evidence (registers, fault address, instruction decode)
  Tier 3 — Cross-verified evidence (multiple dimensions corroborating)
  Tier 4 — Single pattern/module match
"""
from __future__ import annotations

from enum import IntEnum
from typing import Any, Dict, List


class EvidenceTier(IntEnum):
    """Evidence reliability tier — lower is stronger."""
    DETECTOR = 1
    DIRECT = 2
    CROSS_VERIFIED = 3
    PATTERN = 4


# Mapping from evidence type strings to their default tier
EVIDENCE_TYPE_TIER_MAP: Dict[str, int] = {
    # Tier 1 — Detector reports
    "gwp_asan_report": 1,
    "sanitizer_report": 1,
    "memory_tracker": 1,
    "detector": 1,
    # Tier 2 — Direct evidence
    "register_analysis": 2,
    "fault_address": 2,
    "instruction_decode": 2,
    "signal_taxonomy": 2,
    "address_analysis": 2,
    # Tier 3 — Cross-verified
    "stack_cluster": 3,
    "block_vs_busy": 3,
    "dependency_cycle": 3,
    "binder_chain": 3,
    "binder": 3,
    "multi_thread_correlation": 3,
    "reference_chain": 3,
    # Tier 4 — Single pattern
    "freeze_type": 4,
    "sample_hotspot": 4,
    "system_load": 4,
    "event_handler": 4,
    "pattern_match": 4,
    "hint_match": 4,
    "root_hint": 4,
}


def assign_tier(evidence_type: str) -> int:
    """Return the default tier for a given evidence type string.

    Falls back to Tier 4 (PATTERN) for unknown types.
    """
    return EVIDENCE_TYPE_TIER_MAP.get(evidence_type, EvidenceTier.PATTERN)


def annotate_evidence_chain(evidence_chain: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add tier field to each item in an evidence chain (in-place and returns)."""
    for item in evidence_chain:
        if "tier" not in item:
            item["tier"] = assign_tier(str(item.get("type") or ""))
    return evidence_chain


def best_tier_for_mode(fault_mode: Dict[str, Any], evidence_chain: List[Dict[str, Any]]) -> int:
    """Find the strongest (lowest tier) evidence supporting a fault mode."""
    mode_id = str(fault_mode.get("id") or "").lower()
    best = int(EvidenceTier.PATTERN)
    for item in evidence_chain:
        tier = int(item.get("tier", EvidenceTier.PATTERN))
        evidence_values = str(item.get("value") or "") + " ".join(str(e) for e in (item.get("evidence") or []))
        if mode_id and mode_id in evidence_values.lower():
            best = min(best, tier)
        elif tier < best:
            best = min(best, tier)
    return best


def resolve_conflicts(
    fault_modes: List[Dict[str, Any]],
    evidence_chain: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Sort fault modes by their best evidence tier (ascending), then confidence (descending).

    Annotates each fault mode with 'best_evidence_tier' field.
    This provides a principled way to resolve conflicts when multiple
    fault modes are detected — higher-tier evidence wins.
    """
    annotate_evidence_chain(evidence_chain)

    for mode in fault_modes:
        best = int(EvidenceTier.PATTERN)
        # Primary: use the mode's own evidence level field
        level = str(mode.get("level") or "")
        if level == "detector":
            best = min(best, int(EvidenceTier.DETECTOR))
        elif level in ("address", "si_code", "register"):
            best = min(best, int(EvidenceTier.DIRECT))
        elif level in ("stack", "hint"):
            best = min(best, int(EvidenceTier.CROSS_VERIFIED))
        # Secondary: check if any evidence in the chain specifically references this mode
        mode_id = str(mode.get("id") or "").lower()
        mode_evidence_texts = [str(e).lower() for e in (mode.get("evidence") or [])]
        for item in evidence_chain:
            tier = int(item.get("tier", EvidenceTier.PATTERN))
            item_value = str(item.get("value") or "").lower()
            item_type = str(item.get("type") or "").lower()
            # Only associate evidence with this mode if there's a clear link
            if mode_id and (mode_id in item_value or any(me in item_value for me in mode_evidence_texts)):
                best = min(best, tier)
            elif item_type in mode_evidence_texts:
                best = min(best, tier)
        mode["best_evidence_tier"] = best

    # Sort by tier ascending (stronger first), then confidence descending
    return sorted(
        fault_modes,
        key=lambda m: (m.get("best_evidence_tier", 4), -float(m.get("confidence") or 0)),
    )
