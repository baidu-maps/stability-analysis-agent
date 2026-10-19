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
    classify_llm_failure,
    failover_next,
    gateway_error_text,
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


def _oneapi_config():
    return _sample_llm_config(
        mode="fixed",
        active_provider="baidu_one_api",
        routing={"failover_enabled": True, "health_check": False},
        providers={
            "baidu_one_api": {
                "api_key": "sk-real-oneapi-key-for-test",
                "request_format": "anthropic_messages_compatible",
                "auth_header": "x-api-key",
                "auth_prefix": "",
                "base_url": "https://oneapi-comate.baidu-int.com/v1/messages",
                "model": "Claude Sonnet 5.5",
                "models": [
                    "Claude Haiku 4.5",
                    "Claude Sonnet 5.5",
                    "gpt-5.5",
                ],
            }
        },
    )


class ModelListFailoverTest(unittest.TestCase):
    def tearDown(self):
        clear_health_cache()

    def test_models_keep_primary_first(self):
        clear_health_cache()
        state = resolve_for_run(_oneapi_config(), health_check_override=False)
        self.assertEqual(
            [ep.model for ep in state.pool],
            ["Claude Sonnet 5.5", "Claude Haiku 4.5", "gpt-5.5"],
        )
        assert state.selected is not None
        self.assertEqual(state.selected.model, "Claude Sonnet 5.5")

    def test_failover_follows_model_order(self):
        clear_health_cache()
        state = resolve_for_run(_oneapi_config(), health_check_override=False)
        cause = "LLM call failed: Error code: 503 - 当前系统负载过高，请稍后再试"
        nxt = failover_next(state, cause=cause)
        assert nxt is not None
        self.assertEqual(nxt.model, "Claude Haiku 4.5")
        self.assertTrue(state.failover_occurred)
        self.assertEqual(state.failover_from["model"], "Claude Sonnet 5.5")
        nxt = failover_next(state, cause="timed out")
        assert nxt is not None
        self.assertEqual(nxt.model, "gpt-5.5")
        self.assertIsNone(failover_next(state, cause=cause))
        clear_health_cache()

    def test_auth_failure_does_not_switch_models(self):
        clear_health_cache()
        state = resolve_for_run(_oneapi_config(), health_check_override=False)
        nxt = failover_next(state, cause="LLM call failed: Error code: 401 - unauthorized")
        self.assertIsNone(nxt)
        self.assertFalse(state.failover_occurred)
        clear_health_cache()

    def test_classify_overload_and_gateway_body(self):
        self.assertEqual(
            classify_llm_failure("LLM call failed: 当前系统负载过高，请稍后再试"),
            "retryable",
        )
        self.assertEqual(classify_llm_failure("Error code: 401 unauthorized"), "auth")
        self.assertEqual(gateway_error_text("当前系统负载过高，请稍后再试"), "当前系统负载过高，请稍后再试")
        long_text = "分析结论正常。" * 40
        self.assertEqual(gateway_error_text(long_text), "")


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

    def test_request_snapshot_keeps_the_model_that_answered(self):
        from cli.main import _sync_request_llm_with_selected

        request = {"llm": {"provider": "baidu_one_api", "model": "Claude Sonnet 5.5"}}
        result = {
            "metadata": {
                "llm_routing": {
                    "selected": {
                        "provider": "baidu_one_api",
                        "model": "Claude Haiku 4.5",
                    }
                }
            }
        }
        _sync_request_llm_with_selected(request, result)
        self.assertEqual(request["llm"]["model"], "Claude Haiku 4.5")


if __name__ == "__main__":
    unittest.main()
