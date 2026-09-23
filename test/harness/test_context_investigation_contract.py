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

    def test_file_and_medium_aliases_normalize(self):
        """Model slips like type=file / priority=medium should still resolve."""
        out = parse_agent_decision(
            '{"agent_can_fetch_more":true,"context_requests":['
            '{"type":"file","symbol":"include/util/DemoTempl.h","priority":"medium","reason":"need Copy"},'
            '{"type":"function","symbol":"DemoEngineControl::UpdateRoute","priority":"medium","reason":"lifecycle"}'
            "]}"
        )
        self.assertFalse(out["degraded"])
        self.assertTrue(out["agent_can_fetch_more"])
        self.assertEqual(len(out["context_requests"]), 2)
        self.assertEqual(len(out["invalid_context_requests"]), 0)
        types = {item["type"] for item in out["context_requests"]}
        self.assertEqual(types, {"read_file", "function"})
        read_file = next(item for item in out["context_requests"] if item["type"] == "read_file")
        self.assertEqual(read_file["file"], "include/util/DemoTempl.h")
        self.assertEqual(read_file["priority"], "normal")
        func = next(item for item in out["context_requests"] if item["type"] == "function")
        self.assertEqual(func["priority"], "normal")

    def test_all_invalid_requests_do_not_add_missing_list_contract_error(self):
        out = parse_agent_decision(
            '{"agent_can_fetch_more":true,"context_requests":[{"type":"bogus","symbol":"x"}]}'
        )
        self.assertEqual(len(out["context_requests"]), 0)
        self.assertTrue(any("unsupported request type" in str(item.get("error")) for item in out["invalid_context_requests"]))
        self.assertFalse(
            any(
                item.get("error") == "agent_can_fetch_more=true requires context_requests"
                for item in out["invalid_context_requests"]
            )
        )
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
