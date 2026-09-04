import unittest

from services.action_failures import normalize_action_result
from services.context_engine import ContextEngine, ContextEngineConfig, ContextResolverRegistry


class HypothesisObservationSemanticsTests(unittest.TestCase):
    def test_infrastructure_failures_do_not_contradict_hypothesis(self):
        engine = ContextEngine(ContextEngineConfig(), ContextResolverRegistry())
        engine.session.hypotheses = [{"id": "h1", "status": "open", "statement": "uaf"}]
        engine.ingest_observation({"status": "failed", "failure_class": "permission_denied",
                                  "source": "policy", "summary": "denied"})
        hypothesis = engine.session.hypotheses[0]
        self.assertEqual(hypothesis.get("contradicting_evidence"), [])

    def test_verification_failure_is_typed_with_recovery(self):
        result = normalize_action_result({"status": "failed", "stderr": "compile failed"}, action="run_build")
        self.assertEqual(result["failure_class"], "compile_error")
        self.assertEqual(result["recovery"]["kind"], "inspect_build_output")
        self.assertTrue(result["tool_visible"])


if __name__ == "__main__":
    unittest.main()
