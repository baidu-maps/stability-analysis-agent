import unittest
from unittest.mock import Mock

from services.context_engine import ContextEngine, ContextEngineConfig, ContextResolverRegistry
from services.investigation_controller import InvestigationController


class InvestigationControllerTests(unittest.TestCase):
    def test_plans_ordered_navigation_actions(self):
        controller = InvestigationController(max_actions=6)
        actions = controller.plan({
            "stack_symbols": ["Session::onCallback"],
            "fields": ["resource_"],
            "problem_types": ["native_uaf"],
        })
        self.assertEqual([item.kind for item in actions[:3]], ["locate", "find_callers", "find_references"])
        self.assertEqual(actions[0].to_request()["type"], "function")
        self.assertEqual(actions[1].to_request()["type"], "callers")

    def test_failed_action_becomes_blocked_after_bound(self):
        controller = InvestigationController(max_failures=2)
        action = controller.plan({"stack_symbols": ["Foo::bar"]})[0]
        controller.record_result(action, success=False)
        controller.record_result(action, success=False)
        self.assertIn("locate:Foo::bar", controller.state.blocked)
        self.assertTrue(all(item.kind != "locate" for item in controller.plan({"stack_symbols": ["Foo::bar"]})))

    def test_successful_action_is_not_replanned(self):
        controller = InvestigationController()
        action = controller.plan({"stack_symbols": ["Foo::bar"]})[0]
        controller.record_result(action, success=True)
        self.assertNotIn(action, controller.plan({"stack_symbols": ["Foo::bar"]}))

    def test_hypothesis_update_refreshes_repository_candidates(self):
        retriever = Mock()
        retriever.retrieve.return_value = [{"file": "src/lifetime.cpp", "line_start": 9, "score": 0.9}]
        engine = ContextEngine(ContextEngineConfig(), ContextResolverRegistry(), evidence_retriever=retriever)
        engine.session.repo_map["investigation_anchors"] = {"stack_symbols": ["Foo::bar"]}
        engine.update_investigation({"hypotheses": [{"id": "h1", "statement": "lifetime issue"}]}, round_index=1)
        self.assertTrue(engine.session.repo_map["retrieval_candidates"])
        self.assertTrue(retriever.retrieve.called)


if __name__ == "__main__":
    unittest.main()
