import unittest

from services.context_engine import ContextEngine, ContextEngineConfig, ContextResolverRegistry
from services.evidence_graph import EvidenceGraph


class EvidenceGraphTests(unittest.TestCase):
    def test_graph_links_observation_to_hypothesis(self):
        engine = ContextEngine(ContextEngineConfig(), ContextResolverRegistry())
        engine.update_investigation({"hypotheses": [{"id": "h1", "statement": "callback uses released object"}]}, round_index=0)
        engine.ingest_observation({"status": "passed", "source": "native", "summary": "guard held"}, round_index=1)
        graph = engine.session.to_dict()["evidence_graph"]
        self.assertTrue(graph["nodes"])
        self.assertTrue(any(edge["relation"] == "supports" for edge in graph["edges"]))

    def test_graph_round_trip_is_stable(self):
        graph = EvidenceGraph()
        graph.link("hypothesis", "h1", "supports", "observation", "o1")
        restored = EvidenceGraph.from_dict(graph.to_dict())
        self.assertEqual(restored.to_dict(), graph.to_dict())


if __name__ == "__main__":
    unittest.main()
