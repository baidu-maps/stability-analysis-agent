#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM router facade: resolve endpoints for fixed/auto modes, failover, summary payload."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from tool_system.config import LLMConfig

from .endpoint_pool import (
    LLMEndpoint,
    discover_candidates,
    endpoint_to_adapter_dict,
    mark_endpoint_healthy,
    mark_endpoint_unhealthy,
    probe_candidates,
)
from .routing_policy import RoutingContext, assign_role_endpoints, resolve_tier, select_endpoint

logger = logging.getLogger(__name__)


def normalize_mode(raw: Any) -> str:
    mode = str(raw or "fixed").strip().lower()
    # strong_only was an early public mode. Keep old config files usable by
    # treating them as auto routing; strength is an internal tier now.
    if mode == "strong_only":
        return "auto"
    if mode in ("auto", "fixed"):
        return mode
    return "fixed"


def resolve_mode_from_config(llm_config: Dict[str, Any], *, cli_mode: Optional[str] = None) -> str:
    if cli_mode:
        return normalize_mode(cli_mode)
    if not isinstance(llm_config, dict):
        return "fixed"
    explicit = llm_config.get("mode")
    if explicit:
        return normalize_mode(explicit)
    active = str(llm_config.get("active_provider") or "").strip().lower()
    if active == "auto":
        return "auto"
    return "fixed"


@dataclass
class LLMRouterState:
    """Mutable routing state for one analysis run."""

    mode: str = "fixed"
    engine: str = "direct"
    force_profile: Optional[str] = None
    failover_enabled: bool = False
    health_check: bool = True
    preferences: Dict[str, Any] = field(default_factory=dict)
    pool: List[LLMEndpoint] = field(default_factory=list)
    roles: Dict[str, Optional[LLMEndpoint]] = field(default_factory=dict)
    selected: Optional[LLMEndpoint] = None
    requested_tier: str = "default"
    reason: str = ""
    failover_occurred: bool = False
    failover_from: Optional[Dict[str, Any]] = None
    failover_cause: Optional[str] = None
    tried_ids: List[str] = field(default_factory=list)
    calls: List[Dict[str, Any]] = field(default_factory=list)
    engaged: bool = False
    skip_reason: Optional[str] = None

    def to_summary_dict(self) -> Dict[str, Any]:
        totals_ms = sum(int(c.get("duration_ms") or 0) for c in self.calls)
        pool_public = [ep.to_public_dict() for ep in self.pool]
        healthy = sum(1 for ep in self.pool if ep.health_status == "healthy")
        selected = None
        if self.selected is not None:
            selected = {
                "provider": self.selected.provider_key,
                "model": self.selected.model,
                "profile": self.requested_tier,
                "adapter_provider": self.selected.adapter_provider,
            }
        failover: Dict[str, Any] = {"occurred": bool(self.failover_occurred)}
        if self.failover_occurred:
            failover["from"] = self.failover_from
            failover["cause"] = self.failover_cause
        return {
            "engaged": bool(self.engaged),
            "skip_reason": self.skip_reason,
            "mode": self.mode,
            "requested_tier": self.requested_tier,
            "selected": selected,
            "reason": self.reason,
            "failover": failover,
            "pool": {
                "configured": len(self.pool),
                "healthy": healthy,
                "candidates": pool_public,
            },
            "calls": list(self.calls),
            "totals": {
                "call_count": len(self.calls),
                "duration_ms": totals_ms,
            },
        }


def build_llm_config_for_endpoint(
    ep: LLMEndpoint,
    *,
    engine: str = "direct",
) -> LLMConfig:
    d = endpoint_to_adapter_dict(ep, engine=engine, probe=False)
    extra = {
        k: v
        for k, v in d.items()
        if k
        not in {
            "engine",
            "provider",
            "model",
            "api_key",
            "base_url",
            "timeout",
            "temperature",
            "max_tokens",
        }
    }
    return LLMConfig(
        engine=str(d.get("engine") or "direct"),
        provider=str(d.get("provider") or "openai"),
        model=str(d.get("model") or ""),
        api_key=d.get("api_key"),
        base_url=d.get("base_url"),
        timeout=int(d.get("timeout") or 120),
        temperature=float(d.get("temperature") or 0.7),
        max_tokens=int(d.get("max_tokens") or 4096),
        extra=extra,
    )


