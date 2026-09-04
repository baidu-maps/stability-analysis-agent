import unittest
from pathlib import Path

from services.repair_actions import RepairActionDeps, _verification_tool_action_handler


class VerificationProfileExecutionTests(unittest.TestCase):
    def test_declared_check_is_bound_to_runtime_action(self):
        calls = []

        def execute(name, payload):
            calls.append((name, payload))
            return {"status": "passed", "provider": name, "mode": payload.get("mode")}

        deps = RepairActionDeps(
            result={"context_session": {"hypotheses": []}},
            code_roots=["/tmp"], report_dir=Path("/tmp"), run_id="r1",
            verification_config={"checks": [{"id": "native", "kind": "native_replay",
                                               "command": ["runner", "--fixture", "x"],
                                               "iterations": 3, "timeout_sec": 12}]},
            tool_executor=execute,
        )
        out = _verification_tool_action_handler(deps, "reproduce_crash")({"check_id": "native"})
        self.assertEqual(out["status"], "passed")
        self.assertEqual(calls[0][1]["command"], ["runner", "--fixture", "x"])
        self.assertEqual(calls[0][1]["iterations"], 3)
        self.assertEqual(calls[0][1]["check_id"], "native")

    def test_profile_without_check_id_runs_all_checks_in_order(self):
        calls = []

        def execute(name, payload):
            calls.append(payload["check_id"])
            return {"status": "passed", "provider": name, "mode": payload.get("mode")}

        deps = RepairActionDeps(
            result={"context_session": {"hypotheses": []}},
            code_roots=["/tmp"], report_dir=Path("/tmp"), run_id="r1",
            verification_config={"execute_all_declared_checks": True, "checks": [
                {"id": "compile", "kind": "target_compile", "command": ["ninja"]},
                {"id": "replay", "kind": "native_replay", "command": ["runner"]},
            ]},
            tool_executor=execute,
        )
        out = _verification_tool_action_handler(deps, "run_build")({})
        self.assertEqual(calls, ["compile", "replay"])
        self.assertEqual(out["status"], "passed")
        self.assertEqual(out["profile_execution"], "all_declared_checks")

    def test_model_command_is_ignored_and_undeclared_check_is_not_executed(self):
        calls = []
        deps = RepairActionDeps(
            result={"context_session": {"hypotheses": []}}, code_roots=["/tmp"],
            report_dir=Path("/tmp"), run_id="r1",
            verification_config={"checks": [{"id": "safe", "kind": "replay", "command": ["safe-runner"]}]},
            tool_executor=lambda name, payload: calls.append(payload) or {"status": "passed", "provider": name, "mode": "reproduce"},
        )
        out = _verification_tool_action_handler(deps, "reproduce_crash")({
            "check_id": "missing", "verification": {"command": ["injected"]}
        })
        self.assertEqual(out["verification_status"], "not_configured")
        self.assertEqual(calls, [])

    def test_selected_plan_uses_profile_command_and_stable_fingerprint(self):
        calls = []
        config = {"profile_id": "p", "checks": [{"id": "safe", "kind": "replay",
                  "command": ["safe-runner"], "verification_level": "L3"}]}
        deps = RepairActionDeps(
            result={"context_session": {"hypotheses": []}}, code_roots=["/tmp"],
            report_dir=Path("/tmp"), run_id="r1", verification_config=config,
            tool_executor=lambda name, payload: calls.append(payload) or {"status": "passed", "provider": name, "mode": "reproduce"},
        )
        payload = {"reproduction_plan": {"check_id": "safe", "purpose": "pre_fix_reproduce"},
                   "verification_claim": {"statement": "reproduce target crash", "minimum_level": "L3"},
                   "verification": {"command": ["injected"]}}
        first = _verification_tool_action_handler(deps, "reproduce_crash")(payload)
        second = _verification_tool_action_handler(deps, "reproduce_crash")(payload)
        self.assertEqual(calls[0]["command"], ["safe-runner"])
        self.assertEqual(first["plan_fingerprint"], second["plan_fingerprint"])


if __name__ == "__main__":
    unittest.main()
