import tempfile
import unittest
from pathlib import Path

from services.agent_output_parser import parse_agent_decision
from services.context_engine import ContextEngine, ContextEngineConfig, ContextResolverRegistry
from services.verification import VerificationResult, verification_observation


class ContextInvestigationContractTest(unittest.TestCase):
    def test_verification_capabilities_and_selection_are_persisted(self):
        engine = ContextEngine(
            ContextEngineConfig(), ContextResolverRegistry(),
            verification_profile={"checks": [{"id": "replay", "kind": "replay",
                "provider": "test_runner", "description": "fixed fixture",
                "command": ["runner"], "verification_level": "L3"}]},
        )
        decision = engine.parse_decision(
            '{"agent_can_fetch_more": false, "context_requests": [], '
            '"verification_claim": {"statement": "reproduce crash", "minimum_level": "L3"}, '
            '"reproduction_plan": {"check_id": "replay", "purpose": "pre_fix_reproduce", "command": ["bad"]}}'
        )
        self.assertEqual(decision["reproduction_plan"], {"check_id": "replay", "purpose": "pre_fix_reproduce"})
        snapshot = engine.session.to_dict()
        self.assertEqual(snapshot["verification_capabilities"][0]["check_id"], "replay")
        self.assertNotIn("command", snapshot["verification_capabilities"][0])
        self.assertNotIn("command", snapshot["reproduction_plan"])

    def test_mixed_valid_invalid_requests_keep_valid_fetch(self):
        out = parse_agent_decision('{"agent_can_fetch_more":true,"context_requests":['
            '{"type":"function","symbol":"Foo::bar"},'
            '{"type":"bogus","symbol":"x"}]}')
        self.assertTrue(out["agent_can_fetch_more"])
        self.assertEqual(len(out["context_requests"]), 1)
        self.assertEqual(len(out["invalid_context_requests"]), 1)
        self.assertFalse(out["degraded"])

    def test_reproduction_plan_is_rejected_without_profile(self):
        engine = ContextEngine(ContextEngineConfig(), ContextResolverRegistry())
        decision = engine.parse_decision(
            '{"agent_can_fetch_more": false, "context_requests": [], '
            '"reproduction_plan": {"check_id": "guessed", "purpose": "pre_fix_reproduce"}}'
        )
        self.assertEqual(decision["reproduction_plan"], {})
        self.assertEqual(engine.session.reproduction_plan, {})

    def test_investigation_observation_updates_hypothesis(self):
        engine = ContextEngine(ContextEngineConfig(), ContextResolverRegistry())
        engine.update_investigation({"hypotheses": [{"id": "h1", "statement": "uaf", "confidence": .5}]}, round_index=0)
        engine.ingest_observation({"status": "failed", "source": "run_tests", "summary": "compile error"}, round_index=1)
        self.assertEqual(len(engine.session.hypotheses[0]["contradicting_evidence"]), 1)

    def test_verification_observation_is_structured(self):
        result = VerificationResult("failed", "local_command", "build", output="bad")
        observation = verification_observation(result)
        self.assertEqual(observation["kind"], "verification")
        self.assertIn("failure_class", observation)


if __name__ == "__main__":
    unittest.main()
