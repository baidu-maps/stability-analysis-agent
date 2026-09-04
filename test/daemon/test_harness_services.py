from __future__ import annotations
import os
import tempfile
import unittest
from pathlib import Path

from services.evidence_store import EvidenceContextManager, EvidenceItem, EvidenceStore
from services.policy import PolicyEngine
from services.run_store import load_snapshots, save_snapshot
from tool_system.runtime import RunTrace
from tool_system.agent_runtime import AgentRuntime
from services.diff_review import review_changed_files
from services.agent_schema import AgentDecision, RepairPlan, VerificationDecision
from services.runtime_actions import ACTION_NAMES, VERIFICATION_ACTION_TOOLS
from services.code_fixer import CodeFixer


class HarnessServiceTests(unittest.TestCase):
    def test_evidence_is_deduplicated_and_stable(self):
        store = EvidenceStore()
        first = store.add(EvidenceItem(kind="source_code", content="x", source="test", relevance=.2))
        second = store.add(EvidenceItem(kind="source_code", content="x", source="test", relevance=.9))
        self.assertEqual(first.evidence_id, second.evidence_id)
        self.assertEqual(store.items()[0]["relevance"], .9)

    def test_evidence_context_manager_applies_character_budget(self):
        manager = EvidenceContextManager(max_chars=1000)
        manager.add(EvidenceItem(kind="source", content="a" * 700, source="test", relevance=1))
        manager.add(EvidenceItem(kind="source", content="b" * 700, source="test", relevance=.5))
        package = manager.package()
        self.assertEqual(package["item_count"], 1)
        self.assertLessEqual(package["chars"], 1000)

    def test_oversized_first_evidence_is_compressed_within_budget(self):
        manager = EvidenceContextManager(max_chars=1000)
        manager.add(EvidenceItem(kind="source", content="x" * 3000, source="test", relevance=1))
        package = manager.package()
        self.assertEqual(package["item_count"], 1)
        self.assertEqual(package["truncated_count"], 1)
        self.assertLessEqual(package["chars"], 1000)

    def test_context_loop_prompt_is_assembled_by_manager(self):
        manager = EvidenceContextManager(max_chars=4000)
        manager.add(EvidenceItem(kind="source_code", content="void f() {}", source="context_loop", relevance=1))
        package = manager.package(min_round=0)
        assembled = manager.assemble_context_loop_prompt(
            "# 崩溃分析任务\nbase",
            evidence_package=package,
            is_final_round=True,
            early_final_reason="max_rounds",
        )
        self.assertIn("## 本轮任务", assembled["content"])
        self.assertIn("证据:", assembled["content"])
        bounded = manager.select_prompt(assembled["content"], max_chars=500)
        self.assertLessEqual(bounded["chars"], 500)

    def test_trace_emits_step_id(self):
        trace = RunTrace("step-id-test")
        event = trace.emit("stage.transition", kind="stage", name="analyze", status="running")
        self.assertTrue(str(event.get("step_id", "")).startswith("step_"))
        self.assertEqual(event.get("step_id"), "step_000001")

    def test_policy_requires_approval_and_allowlist(self):
        policy = PolicyEngine(allowed_commands=["pytest"], allowed_roots=["/tmp"])
        self.assertEqual(policy.check_command(["pytest"], workspace="/tmp", approved=False).decision, "approval_required")
        self.assertTrue(policy.check_command(["pytest"], workspace="/tmp", approved=True).allowed)
        self.assertFalse(policy.check_command(["sh"], workspace="/tmp", approved=True).allowed)

    def test_run_store_round_trip(self):
        class Run:
            run_id = "test-run-store"; transport_status = "verification_pending"; created_at = 1.0
            started_at = 1.0; finished_at = None; exit_code = 0; error = None; output_format = "json"
            report_dir = "/tmp/report"; workspace_dir = "/tmp/worktree"
            original_code_roots = []; isolated_code_roots = []; workspace_manifest = None; patch_path = None
            last_progress = None; last_progress_percent = None; completion_reason = "verification_pending"
            pending_changed_files = ["/tmp/worktree/a.py"]; pending_verification = {"status": "pending"}; request = None
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("STABILITY_AGENT_RUN_STORE")
            os.environ["STABILITY_AGENT_RUN_STORE"] = tmp
            try:
                save_snapshot(Run())
                self.assertEqual(load_snapshots()[0]["transport_status"], "verification_pending")
                self.assertEqual(load_snapshots()[0]["pending_changed_files"], ["/tmp/worktree/a.py"])
            finally:
                if old is None: os.environ.pop("STABILITY_AGENT_RUN_STORE", None)
                else: os.environ["STABILITY_AGENT_RUN_STORE"] = old

    def test_trace_has_monotonic_sequence(self):
        trace = RunTrace("trace-test")
        trace.emit("one"); trace.emit("two")
        self.assertEqual([x["seq"] for x in trace.events], [1, 2])

    def test_trace_aggregates_usage_cost_and_normalized_fields(self):
        trace = RunTrace("usage", engine="direct")
        event = trace.emit("llm.success", kind="llm", status="success",
                           token_usage={"prompt_tokens": 3, "completion_tokens": 2},
                           estimated_cost=0.25)
        budget = trace.snapshot()["budget"]
        self.assertEqual(budget["token_usage"]["total_tokens"], 5)
        self.assertEqual(budget["estimated_cost"], 0.25)
        for key in ("tool_call_id", "duration_ms", "input_hash", "output_hash",
                    "artifact_path", "retry_count", "failover_from", "approval_id",
                    "termination_reason"):
            self.assertIn(key, event)

    def test_strict_agent_repair_and_verification_schemas(self):
        decision, violations = AgentDecision.from_mapping({
            "agent_can_fetch_more": True, "context_requests": "bad",
        })
        self.assertFalse(decision.agent_can_fetch_more)
        self.assertTrue(violations)
        plan, violations = RepairPlan.from_mapping({"edits": [{"file": "../escape", "replacement_code": "x"}]})
        self.assertIsNone(plan)
        self.assertTrue(violations)
        verification, error = VerificationDecision.from_mapping({"status": "maybe", "provider": "x", "mode": "test"})
        self.assertIsNone(verification)
        self.assertTrue(error)

    def test_apply_fix_plan_rejects_malformed_schema_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "a.cpp"
            target.write_text("int f() { return 1; }\n", encoding="utf-8")
            result = CodeFixer(None).apply_fix_plan(
                {"edits": [{"file": str(target), "function_signature": "int f()"}]},
                [], [tmp],
            )
            self.assertFalse(result.success)
            self.assertEqual(result.skipped_reason, "schema_violation")
            self.assertEqual(target.read_text(encoding="utf-8"), "int f() { return 1; }\n")

    def test_runtime_trace_is_shared_with_workflow(self):
        class Executor:
            last_run_trace = None
            def create_run_trace(self, *, engine=None, problem=None):
                self.last_run_trace = RunTrace(engine=engine)
                return self.last_run_trace
            def execute_workflow(self, workflow, problem):
                self.last_run_trace.emit("workflow.finished", name=workflow)
                return {"status": "success"}
        executor = Executor()
        AgentRuntime(executor, engine="langgraph").run("crash_analysis", {})
        self.assertEqual(executor.last_run_trace.engine, "langgraph")
        self.assertEqual(executor.last_run_trace.events[0]["stage"], "observe")

    def test_runtime_checkpoint_and_pause_are_serializable(self):
        class Executor:
            last_run_trace = None
            def execute_workflow(self, workflow, problem):
                return {"status": "verification_pending"}
        runtime = AgentRuntime(Executor())
        state = runtime.run("crash_analysis", {})["metadata"]["runtime_state"]
        self.assertEqual(state["stage"], "verify")
        self.assertEqual(state["status"], "pending")
        self.assertTrue(state["checkpoints"])

    def test_runtime_records_failed_decision_checkpoint(self):
        class Executor:
            last_run_trace = RunTrace("failed-run")
            def execute_workflow(self, workflow, problem):
                raise RuntimeError("workflow failed")
        runtime = AgentRuntime(Executor())
        with self.assertRaisesRegex(RuntimeError, "workflow failed"):
            runtime.run("crash_analysis", {})
        self.assertEqual(runtime.state.status, "error")
        self.assertEqual(runtime.state.stage, "decide")
        self.assertEqual(runtime.state.checkpoints[-1].status, "error")

    def test_runtime_state_can_restore_and_retry_stage(self):
        class Executor: pass
        runtime = AgentRuntime(Executor())
        runtime.restore_state({"stage": "verify", "status": "pending", "checkpoints": []})
        with self.assertRaisesRegex(ValueError, "act replay is forbidden"):
            runtime.retry_stage("act")
        self.assertEqual(runtime.retry_stage("verify")["stage"], "verify")
        self.assertEqual(runtime.state.checkpoints[-1].retry_count, 1)

    def test_diff_review_rejects_unauthorized_files(self):
        review = review_changed_files(["src/a.cpp", "src/b.cpp"], ["src/a.cpp"])
        self.assertEqual(review.status, "failed")
        self.assertEqual(review.unauthorized_files, ["src/b.cpp"])

    def test_policy_rejects_tool_paths_outside_roots(self):
        policy = PolicyEngine(allowed_roots=[str(Path("/tmp/allowed").resolve())])
        decision = policy.check_tool(
            risk="read_only",
            workspace_paths=["/etc/passwd"],
        )
        self.assertFalse(decision.allowed)

    def test_trace_emits_structured_agent_event(self):
        trace = RunTrace("structured")
        payload = trace.emit("tool.success", kind="tool", name="demo", status="success")
        self.assertIn("agent_event", payload)
        self.assertEqual(payload["agent_event"]["event"], "tool.success")

    def test_evaluate_case_counts_trace_metrics(self):
        from services.evaluation import evaluate_case

        result = {
            "metadata": {
                "runtime_trace": {
                    "events": [
                        {"event": "agent.context_requests_parsed", "request_count": 2, "invalid_count": 1},
                        {"event": "tool.policy", "status": "denied"},
                    ],
                    "budget": {"tool_calls": 3, "llm_calls": 2},
                },
                "evidence_items": [{"kind": "source_code"}],
            },
            "verification": {"status": "passed", "post_fix_diagnosis": {"status": "passed"}},
            "applied_ai_fixes": {"success": True, "applied": [{"file": "a.cpp"}]},
        }
        evaluation = evaluate_case("case-1", result=result, allowed_files=["a.cpp"], duration_ms=1200)
        self.assertEqual(evaluation.runtime["invalid_context_requests"], 1)
        self.assertEqual(evaluation.runtime["policy_denials"], 1)
        self.assertEqual(evaluation.repair["post_fix_diagnosis_status"], "passed")

    def test_verification_tools_are_first_class_runtime_actions(self):
        for tool_name in VERIFICATION_ACTION_TOOLS:
            self.assertIn(tool_name, ACTION_NAMES)
