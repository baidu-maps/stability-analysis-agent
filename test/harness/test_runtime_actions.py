from __future__ import annotations

import time
import unittest

from services.runtime_actions import ApprovalBinding, RuntimeAction, RuntimeActionExecutor


def _granted_approval(**overrides):
    base = {
        "status": "granted",
        "approval_id": "appr-1",
        "run_id": "run-1",
        "tool_call_id": "tc-1",
        "command_fingerprint": "fp-1",
        "scope": "single_command",
        "expires_at": time.time() + 3600,
    }
    base.update(overrides)
    return base


def _binding(**overrides):
    base = {
        "run_id": "run-1",
        "tool_call_id": "tc-1",
        "fingerprint": "fp-1",
        "approval_id": "appr-1",
    }
    base.update(overrides)
    return ApprovalBinding(**base)


class RuntimeActionApprovalTests(unittest.TestCase):
    def test_schema_violation_is_traced_and_checkpointed(self):
        from tool_system.runtime import RunTrace, RuntimeState

        state = RuntimeState()
        trace = RunTrace("schema-test")
        executor = RuntimeActionExecutor(state=state, trace=trace)
        executor.register(RuntimeAction("apply_patch", lambda payload: {"success": True}, input_schema={"code_roots": list}))
        with self.assertRaises(ValueError):
            executor.execute("apply_patch", {"code_roots": ["../outside"]})
        self.assertTrue(any(item.get("event") == "action.schema_violation" for item in trace.events))
        self.assertTrue(state.checkpoints)

    def test_command_must_be_argv_list(self):
        from tool_system.runtime import RuntimeState

        state = RuntimeState()
        executor = RuntimeActionExecutor(state=state)
        executor.register(RuntimeAction("verify", lambda payload: {"status": "pending"}, input_schema={"artifact_dir": str}))
        with self.assertRaises(ValueError):
            executor.execute("verify", {"artifact_dir": "/tmp", "verification": {"command": "make test"}})

    def test_paths_must_stay_under_authorized_workspace(self):
        from tool_system.runtime import RuntimeState

        state = RuntimeState()
        executor = RuntimeActionExecutor(state=state)
        executor.register(RuntimeAction("inspect_diff", lambda payload: {"ok": True},
                                        input_schema={"artifact_dir": str}))
        with self.assertRaises(ValueError):
            executor.execute(
                "inspect_diff",
                {
                    "workspace": "/tmp/runtime-authorized",
                    "report_dir": "/tmp/runtime-report",
                    "artifact_dir": "/tmp/runtime-report/artifacts",
                    "changed_files": ["/tmp/outside/file.cpp"],
                },
            )

    def test_report_artifacts_may_use_explicit_report_root(self):
        from tool_system.runtime import RuntimeState

        state = RuntimeState()
        executor = RuntimeActionExecutor(state=state)
        executor.register(RuntimeAction("inspect_diff", lambda payload: {"ok": True},
                                        input_schema={"artifact_dir": str}))
        result = executor.execute(
            "inspect_diff",
            {
                "workspace": "/tmp/runtime-authorized",
                "report_dir": "/tmp/runtime-report",
                "artifact_dir": "/tmp/runtime-report/artifacts",
                "changed_files": ["src/file.cpp"],
            },
        )
        self.assertTrue(result["ok"])

    def test_consumed_approval_is_rejected(self):
        from tool_system.runtime import RuntimeState

        state = RuntimeState()
        executor = RuntimeActionExecutor(state=state)
        executor.register(
            RuntimeAction(
                "apply_patch",
                lambda payload: {"success": True},
                requires_approval=True,
                risk="workspace_write",
                side_effect=True,
                input_schema={"code_roots": list},
            )
        )
        with self.assertRaises(PermissionError):
            executor.execute(
                "apply_patch",
                {"code_roots": ["/tmp"]},
                approval={"status": "consumed", "approval_id": "appr-1"},
            )

    def test_granted_approval_requires_binding(self):
        from tool_system.runtime import RuntimeState

        state = RuntimeState()
        executor = RuntimeActionExecutor(state=state)
        executor.register(
            RuntimeAction(
                "apply_patch",
                lambda payload: {"success": True},
                requires_approval=True,
                risk="workspace_write",
                side_effect=True,
                input_schema={"code_roots": list},
            )
        )
        with self.assertRaises(PermissionError):
            executor.execute(
                "apply_patch",
                {"code_roots": ["/tmp"]},
                approval=_granted_approval(),
            )

    def test_granted_approval_with_binding_is_accepted(self):
        from tool_system.runtime import RuntimeState

        state = RuntimeState()
        executor = RuntimeActionExecutor(state=state)
        executor.register(
            RuntimeAction(
                "apply_patch",
                lambda payload: {"success": True},
                requires_approval=True,
                risk="workspace_write",
                side_effect=True,
                input_schema={"code_roots": list},
            )
        )
        result = executor.execute(
            "apply_patch",
            {"code_roots": ["/tmp"]},
            approval=_granted_approval(),
            approval_binding=_binding(),
        )
        self.assertTrue(result["success"])

    def test_expired_approval_is_rejected(self):
        from tool_system.runtime import RuntimeState

        state = RuntimeState()
        executor = RuntimeActionExecutor(state=state)
        executor.register(
            RuntimeAction(
                "run_build",
                lambda payload: {"success": True},
                requires_approval=True,
                risk="execute",
                side_effect=True,
                input_schema={"artifact_dir": str},
            )
        )
        with self.assertRaises(PermissionError):
            executor.execute(
                "run_build",
                {"artifact_dir": "/tmp"},
                approval=_granted_approval(expires_at=time.time() - 10),
                approval_binding=_binding(),
            )

    def test_fingerprint_mismatch_is_rejected(self):
        from tool_system.runtime import RuntimeState

        state = RuntimeState()
        executor = RuntimeActionExecutor(state=state)
        executor.register(
            RuntimeAction(
                "run_tests",
                lambda payload: {"success": True},
                requires_approval=True,
                risk="execute",
                side_effect=True,
                input_schema={"artifact_dir": str},
            )
        )
        with self.assertRaises(PermissionError):
            executor.execute(
                "run_tests",
                {"artifact_dir": "/tmp"},
                approval=_granted_approval(command_fingerprint="expected"),
                approval_binding=_binding(fingerprint="different"),
            )


if __name__ == "__main__":
    unittest.main()
