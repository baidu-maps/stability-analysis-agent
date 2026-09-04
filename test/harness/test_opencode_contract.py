import unittest

from services.context_parts import ContextPart, parts_from_evidence
from services.policy import PolicyEngine
from services.verification import parse_verification_diagnostics
from tool_system.runtime import RuntimeState
from services.runtime_actions import RuntimeAction, RuntimeActionExecutor


class OpenCodeContractTests(unittest.TestCase):
    def test_context_part_is_typed_and_stable(self):
        part = ContextPart(kind="tool_result", content="result", atomic_group="call_1", tokens=2)
        restored = ContextPart.from_mapping(part.to_dict())
        self.assertEqual(part.part_id, restored.part_id)
        self.assertEqual(restored.atomic_group, "call_1")
        self.assertEqual(parts_from_evidence([{"kind": "source_code", "content": "x", "priority": "stable"}])[0].kind, "stable_evidence")

    def test_action_hooks_are_ordered_and_hook_errors_are_isolated(self):
        state = RuntimeState()
        executor = RuntimeActionExecutor(state=state)
        executor.register(RuntimeAction("inspect_diff", lambda payload: {"status": "completed", "value": 1}))
        events = []
        executor.register_before_action_hook(lambda name, payload: events.append("before"))
        executor.register_after_action_hook(lambda name, payload, result: (_ for _ in ()).throw(RuntimeError("hook")))
        executor.execute("inspect_diff", {})
        self.assertEqual(events, ["before"])

    def test_stage_gate_blocks_write_during_diagnose(self):
        state = RuntimeState(stage="diagnose")
        executor = RuntimeActionExecutor(state=state)
        executor.register(RuntimeAction("apply_patch", lambda payload: {"status": "completed"}))
        with self.assertRaises(PermissionError):
            executor.execute("apply_patch", {})

    def test_pattern_permission_and_diagnostics(self):
        policy = PolicyEngine(permission_rules=[{"permission": "write", "patterns": ["src/*"], "decision": "deny"}])
        self.assertFalse(policy.check_permission("write", "src/a.cpp").allowed)
        diagnostics = parse_verification_diagnostics("src/a.cpp:12:4: error: use of undeclared identifier 'x'")
        self.assertEqual(diagnostics[0]["line"], 12)
        self.assertEqual(diagnostics[0]["severity"], "error")


if __name__ == "__main__":
    unittest.main()