def _fixed_endpoint(llm_config: Dict[str, Any], active_provider: str) -> Optional[LLMEndpoint]:
    """Resolve the single active_provider endpoint (may include placeholders filtered out)."""
    candidates = discover_candidates(llm_config)
    for ep in candidates:
        if ep.provider_key == active_provider:
            ep.health_status = "unknown"
            return ep
    # active may have placeholder when discover skipped it — try raw merge for error messaging
    return None


def resolve_for_run(
    llm_config: Dict[str, Any],
    *,
    engine: str = "direct",
    cli_mode: Optional[str] = None,
    force_profile: Optional[str] = None,
    routing_ctx: Optional[RoutingContext] = None,
    engage: bool = True,
    skip_reason: Optional[str] = None,
    health_check_override: Optional[bool] = None,
) -> LLMRouterState:
    """Resolve initial endpoint + router state for a run."""
    state = LLMRouterState(
        engine=engine,
        force_profile=(str(force_profile).strip().lower() if force_profile else None),
    )
    if not engage:
        state.engaged = False
        state.skip_reason = skip_reason or "llm_not_engaged"
        state.mode = resolve_mode_from_config(llm_config, cli_mode=cli_mode)
        return state

    if not isinstance(llm_config, dict):
        llm_config = {}

    mode = resolve_mode_from_config(llm_config, cli_mode=cli_mode)
    state.mode = mode
    routing = llm_config.get("routing") if isinstance(llm_config.get("routing"), dict) else {}
    state.failover_enabled = bool(routing.get("failover_enabled")) or mode == "auto"
    state.health_check = routing.get("health_check", True) is not False
    if health_check_override is not None:
        state.health_check = bool(health_check_override)
    prefs = llm_config.get("preferences") if isinstance(llm_config.get("preferences"), dict) else {}
    state.preferences = dict(prefs)

    ctx = routing_ctx or RoutingContext(mode=mode, force_profile=state.force_profile)
    ctx.mode = mode
    if state.force_profile:
        ctx.force_profile = state.force_profile

    if mode == "fixed":
        active = str(llm_config.get("active_provider") or "openai").strip()
        if active.lower() == "auto":
            active = "openai"
        # Discover all then pick active; if active missing from discover (placeholder),
        # fall back to building from raw config via discover of only that key.
        all_cands = discover_candidates(llm_config)
        state.pool = [ep for ep in all_cands if ep.provider_key == active] or all_cands[:0]
        selected = None
        for ep in all_cands:
            if ep.provider_key == active:
                selected = ep
                break
        if selected is None:
            state.engaged = False
            state.skip_reason = f"active_provider={active} not configured or secret missing"
            state.reason = state.skip_reason
            return state
        selected.health_status = "unknown"
        state.pool = [selected]
        state.selected = selected
        state.requested_tier = "default"
        state.reason = f"mode=fixed;active_provider={active}"
        state.roles = {"default": selected, "strong": selected, "fast": selected}
        state.engaged = True
        state.tried_ids = [selected.endpoint_id]
        return state

    # auto routing
    candidates = discover_candidates(llm_config)
    if not candidates:
        state.engaged = False
        state.skip_reason = "no_configured_providers"
        state.reason = state.skip_reason
        return state

    if state.health_check:
        candidates = probe_candidates(candidates, engine=engine)
    else:
        for ep in candidates:
            if ep.health_status == "unknown":
                # Treat as usable without probe
                pass

    state.pool = list(candidates)
    healthy = [
        ep
        for ep in candidates
        if ep.health_status in ("healthy", "rate_limited")
        or (not state.health_check and ep.health_status == "unknown")
    ]
    if not healthy and not state.health_check:
        healthy = list(candidates)
    if not healthy:
        # last resort: try all unknown/unreachable once? Keep empty → fail clearly
        state.engaged = False
        state.skip_reason = "no_healthy_providers"
        state.reason = "all_configured_providers_unhealthy"
        return state

    state.roles = assign_role_endpoints(healthy, preferences=state.preferences)
    tier, reason = resolve_tier(ctx)
    state.requested_tier = tier
    state.reason = reason
    selected = select_endpoint(healthy, tier, preferences=state.preferences)  # type: ignore[arg-type]
    if selected is None:
        selected = healthy[0]
        state.reason = reason + ";fallback_first_healthy"
    state.selected = selected
    state.engaged = True
    state.tried_ids = [selected.endpoint_id]
    return state


