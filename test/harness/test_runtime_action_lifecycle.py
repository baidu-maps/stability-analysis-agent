import unittest

from services.runtime_actions import RuntimeAction, RuntimeActionExecutor
from tool_system.runtime import RuntimeState, RunTrace


class RuntimeActionLifecycleTests(unittest.TestCase):
    def test_verification_selection_survives_checkpoint_round_trip(self):
        state = RuntimeState()
        state.verification_capabilities = [{"check_id": "replay", "verification_level": "L3"}]
        state.verification_claim = {"statement": "reproduce target", "minimum_level": "L3"}
        state.reproduction_plan = {"check_id": "replay", "purpose": "pre_fix_reproduce"}
        state.verification_plan_fingerprint = "plan-1"
        state.checkpoint()
        restored = RuntimeState.from_dict(state.to_dict())
        self.assertEqual(restored.reproduction_plan["check_id"], "replay")
        self.assertEqual(restored.checkpoints[0].verification_plan_fingerprint, "plan-1")

    def test_failed_result_runs_failure_hook_and_checkpoints(self):
        state = RuntimeState()
        trace = RunTrace()
        executor = RuntimeActionExecutor(state=state, trace=trace)
        executor.register(RuntimeAction("inspect_diff", lambda _: {"status": "failed", "error": "empty"}))
        calls = []
        executor.register_failure_hook(lambda name, payload, exc: calls.append(name))
        result = executor.execute("inspect_diff", {})
        self.assertEqual(result["failure_class"], "empty_result")
        self.assertEqual(calls, ["inspect_diff"])
        self.assertTrue(state.checkpoints)

    def test_new_stage_names_are_transitionable_and_legacy_aliases_restore(self):
        state = RuntimeState()
        for stage in ("diagnose", "evidence_review", "repair_proposal", "judge"):
            state.transition(stage)
        restored = RuntimeState.from_dict({"stage": "analysis", "checkpoints": []})
        self.assertEqual(restored.stage, "analyze")


if __name__ == "__main__":
    unittest.main()
