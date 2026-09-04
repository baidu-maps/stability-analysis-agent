from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch


class DaemonToolSystemRuntimeTests(unittest.TestCase):
    @patch("daemon.server._get_ts_agent_runtime")
    def test_run_tool_system_workflow_uses_agent_runtime(self, get_runtime):
        from daemon.server import _run_tool_system_workflow

        runtime = MagicMock()
        runtime.run.return_value = {"status": "success", "analysis": "ok"}
        runtime.state.to_dict.return_value = {"stage": "decide", "decision": "accept"}
        runtime.state.decision = "accept"
        runtime.trace.snapshot.return_value = {"events": []}
        get_runtime.return_value = runtime

        result = _run_tool_system_workflow("crash_analysis", {"crash_log": "x"}, engine="langgraph")
        get_runtime.assert_called_once_with("langgraph")
        runtime.run.assert_called_once_with("crash_analysis", {"crash_log": "x"}, defer_decision=False)
        self.assertEqual(result["metadata"]["runtime_state"]["stage"], "decide")
        self.assertEqual(result["metadata"]["runtime_decision"], "accept")

    def test_runstate_stage_projects_from_runtime_state(self):
        from daemon.server import RunState

        run = RunState(
            run_id="r1",
            transport_status="approval_required",
            created_at=0.0,
            runtime_state={"stage": "verify", "status": "pending"},
        )
        self.assertEqual(run.stage, "verify")
        self.assertEqual(run.runtime_status, "pending")
        self.assertEqual(run.status, "approval_required")
        self.assertEqual(run.transport_status, "approval_required")

    def test_set_transport_status_syncs_runtime_state(self):
        from daemon.server import RunState, _set_transport_status

        run = RunState(run_id="r1", transport_status="queued", created_at=0.0)
        _set_transport_status(run, "verification_pending")
        self.assertEqual(run.transport_status, "verification_pending")
        self.assertEqual(run.runtime_status, "pending")
        self.assertEqual(run.stage, "verify")

    def test_resolve_tool_system_engine_rejects_explicit_invalid(self):
        from daemon.server import _resolve_tool_system_engine

        with self.assertRaises(ValueError):
            _resolve_tool_system_engine("sequential")

    def test_resolve_tool_system_engine_rejects_invalid_env_default(self):
        from daemon.server import _resolve_tool_system_engine

        old = os.environ.get("STABILITY_AGENT_DAEMON_ENGINE")
        os.environ["STABILITY_AGENT_DAEMON_ENGINE"] = "sequential"
        try:
            with self.assertRaises(ValueError):
                _resolve_tool_system_engine(None)
        finally:
            if old is None:
                os.environ.pop("STABILITY_AGENT_DAEMON_ENGINE", None)
            else:
                os.environ["STABILITY_AGENT_DAEMON_ENGINE"] = old


if __name__ == "__main__":
    unittest.main()
