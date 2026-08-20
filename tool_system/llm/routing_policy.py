#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Built-in LLM tier selection policy (no user routing JSON)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Sequence

from .endpoint_pool import LLMEndpoint

ProfileName = Literal["default", "strong", "fast"]


@dataclass
class RoutingContext:
    """Inputs for resolve_tier."""

    mode: str = "fixed"  # fixed|auto
    force_profile: Optional[str] = None  # CLI --llm-profile
    prompt_mode: str = "fix"  # analysis|fix
    apply_ai_fixes: bool = False
    agent_loop: str = "single"  # single|context_loop
    round_index: int = 0
    crash_diagnosis: Optional[Dict[str, Any]] = None


def resolve_tier(ctx: RoutingContext) -> tuple:
    """Return (profile, reason)."""
    force = str(ctx.force_profile or "").strip().lower()
    if force in ("default", "strong", "fast"):
        return force, f"cli_force_profile={force}"

    mode = str(ctx.mode or "fixed").strip().lower()
    if str(ctx.agent_loop or "").strip().lower() == "context_loop" and int(ctx.round_index or 0) > 0:
        return "fast", f"context_loop_round={ctx.round_index}"
    if str(ctx.prompt_mode or "").strip().lower() == "fix":
        return "strong", "prompt_mode=fix"
    if ctx.apply_ai_fixes:
        return "strong", "apply_ai_fixes=true"

    diag = ctx.crash_diagnosis if isinstance(ctx.crash_diagnosis, dict) else {}
    compass = diag.get("evidence_compass") if isinstance(diag.get("evidence_compass"), dict) else {}
    ceiling = compass.get("confidence_ceiling")
    try:
        ceiling_f = float(ceiling) if ceiling is not None else None
    except (TypeError, ValueError):
        ceiling_f = None

    data_avail = diag.get("data_availability") if isinstance(diag.get("data_availability"), dict) else {}
    missing_symbol = data_avail.get("has_symbolized_function") is False
    missing_source = data_avail.get("has_source_file_line") is False
    if ceiling_f is not None and ceiling_f < 0.6:
        return "strong", f"confidence_ceiling={ceiling_f:.2f}<0.6"
    if missing_symbol or missing_source:
        bits = []
        if missing_symbol:
            bits.append("no_symbol")
        if missing_source:
            bits.append("no_source_line")
        return "strong", "missing_evidence:" + ",".join(bits)

    facts = diag.get("deterministic_facts")
    if isinstance(facts, list) and facts:
        high = False
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            try:
                conf = float(fact.get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0.0
            if conf >= 0.95:
                high = True
                break
        if high and ceiling_f is not None and ceiling_f >= 0.85:
            return "fast", "deterministic_high_confidence"
        if high and ceiling_f is None:
            return "default", "deterministic_high_confidence_no_ceiling"

    return "default", "default_analysis"


def _healthy(ep: LLMEndpoint) -> bool:
    return ep.health_status in ("healthy", "unknown", "rate_limited")


def select_endpoint(
    pool: Sequence[LLMEndpoint],
    profile: ProfileName,
    *,
    preferences: Optional[Dict[str, Any]] = None,
    exclude_ids: Optional[Sequence[str]] = None,
) -> Optional[LLMEndpoint]:
    """Pick best endpoint for a profile from the pool."""
    prefs = preferences if isinstance(preferences, dict) else {}
    excluded = set(exclude_ids or [])
    usable = [ep for ep in pool if ep.endpoint_id not in excluded and _healthy(ep)]
    if not usable:
        return None

    preferred_key = None
    if profile == "strong":
        preferred_key = prefs.get("strong_provider") or prefs.get("default_provider")
    elif profile == "fast":
        preferred_key = prefs.get("default_provider")
    else:
        preferred_key = prefs.get("default_provider")
    preferred_key = str(preferred_key).strip() if preferred_key else ""

    def _prefer(cands: List[LLMEndpoint]) -> Optional[LLMEndpoint]:
        if not cands:
            return None
        if preferred_key:
            for ep in cands:
                if ep.provider_key == preferred_key:
                    return ep
        return cands[0]

    by_score = sorted(usable, key=lambda e: (-int(e.score), e.provider_key))

    if profile == "strong":
        strongish = [ep for ep in by_score if ep.tier == "strong" or ep.score >= 85]
        return _prefer(strongish or by_score)

    if profile == "fast":
        economy = [ep for ep in by_score if ep.tier == "economy"]
        defaults = [ep for ep in by_score if ep.tier == "default"]
        # Prefer cheaper/faster: economy first, else lowest score among default
        if economy:
            economy_sorted = sorted(economy, key=lambda e: (int(e.score), e.provider_key))
            return _prefer(economy_sorted)
        if defaults:
            defaults_sorted = sorted(defaults, key=lambda e: (int(e.score), e.provider_key))
            return _prefer(defaults_sorted)
        return _prefer(list(reversed(by_score)))  # weakest strong as last resort

    # default profile: prefer default-tier, else mid score
    defaults = [ep for ep in by_score if ep.tier == "default"]
    if defaults:
        return _prefer(defaults)
    economy = [ep for ep in by_score if ep.tier == "economy"]
    if economy:
        return _prefer(economy)
    return _prefer(by_score)


def assign_role_endpoints(
    pool: Sequence[LLMEndpoint],
    *,
    preferences: Optional[Dict[str, Any]] = None,
) -> Dict[str, Optional[LLMEndpoint]]:
    """Assign default/strong/fast role endpoints from a healthy pool."""
    return {
        "default": select_endpoint(pool, "default", preferences=preferences),
        "strong": select_endpoint(pool, "strong", preferences=preferences),
        "fast": select_endpoint(pool, "fast", preferences=preferences),
    }
