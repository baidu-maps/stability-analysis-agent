from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from services.agent_output_parser import parse_agent_decision
from services.stage_artifacts import (
    hydrate_problem_from_artifact,
    load_analyze_artifact,
    load_analyze_round_artifact,
    save_analyze_artifact,
    save_analyze_round_artifact,
)
from tool_system.agent_runtime import AgentRuntime
from workflows.crash_analysis_workflow import GenericCrashAnalyzeWorkflow
from tool_system.workflow import WorkflowContext


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "reports"


class CheckpointReplayTests(unittest.TestCase):
    def test_analyze_artifact_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = {"status": "success", "analysis": "done", "parse_result": {"ok": True}}
            path = save_analyze_artifact(root, payload)
            loaded = load_analyze_artifact(root)
            self.assertEqual(path.resolve(), (root / "artifacts" / "stage_analyze_result.json").resolve())
            self.assertEqual(loaded, payload)

    def test_analyze_round_artifact_uses_schema_v2(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_analyze_round_artifact(root, 2, {"analysis": "done"})
            loaded = load_analyze_round_artifact(root, 2)
            self.assertEqual(loaded.get("schema_version"), 2)
            self.assertEqual(loaded.get("round"), 2)

    def test_hydrate_problem_skips_workflow_tools(self):
        artifact = {"status": "success", "analysis": "cached", "parse_result": {"x": 1}}
        problem = hydrate_problem_from_artifact({"scope": "full"}, artifact)
        self.assertEqual(problem["_hydrated_analyze"], artifact)
        self.assertEqual(problem["_hydrated_parse_result"], {"x": 1})

    def test_restore_from_report_computes_skip_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_state = {
                "stage": "analyze",
                "status": "completed",
                "checkpoints": [
                    {
                        "checkpoint_id": "ckpt_analyze",
                        "stage": "analyze",
                        "status": "completed",
                        "state": {},
                    }
                ],
            }
            (root / "00_run_summary.json").write_text(
                json.dumps({"runtime_state": runtime_state}), encoding="utf-8"
            )
            restored = AgentRuntime.restore_from_report(root, checkpoint_id="ckpt_analyze")
            skip = restored["resume_plan"]["skip_stages"]
            self.assertIn("observe", skip)
            self.assertIn("analyze", skip)

    def test_workflow_solve_hydrate_short_circuit(self):
        workflow = GenericCrashAnalyzeWorkflow()
        context = WorkflowContext(
            llm_adapter=None,
            tool_registry=MagicMock(),
            config={},
        )
        artifact = {"status": "success", "analysis": "cached"}
        problem = {
            "crash_log": "ignored",
            "_hydrated_analyze": artifact,
            "_resume_plan": {"skip_stages": ["observe", "analyze"]},
        }
        with patch.object(workflow, "_resolve_scope", return_value="full"):
            result = workflow.solve(problem, context)
        self.assertEqual(result.get("analysis"), "cached")
        self.assertTrue(result.get("metadata", {}).get("pipeline_skipped"))

    def test_restore_round_checkpoint_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "artifacts").mkdir(parents=True)
            (root / "artifacts" / "analyze_round_1.json").write_text(
                json.dumps({"round": 1, "analysis": "cached-round"}), encoding="utf-8"
            )
            restored = AgentRuntime.restore_from_report(root, checkpoint_id="analyze:round:1")
            self.assertEqual(restored["resume_plan"].get("round_index"), 1)
            self.assertIn("analyze", restored["resume_plan"].get("skip_stages", []))

    def test_parse_agent_decision_degrades_on_invalid_requests(self):
        text = json.dumps(
            {
                "agent_can_fetch_more": True,
                "context_requests": [{"type": "unknown", "symbol": "foo"}],
            }
        )
        parsed = parse_agent_decision(text)
        self.assertTrue(parsed["degraded"])
        self.assertFalse(parsed["agent_can_fetch_more"])


if __name__ == "__main__":
    unittest.main()
