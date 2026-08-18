#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for LLM routing (discover / capability / policy / summary)."""

from __future__ import annotations

import unittest
from unittest import mock

from tool_system.llm.capability_registry import score_model
from tool_system.llm.endpoint_pool import (
    clear_health_cache,
    discover_candidates,
)
from tool_system.llm.llm_router import (
    resolve_for_run,
    skipped_summary,
)
from tool_system.llm.routing_policy import (
    RoutingContext,
    resolve_tier,
    select_endpoint,
)


def _sample_llm_config(**overrides):
    cfg = {
        "mode": "fixed",
        "active_provider": "deepseek",
        "provider_defaults": {
            "request_format": "openai_chat_completions_compatible",
            "auth_type": "api_key",
        },
        "providers": {
            "deepseek": {
                "api_key": "sk-real-deepseek-key-for-test",
                "base_url": "https://api.deepseek.com/v1/chat/completions",
                "model": "deepseek-chat",
                "adapter_provider": "deepseek",
            },
            "claude": {
                "api_key": "sk-real-claude-key-for-test",
                "request_format": "anthropic_messages_compatible",
                "auth_header": "x-api-key",
                "auth_prefix": "",
                "base_url": "https://api.anthropic.com/v1/messages",
                "model": "claude-sonnet-4-20250514",
            },
            "openai": {
                "api_key": "YOUR_OPENAI_API_KEY",
                "base_url": "https://api.openai.com/v1/chat/completions",
                "model": "gpt-4o",
            },
        },
        "routing": {"failover_enabled": False, "health_check": False},
        "preferences": {},
    }
    cfg.update(overrides)
    return cfg


class CapabilityRegistryTest(unittest.TestCase):
    def test_strong_claude(self):
        tier, score = score_model("claude", "claude-sonnet-4-20250514")
        self.assertEqual(tier, "strong")
        self.assertGreaterEqual(score, 90)

    def test_default_deepseek_chat(self):
        tier, score = score_model("deepseek", "deepseek-chat")
        self.assertEqual(tier, "default")
        self.assertGreaterEqual(score, 60)


class DiscoverTest(unittest.TestCase):
    def test_filters_placeholder(self):
        cands = discover_candidates(_sample_llm_config())
        keys = {c.provider_key for c in cands}
        self.assertIn("deepseek", keys)
        self.assertIn("claude", keys)
        self.assertNotIn("openai", keys)

    def test_fixed_picks_active_only(self):
        clear_health_cache()
        state = resolve_for_run(
            _sample_llm_config(mode="fixed", active_provider="deepseek"),
            health_check_override=False,
        )
        self.assertTrue(state.engaged)
        self.assertEqual(state.mode, "fixed")
        self.assertIsNotNone(state.selected)
        assert state.selected is not None
        self.assertEqual(state.selected.provider_key, "deepseek")
        self.assertEqual(len(state.pool), 1)

    def test_auto_prefers_strong_claude_for_fix(self):
        clear_health_cache()
        state = resolve_for_run(
            _sample_llm_config(mode="auto"),
            health_check_override=False,
            routing_ctx=RoutingContext(mode="auto", prompt_mode="fix"),
        )
        self.assertTrue(state.engaged)
        self.assertEqual(state.requested_tier, "strong")
        assert state.selected is not None
        self.assertEqual(state.selected.provider_key, "claude")


class RoutingPolicyTest(unittest.TestCase):
    def test_force_profile(self):
        tier, reason = resolve_tier(RoutingContext(force_profile="fast"))
        self.assertEqual(tier, "fast")
        self.assertIn("cli_force", reason)

    def test_context_loop_followup(self):
        tier, _ = resolve_tier(
            RoutingContext(mode="auto", agent_loop="context_loop", round_index=2)
        )
        self.assertEqual(tier, "fast")

    def test_select_strong(self):
        cands = discover_candidates(_sample_llm_config())
        for c in cands:
            c.health_status = "healthy"
        ep = select_endpoint(cands, "strong")
        self.assertIsNotNone(ep)
        assert ep is not None
        self.assertEqual(ep.provider_key, "claude")


class SummaryHelperTest(unittest.TestCase):
    def test_skipped_summary(self):
        s = skipped_summary(scope="parse_stack_only", mode="fixed")
        self.assertFalse(s["engaged"])
        self.assertIn("parse_stack_only", s["skip_reason"])

    def test_router_summary_shape(self):
        clear_health_cache()
        state = resolve_for_run(
            _sample_llm_config(mode="fixed"),
            health_check_override=False,
        )
        d = state.to_summary_dict()
        self.assertIn("engaged", d)
        self.assertIn("pool", d)
        self.assertIn("calls", d)
        self.assertIn("failover", d)


class FinalSummaryLlmFieldTest(unittest.TestCase):
    def test_llm_summary_from_result_helper(self):
        # Import late to avoid heavy cli import side effects in environments without deps
        from cli.main import _llm_summary_from_result

        result = {
            "metadata": {
                "llm_routing": {
                    "engaged": True,
                    "mode": "fixed",
                    "selected": {"provider": "deepseek", "model": "deepseek-chat"},
                }
            }
        }
        out = _llm_summary_from_result(result, scope="full", request_record={})
        self.assertTrue(out["engaged"])
        self.assertEqual(out["mode"], "fixed")

        out2 = _llm_summary_from_result({}, scope="parse_stack_only", request_record={})
        self.assertFalse(out2["engaged"])


if __name__ == "__main__":
    unittest.main()
