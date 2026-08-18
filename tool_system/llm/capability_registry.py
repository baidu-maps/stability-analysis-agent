#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内置 LLM 能力分档（provider + model → tier / score）。"""

from __future__ import annotations

import re
from typing import Literal, Tuple

TierName = Literal["strong", "default", "economy"]

# Higher score = stronger. Used to pick default/strong from a healthy pool.
_MODEL_RULES: Tuple[Tuple[str, TierName, int], ...] = (
    # Strong
    (r"(?i)claude[-_]?opus", "strong", 100),
    (r"(?i)claude[-_]?sonnet", "strong", 95),
    (r"(?i)gpt-4\.1(?!.*mini)", "strong", 94),
    (r"(?i)gpt-4o(?!.*mini)", "strong", 92),
    (r"(?i)gpt-4(?!.*mini|.*turbo-mini)", "strong", 90),
    (r"(?i)o[1-9]|o3|o4", "strong", 96),
    (r"(?i)deepseek[-_]?reasoner|deepseek[-_]?r1", "strong", 93),
    (r"(?i)qwen[-_]?max|qwen3?[-_]?max", "strong", 88),
    (r"(?i)ernie[-_]?4\.5|ernie[-_]?4\.0", "strong", 86),
    (r"(?i)glm-4(?:\.5)?(?:-plus)?$", "strong", 85),
    # Default
    (r"(?i)deepseek[-_]?chat|deepseek[-_]?v[23]", "default", 70),
    (r"(?i)qwen[-_]?plus|qwen2\.5", "default", 68),
    (r"(?i)glm-4", "default", 66),
    (r"(?i)moonshot|kimi", "default", 65),
    (r"(?i)minimax", "default", 64),
    (r"(?i)gpt-4o-mini|gpt-4\.1-mini", "default", 62),
    (r"(?i)claude[-_]?haiku", "default", 60),
    # Economy
    (r"(?i)turbo|mini|flash|lite|nano", "economy", 40),
)

_PROVIDER_DEFAULT_TIER: dict = {
    "claude": ("strong", 90),
    "openai": ("default", 70),
    "deepseek": ("default", 70),
    "qwen": ("default", 68),
    "zhipu_bigmodel": ("default", 66),
    "baidu_qianfan": ("default", 66),
    "kimi": ("default", 65),
    "minimax": ("default", 64),
}


def score_model(provider_key: str, model: str) -> Tuple[TierName, int]:
    """Return (tier, score) for a provider key + model id."""
    model_s = str(model or "").strip()
    provider_s = str(provider_key or "").strip().lower()
    if model_s:
        for pattern, tier, score in _MODEL_RULES:
            if re.search(pattern, model_s):
                return tier, score
    if provider_s in _PROVIDER_DEFAULT_TIER:
        tier, score = _PROVIDER_DEFAULT_TIER[provider_s]
        return tier, score  # type: ignore[return-value]
    return "default", 50


def profile_for_tier(tier: TierName) -> str:
    """Map capability tier to routing profile name."""
    if tier == "strong":
        return "strong"
    if tier == "economy":
        return "fast"
    return "default"
