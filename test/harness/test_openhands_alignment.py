import tempfile
import unittest
from pathlib import Path

from services.action_security import ActionSecurityAnalyzer
from services.context_compactor import ContextCompactor
from services.workspace_revision import workspace_revisions
from tool_system.runtime import RunTrace, RuntimeState


class OpenHandsAlignmentTests(unittest.TestCase):
    def test_trace_has_canonical_event_and_schema(self):
        trace = RunTrace("r1")
        event = trace.emit("tool.success", kind="tool", name="repo_search", output="large")
        self.assertEqual(event["schema_version"], 1)
        self.assertEqual(event["canonical_event"], "tool_result")
        self.assertNotIn("output", event)

    def test_compactor_retains_control_before_old_history(self):
        out = ContextCompactor().compact([
            {"priority": "history", "content": "old" * 1000},
            {"priority": "control", "content": "FINAL_JSON_CONTRACT"},
        ], max_chars=100)
        self.assertIn("FINAL_JSON_CONTRACT", out.text)

    def test_security_rejects_outside_path_and_dangerous_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            decision = ActionSecurityAnalyzer().analyze_action(
                {"name": "run_build", "changed_files": ["/etc/passwd"], "command": ["sudo", "make"]}, tmp
            )
        self.assertFalse(decision.allowed)
        self.assertIn("dangerous_command", decision.risks)

    def test_non_git_revision_is_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.c"
            path.write_text("int a;", encoding="utf-8")
            first = workspace_revisions([tmp])
            second = workspace_revisions([tmp])
        self.assertEqual(first, second)

    def test_runtime_state_new_fields_are_backward_compatible(self):
        state = RuntimeState.from_dict({"stage": "analyze", "status": "running"})
        self.assertTrue(state.session_id)
        self.assertTrue(state.replay_safe)


if __name__ == "__main__":
    unittest.main()
