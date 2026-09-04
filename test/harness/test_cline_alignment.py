import tempfile
import unittest
from pathlib import Path

from services.action_failures import normalize_action_result
from services.context_compactor import ContextCompactor
from services.context_engine import ContextEngine, ContextEngineConfig, ContextResolverRegistry, CallableContextResolver
from services.file_context_tracker import FileContextTracker
from tool_system.runtime import RuntimeState


class ClineAlignmentTests(unittest.TestCase):
    def test_file_tracker_detects_external_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.c"
            path.write_text("int a;", encoding="utf-8")
            tracker = FileContextTracker()
            tracker.record_read(str(path), path.read_text(), 1, 1, "rev1")
            path.write_text("int changed;", encoding="utf-8")
            result = tracker.check_stale(str(path), workspace_revision="rev1")
            self.assertTrue(result["stale"])
            self.assertNotEqual(result["expected_fingerprint"], result["actual_fingerprint"])

    def test_compactor_summary_failure_falls_back(self):
        out = ContextCompactor().compact([
            {"priority": "control", "content": "JSON_CONTRACT"},
            {"priority": "history", "content": "x" * 1000},
        ], max_chars=120, summary_provider=lambda _: (_ for _ in ()).throw(RuntimeError("down")))
        self.assertIn("JSON_CONTRACT", out.text)
        self.assertEqual(out.metadata["summary_provider_used"], 0)

    def test_focus_chain_and_checkpoint_are_serializable(self):
        engine = ContextEngine(ContextEngineConfig(), ContextResolverRegistry([
            CallableContextResolver("function", lambda request: {"success": True, "content": "ok"})
        ]))
        engine.update_investigation({"hypotheses": [{"statement": "lifetime bug", "id": "h1"}],
                                    "next_action": {"kind": "inspect", "target": "destroy", "reason": "find path"}}, round_index=1)
        self.assertEqual(engine.session.focus_chain[0]["status"], "open")
        state = RuntimeState()
        state.focus_chain = engine.session.focus_chain
        restored = RuntimeState.from_dict(state.to_dict())
        self.assertEqual(restored.focus_chain[0]["id"], "f1")

    def test_failure_normalization(self):
        result = normalize_action_result({"status": "failed", "error": "file fingerprint stale"}, action="apply_patch")
        self.assertEqual(result["failure_class"], "stale_file")
        self.assertFalse(result["retryable"])
        self.assertEqual(result["fallback_action"], "read_file")


if __name__ == "__main__":
    unittest.main()