def re_resolve_tier(state: LLMRouterState, ctx: RoutingContext) -> Optional[LLMEndpoint]:
    """Re-select endpoint for a new round/context without re-probing."""
    if state.mode == "fixed" or state.selected is None:
        return state.selected
    ctx.mode = state.mode
    if state.force_profile:
        ctx.force_profile = state.force_profile
    else:
        tier, reason = resolve_tier(ctx)
    state.requested_tier = tier
    state.reason = reason
    usable = [
        ep
        for ep in state.pool
        if ep.health_status in ("healthy", "rate_limited", "unknown")
        and ep.endpoint_id not in ()  # keep all healthy
    ]
    # Prefer not to re-use permanently failed ones
    usable = [ep for ep in usable if ep.health_status != "auth_failed"]
    if not usable:
        usable = [ep for ep in state.pool if ep.health_status != "auth_failed"]
    selected = select_endpoint(usable, tier, preferences=state.preferences)  # type: ignore[arg-type]
    if selected is not None:
        state.selected = selected
        if selected.endpoint_id not in state.tried_ids:
            state.tried_ids.append(selected.endpoint_id)
    return state.selected


def failover_next(state: LLMRouterState, *, cause: str) -> Optional[LLMEndpoint]:
    """Mark current unhealthy and pick next candidate."""
    if not state.failover_enabled:
        return None
    if state.selected is None:
        return None
    prev = state.selected
    mark_endpoint_unhealthy(prev, error=cause, status="unreachable")
    if not state.failover_occurred:
        state.failover_occurred = True
        state.failover_from = {"provider": prev.provider_key, "model": prev.model}
        state.failover_cause = cause[:300]

    next_ep = select_endpoint(
        state.pool,
        state.requested_tier,  # type: ignore[arg-type]
        preferences=state.preferences,
        exclude_ids=state.tried_ids,
    )
    if next_ep is None:
        # try any remaining healthy-ish
        next_ep = select_endpoint(
            state.pool,
            "default",
            preferences=state.preferences,
            exclude_ids=state.tried_ids,
        )
    if next_ep is None:
        return None
    state.selected = next_ep
    state.tried_ids.append(next_ep.endpoint_id)
    state.reason = f"{state.reason};failover_to={next_ep.provider_key}"
    return next_ep


def record_call(
    state: LLMRouterState,
    *,
    stage: str,
    round_index: int,
    status: str,
    duration_ms: int,
    error: Optional[str] = None,
) -> None:
    ep = state.selected
    entry: Dict[str, Any] = {
        "stage": stage,
        "round": int(round_index),
        "provider": ep.provider_key if ep else None,
        "model": ep.model if ep else None,
        "tier": state.requested_tier,
        "status": status,
        "duration_ms": int(duration_ms),
    }
    if error:
        entry["error"] = error[:300]
    state.calls.append(entry)
    if status == "success" and ep is not None:
        mark_endpoint_healthy(ep, latency_ms=duration_ms)


def skipped_summary(*, scope: str, mode: str = "fixed") -> Dict[str, Any]:
    return {
        "engaged": False,
        "skip_reason": f"scope={scope}",
        "mode": mode,
        "requested_tier": None,
        "selected": None,
        "reason": None,
        "failover": {"occurred": False},
        "pool": {"configured": 0, "healthy": 0, "candidates": []},
        "calls": [],
        "totals": {"call_count": 0, "duration_ms": 0},
    }
