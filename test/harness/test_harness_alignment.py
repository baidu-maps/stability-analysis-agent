from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from services.context_engine import ContextEngine, ContextEngineConfig, ContextResolverRegistry
from services.harness_judge import judge_run
from services.memory_feedback import record_verified_feedback
from services.observations import ObservationStore
from services.decide_scorer import score_repair_decision
from skill_system.parser import load_skill_bundle
from tool_system.tool_gateway import ToolExecutionGateway


class _ReadTool:
    definition = MagicMock(
        risk="read_only",
        side_effect=False,
        requires_approval=False,
        timeout_sec=None,
        allowed_roots=[],
    )

    def runtime_policy(self):
        return {"risk": "read_only", "cost_class": "low"}

    def execute(self, input_data):
        return {"ok": True}


class HarnessAlignmentTests(unittest.TestCase):
    def test_tool_gateway_records_observation(self):
        store = ObservationStore()
        result = ToolExecutionGateway().execute(
            "read", _ReadTool(), {"_observation_store": store},
        )
        self.assertTrue(result["ok"])
        self.assertEqual(store.items()[0]["kind"], "tool_result")

    def test_context_engine_exposes_recent_observations(self):
        store = ObservationStore()
        store.record(
            kind="verification",
            source="tests",
            status="failed",
            summary="unit test failed",
            actionable=True,
        )
        engine = ContextEngine(
            ContextEngineConfig(),
            ContextResolverRegistry(),
            observation_store=store,
        )
        delta = engine.evidence_delta([], round_index=1)
        self.assertTrue(any(item["kind"] == "runtime_observation" for item in delta))
        self.assertIn("unit test failed", "\n".join(item["content"] for item in delta))

    def test_context_engine_includes_observations_on_first_turn(self):
        store = ObservationStore()
        store.record(kind="tool_error", source="repo_search", status="failed", summary="index unavailable")
        engine = ContextEngine(ContextEngineConfig(), ContextResolverRegistry(), observation_store=store)
        prompt = engine.build_prompt("# 崩溃分析任务\n分析")
        self.assertIn("index unavailable", prompt)

    def test_judge_rejects_unverified_repair(self):
        judged = judge_run({
            "analysis": "done",
            "crash_diagnosis": {"category": "memory"},
            "applied_ai_fixes": {"success": True},
            "verification": {"status": "failed", "error": "tests failed"},
            "context_session": {"termination_reason": "model_final"},
            "metadata": {"structured_analysis": {"root_cause": "memory"}},
        })
        self.assertEqual(judged.verdict, "reject")
        self.assertFalse(judged.gates["verification_passed"])
        self.assertTrue(judged.questions)

    def test_repair_without_verification_is_not_accepted_by_scorer(self):
        score = score_repair_decision(
            applied_ai_fixes={"success": True, "applied": [{"status": "applied"}]},
            diff_review={"status": "passed"},
            verification={"status": "skipped"},
            run_status="success",
        )
        self.assertNotEqual(score.decision, "accept")
        self.assertFalse(score.verification_passed)

    def test_streaming_tool_is_audited_and_internal_handles_are_hidden(self):
        store = ObservationStore()

        class StreamTool(_ReadTool):
            def execute_stream(self, input_data):
                self.seen = dict(input_data)
                yield "a"
                yield "b"

        tool = StreamTool()
        output = list(ToolExecutionGateway().execute_stream(
            "stream", tool, {"value": 1, "_observation_store": store},
        ))
        self.assertEqual(output, ["a", "b"])
        self.assertNotIn("_observation_store", tool.seen)
        self.assertEqual(store.items()[-1]["kind"], "tool_result")

    def test_memory_feedback_requires_terminal_verification(self):
        self.assertEqual(
            record_verified_feedback({"verification": {"status": "pending"}})["reason"],
            "verification_not_terminal",
        )

    def test_memory_feedback_records_verified_pattern(self):
        analyzer = MagicMock()
        handle = MagicMock(analyzer=analyzer)
        with patch("rag.vector_store_config.get_vector_store", return_value=handle):
            feedback = record_verified_feedback({
                "pattern_hits": [{"pattern_id": "pattern-1"}],
                "verification": {"status": "passed", "output": "tests passed"},
            })
        self.assertTrue(feedback["recorded"])
        analyzer.record_feedback.assert_called_once_with("pattern-1", "adopted", "tests passed")

    def test_record_run_memory_commits_case_on_passed_verification(self):
        with patch("services.memory_feedback.record_verified_feedback", return_value={"recorded": True}):
            with patch("rag.case_writer.commit_from_report_dir", return_value={"ok": True, "pattern_id": "p1"}):
                with patch("rag.case_writer.write_commit_audit", return_value=Path("/tmp/audit.json")):
                    from services.memory_feedback import record_run_memory

                    out = record_run_memory(
                        {
                            "verification": {"status": "passed"},
                            "pattern_hits": [{"pattern_id": "pattern-1"}],
                        },
                        report_dir="/tmp/report",
                    )
        self.assertTrue(out["recorded"])
        self.assertTrue(out["case_commit"].get("ok"))

    def test_skill_network_permission_denies_network_tool(self):
        from tool_system.tool_gateway import ToolExecutionGateway
        from skill_system.runtime import _policy_from_skill_capabilities

        policy = _policy_from_skill_capabilities({"permissions": {"network": False}})

        class _NetTool:
            definition = MagicMock(
                risk="network",
                side_effect=False,
                requires_approval=False,
                timeout_sec=None,
                allowed_roots=[],
            )

            def runtime_policy(self):
                return {"risk": "network"}

            def execute(self, input_data):
                return {"ok": True}

        with self.assertRaises(PermissionError):
            ToolExecutionGateway(policy).execute("net", _NetTool(), {})

    def test_skill_capability_projection_includes_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_text(
                "---\nname: demo\nallowed-tools: [repo_search]\ncontext: fork\n---\nBody\n",
                encoding="utf-8",
            )
            (root / "skill.json").write_text(
                '{"type":"tool","tags":["analysis"],"metadata":{"permissions":{"network":false}}}',
                encoding="utf-8",
            )
            capabilities = load_skill_bundle(root).capabilities
        self.assertEqual(capabilities["allowed_tools"], ["repo_search"])
        self.assertFalse(capabilities["permissions"]["network"])


if __name__ == "__main__":
    unittest.main()
