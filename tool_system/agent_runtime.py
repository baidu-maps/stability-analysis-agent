"""Backend-neutral facade for direct, LangChain and LangGraph execution."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from .runtime import RUN_STAGES, RuntimeState

ENGINE_TYPES = {"direct", "langchain", "langgraph"}


class AgentRuntime:
    """Single lifecycle surface; the configured executor remains the backend."""

    def __init__(self, executor: Any, *, engine: str = "direct"):
        self.executor = executor
        if engine not in ENGINE_TYPES:
            raise ValueError("engine must be one of: direct, langchain, langgraph")
        self.engine = engine
        self.state = RuntimeState()

    def _transition(self, stage: str, *, status: str = "running", reason: Optional[str] = None) -> None:
        self.state.transition(stage, status=status, reason=reason)
        trace = getattr(self.executor, "last_run_trace", None)
        if trace is not None:
            trace.stage = stage
            trace.engine = self.engine
            trace.emit("stage.transition", kind="stage", name=stage, status=status, reason=reason)
            self.state.last_event_seq = len(trace.events)

    def _finalize_decision(
        self,
        result: Dict[str, Any],
        *,
        verification_status: Optional[str] = None,
        report_dir: Optional[Path] = None,
    ) -> str:
        """Decide-stage scoring shared with offline evaluation."""
        from services.decide_scorer import apply_decide_to_result, score_repair_decision

        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        verification = result.get("verification") if isinstance(result.get("verification"), dict) else {}
        if verification_status is not None:
            verification = {**verification, "status": verification_status}
        post_fix = verification.get("post_fix_diagnosis") if isinstance(verification, dict) else None
        score = score_repair_decision(
            applied_ai_fixes=result.get("applied_ai_fixes") if isinstance(result.get("applied_ai_fixes"), dict) else None,
            diff_review=result.get("diff_review") if isinstance(result.get("diff_review"), dict) else metadata.get("diff_review"),
            verification=verification,
            post_fix_diagnosis=post_fix if isinstance(post_fix, dict) else None,
            run_status=str(result.get("status") or ""),
            pipeline_skipped=bool(metadata.get("pipeline_skipped")),
            crash_diagnosis=result.get("crash_diagnosis") if isinstance(result.get("crash_diagnosis"), dict) else None,
            structured_analysis=metadata.get("structured_analysis") if isinstance(metadata.get("structured_analysis"), dict) else None,
            runtime_trace=metadata.get("runtime_trace") if isinstance(metadata.get("runtime_trace"), dict) else None,
        )
        analyze_shaped = any(
            key in result for key in ("analysis", "crash_diagnosis", "applied_ai_fixes", "verification")
        )
        if analyze_shaped:
            from services.harness_judge import judge_run

            judge = judge_run(result, verification_status=verification_status)
            result["judge"] = judge.to_dict()
            decision = score.decision
            if judge.verdict == "reject" and decision in {"accept", "partial"}:
                decision = "reject"
            elif judge.verdict == "pending" and decision == "accept":
                decision = "pending"
            if decision != score.decision:
                from dataclasses import replace

                score = replace(score, decision=decision)
            self._record_observation(
                kind="judge_feedback",
                source="harness_judge",
                status=judge.verdict,
                summary="; ".join(judge.reasons) or "judge accepted run",
                details=judge.to_dict(),
                actionable=judge.verdict != "accept",
            )
            if report_dir is not None:
                from services.stage_artifacts import save_judge_artifact

                save_judge_artifact(report_dir, judge.to_dict())
        else:
            decision = score.decision
        self.state.decision = decision
        apply_decide_to_result(
            result,
            score,
            report_dir=report_dir,
            trace=getattr(self.executor, "last_run_trace", None),
        )
        trace = getattr(self.executor, "last_run_trace", None)
        if trace is not None:
            trace.emit(
                "decision.final",
                kind="decision",
                name=decision,
                status=str(result.get("status") or "unknown"),
                decision=decision,
            )
        return decision

    def run(
        self,
        workflow: str,
        problem: Dict[str, Any],
        *,
        defer_decision: bool = False,
        resume_plan: Optional[Dict[str, Any]] = None,
        restored_state: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if isinstance(restored_state, dict) and restored_state:
            self.restore_state(restored_state)
        else:
            self.state = RuntimeState()
        problem = dict(problem)
        if isinstance(resume_plan, dict):
            problem["_resume_plan"] = resume_plan
            replay_report = str(problem.get("_replay_source_report") or problem.get("_report_dir") or "").strip()
            if replay_report:
                from services.stage_artifacts import load_analyze_artifact, hydrate_problem_from_artifact

                artifact = load_analyze_artifact(Path(replay_report))
                if isinstance(artifact, dict):
                    problem = hydrate_problem_from_artifact(problem, artifact)
        create_trace = getattr(self.executor, "create_run_trace", None)
        restored_trace = problem.get("_restored_trace")
        if isinstance(restored_trace, dict):
            from tool_system.runtime import RunTrace

            self.executor._pending_run_trace = RunTrace.from_dict(restored_trace, engine=self.engine)
            self.executor.last_run_trace = self.executor._pending_run_trace
        elif callable(create_trace):
            create_trace(engine=self.engine, problem=problem)
        code_roots = problem.get("code_roots") if isinstance(problem.get("code_roots"), list) else []
        try:
            from services.git_worktree_manager import revision_for_code_roots
            source_revision = revision_for_code_roots(code_roots, include_diff=False)
            worktree_revision = revision_for_code_roots(code_roots, include_diff=True)
        except Exception:
            source_revision = worktree_revision = None
        report_dir_value = str(problem.get("_report_dir") or "").strip()
        input_artifact = str(Path(report_dir_value) / "00_run_request.json") if report_dir_value else None
        self._transition("observe")
        trace = getattr(self.executor, "last_run_trace", None)
        if trace is not None:
            self.state.session_id = trace.run_id
            trace.emit("session.started", kind="session", name=workflow, status="running")
        self.state.checkpoint(state={"workflow": workflow}, source_revision=source_revision,
                              worktree_revision=worktree_revision, input_artifact=input_artifact)
        self._transition("analyze")
        try:
            prepare_problem = dict(problem)
            prepare_problem["_runtime_owned_context_loop"] = True
            execute_prepare = getattr(self.executor, "execute_workflow_prepare", None)
            if callable(execute_prepare):
                result = execute_prepare(workflow, prepare_problem)
            else:
                result = self.executor.execute_workflow(workflow, prepare_problem)
            prepare = result.get("_analyze_prepare") if isinstance(result, dict) else None
            if isinstance(prepare, dict) and prepare.get("initial_prompt") and not prepare.get("skip_context_loop"):
                from services.analyze_pipeline import (
                    merge_analyze_prepare_result,
                    run_analyze_context_loop,
                    should_run_context_loop,
                )

                if should_run_context_loop(problem, prepare):
                    ctx = getattr(self.executor, "last_workflow_context", None)
                    if ctx is not None:
                        loop_out = run_analyze_context_loop(
                            context=ctx,
                            prepare=prepare,
                            problem=problem,
                            trace=getattr(self.executor, "last_run_trace", None),
                            runtime_state=self.state,
                            report_dir=report_dir_value or None,
                        )
                        result = merge_analyze_prepare_result(result, loop_out)
        except Exception as exc:
            self.state.transition("decide", status="error", reason=str(exc))
            self.state.checkpoint(state={"workflow": workflow, "error": str(exc)}, status="error",
                                  source_revision=source_revision, worktree_revision=worktree_revision,
                                  input_artifact=input_artifact)
            trace = getattr(self.executor, "last_run_trace", None)
            if trace is not None:
                trace.emit("stage.transition", kind="stage", name="decide", status="error", reason=str(exc))
            raise
        result_status = result.get("status") if isinstance(result, dict) else None
        output_artifact = None
        trace = getattr(self.executor, "last_run_trace", None)
        if report_dir_value and trace is not None:
            output_artifact = trace.write_artifact(
                Path(report_dir_value) / "artifacts", "stage_analyze_result.json", result,
            )
        self.state.checkpoint(state={"workflow": workflow, "result_status": result_status},
                              source_revision=source_revision, worktree_revision=worktree_revision,
                              input_artifact=input_artifact, output_artifact=output_artifact)
        self._transition("plan", status="completed")
        self.state.checkpoint(
            state={"workflow": workflow, "repair_candidate": result_status == "success"},
            source_revision=source_revision, worktree_revision=worktree_revision,
            input_artifact=output_artifact, output_artifact=output_artifact,
        )
        if not defer_decision:
            if result_status in {"verification_pending", "approval_required"}:
                self.pause(str(result_status))
            else:
                self._finalize_decision(result if isinstance(result, dict) else {})
                self._transition(
                    "decide",
                    status="completed" if result_status in {"success", None} else "error",
                    reason=None if result_status in {"success", None} else str(result_status),
                )
                self.state.checkpoint(
                    state={"result_status": result_status},
                    status="completed" if result_status in {"success", None} else "error",
                    source_revision=source_revision, worktree_revision=worktree_revision,
                    output_artifact=output_artifact,
                )
        if isinstance(result, dict):
            metadata = result.setdefault("metadata", {})
            metadata["runtime_engine"] = self.engine
            metadata["runtime_trace"] = getattr(getattr(self.executor, "last_run_trace", None), "snapshot", lambda: {})()
            metadata["runtime_state"] = self.state.to_dict()
            self._sync_observations(result)
        return result

    def _observation_store(self):
        context = getattr(self.executor, "last_workflow_context", None)
        if context is None:
            context = getattr(self.executor, "_workflow_context", None)
        return getattr(context, "observations", None) if context is not None else None

    def _record_observation(self, **payload: Any) -> None:
        store = self._observation_store()
        if store is not None and hasattr(store, "record"):
            store.record(**payload)

    def _sync_observations(self, result: Dict[str, Any]) -> None:
        store = self._observation_store()
        if store is None or not hasattr(store, "snapshot"):
            return
        snapshot = store.snapshot()
        if snapshot.get("count"):
            result.setdefault("metadata", {})["observations"] = snapshot

    def run_repair_and_verify(
        self,
        *,
        result: Dict[str, Any],
        code_roots: list,
        report_dir: Path,
        run_id: str,
        verification_config: Optional[Dict[str, Any]],
        llm_adapter: Any,
        backup_original_sources: bool = True,
        uaf_nullptr_guard_policy: Any = None,
        request_record: Optional[Dict[str, Any]] = None,
        post_fix_diagnosis: bool = True,
    ) -> Dict[str, Any]:
        """Run act/verify stages after workflow analysis completes."""
        from services.repair_pipeline import run_repair_pipeline

        tool_executor = None
        if hasattr(self.executor, "execute_tool"):
            tool_executor = lambda name, data: self.executor.execute_tool(name, data)

        self._transition("act")
        pipeline = run_repair_pipeline(
            result=result,
            code_roots=list(code_roots or []),
            report_dir=Path(report_dir),
            run_id=str(run_id or ""),
            verification_config=verification_config,
            llm_adapter=llm_adapter,
            backup_original_sources=backup_original_sources,
            uaf_nullptr_guard_policy=uaf_nullptr_guard_policy,
            trace=self.trace,
            request_record=request_record,
            post_fix_diagnosis=post_fix_diagnosis,
            tool_executor=tool_executor,
            runtime_state=self.state,
            policy=getattr(self.executor, "policy", None),
        )
        if isinstance(result, dict):
            result.update(pipeline.result_updates)
        terminal = result.get("status") if isinstance(result, dict) else None
        if terminal == "verification_pending":
            self.pause(
                "verification_pending",
                approval=(pipeline.verification_result or {}).get("approval")
                if isinstance(pipeline.verification_result, dict)
                else None,
            )
        elif terminal == "approval_required":
            pending = pipeline.pending_tool_approval or result.get("pending_tool_approval")
            approval = pending.get("approval") if isinstance(pending, dict) else None
            self.pause("approval_required", approval=approval if isinstance(approval, dict) else None)
            if isinstance(result, dict):
                metadata = result.setdefault("metadata", {})
                metadata["pending_tool_approval"] = pending
        else:
            verification_status = None
            if isinstance(pipeline.verification_result, dict):
                verification_status = str(pipeline.verification_result.get("status") or "")
                ctx = getattr(self.executor, "last_workflow_context", None)
                if ctx is not None and hasattr(ctx, "observations"):
                    verification_payload = pipeline.verification_result
                    ctx.observations.record(
                        kind="verification",
                        source=str(verification_payload.get("provider") or "verification"),
                        status=verification_status or "unknown",
                        summary=str(verification_payload.get("error") or verification_payload.get("output") or "verification completed")[:2000],
                        details=verification_payload,
                        actionable=verification_status != "passed",
                    )
            pre_judge = None
            if isinstance(result, dict):
                from services.harness_judge import judge_run

                pre_judge = judge_run(result, verification_status=verification_status).to_dict()
            if isinstance(result, dict):
                from services.feedback_analyze import should_run_feedback_analyze, run_feedback_analyze

                problem_payload = request_record if isinstance(request_record, dict) else {}
                report_path = str(report_dir) if report_dir is not None else ""
                if should_run_feedback_analyze(
                    result,
                    verification_status=verification_status,
                    judge=pre_judge,
                    problem=problem_payload,
                    trace=self.trace,
                ):
                    ctx = getattr(self.executor, "last_workflow_context", None)
                    if ctx is not None:
                        run_feedback_analyze(
                            context=ctx,
                            result=result,
                            problem=problem_payload,
                            trace=self.trace,
                            runtime_state=self.state,
                            report_dir=report_path or None,
                            judge=pre_judge,
                        )
            self._finalize_decision(
                result if isinstance(result, dict) else {},
                verification_status=verification_status,
                report_dir=Path(report_dir),
            )
            from services.memory_feedback import record_run_memory

            report_path = str(report_dir) if report_dir is not None else str(
                (result.get("metadata") or {}).get("_report_dir") or ""
            ).strip()
            result.setdefault("metadata", {})["memory_feedback"] = record_run_memory(
                result,
                report_dir=report_path or None,
            )
            feedback = result["metadata"]["memory_feedback"]
            self._record_observation(
                kind="memory_feedback",
                source="crash_engineering_memory",
                status="recorded" if feedback.get("recorded") else "skipped",
                summary=str(feedback.get("reason") or feedback.get("feedback_type") or "memory feedback processed"),
                details=feedback,
                actionable=not feedback.get("recorded", False) and feedback.get("reason") not in {"no_pattern_ids", "verification_not_terminal"},
            )
            self._transition(
                "decide",
                status="completed" if terminal in {"success", None} else "error",
                reason=result.get("error") if isinstance(result, dict) else None,
            )
            self.state.checkpoint(
                state={
                    "result_status": terminal,
                    "verification_status": (pipeline.verification_result or {}).get("status"),
                },
                status="completed" if terminal in {"success", None} else "error",
            )
        if isinstance(result, dict):
            metadata = result.setdefault("metadata", {})
            metadata["runtime_state"] = self.state.to_dict()
            metadata["runtime_trace"] = self.trace.snapshot() if self.trace is not None else {}
            self._sync_observations(result)
        return {
            "result": result,
            "applied_fix_result": pipeline.applied_fix_result,
            "verification_result": pipeline.verification_result,
            "apply_fix_duration_ms": pipeline.apply_fix_duration_ms,
            "pending_tool_approval": pipeline.pending_tool_approval,
        }

    def pause(self, reason: str, *, approval: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Pause the lifecycle without executing an implicit tool."""
        self.state.transition("verify", status="pending", reason=reason)
        self.state.approval = dict(approval) if approval else None
        self.state.checkpoint(state={"reason": reason}, status="pending")
        return self.state.to_dict()

    def restore_state(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Restore a serialized lifecycle state before a retry/resume."""
        self.state = RuntimeState.from_dict(payload)
        return self.state.to_dict()

    @classmethod
    def restore_from_report(
        cls,
        report_dir: Path,
        *,
        stage: str = "",
        checkpoint_id: str = "",
    ) -> Dict[str, Any]:
        """Load runtime_state and trace sidecar from a report directory."""
        report_dir = Path(report_dir)
        trace_path = report_dir / "00_runtime_trace.json"
        summary_path = report_dir / "00_run_summary.json"
        payload: Dict[str, Any] = {}
        if trace_path.is_file():
            payload["runtime_trace"] = json.loads(trace_path.read_text(encoding="utf-8"))
        elif summary_path.is_file():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            payload["runtime_trace"] = summary.get("trace")
            payload["runtime_state"] = summary.get("runtime_state")
        if summary_path.is_file() and "runtime_state" not in payload:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            payload["runtime_state"] = summary.get("runtime_state")
        approval_path = report_dir / "09_pending_tool_approval.json"
        if approval_path.is_file():
            try:
                pending = json.loads(approval_path.read_text(encoding="utf-8"))
                if isinstance(pending, dict):
                    payload["pending_tool_approval"] = pending
            except (OSError, ValueError):
                pass
        resume_plan: Dict[str, Any] = {"from_stage": stage or "analyze", "checkpoint_id": checkpoint_id, "skip_stages": []}
        runtime_state = payload.get("runtime_state")
        if checkpoint_id.startswith("analyze:round:"):
            try:
                round_index = int(checkpoint_id.rsplit(":", 1)[-1])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid round checkpoint: {checkpoint_id}") from exc
            from services.stage_artifacts import load_analyze_round_artifact

            round_artifact = load_analyze_round_artifact(report_dir, round_index)
            if round_artifact is None:
                raise ValueError(f"round artifact not found: {checkpoint_id}")
            resume_plan["round_index"] = round_index
            resume_plan["skip_stages"] = ["observe", "analyze"]
            payload["round_artifact"] = round_artifact
        elif isinstance(runtime_state, dict) and checkpoint_id:
            checkpoints = runtime_state.get("checkpoints") or []
            selected = next(
                (item for item in checkpoints if isinstance(item, dict) and item.get("checkpoint_id") == checkpoint_id),
                None,
            )
            if selected:
                resume_plan["checkpoint"] = selected
                try:
                    selected_idx = RUN_STAGES.index(str(selected.get("stage") or "observe"))
                    resume_plan["skip_stages"] = list(RUN_STAGES[: selected_idx + 1])
                except ValueError:
                    resume_plan["skip_stages"] = []
            else:
                raise ValueError(f"checkpoint not found: {checkpoint_id}")
        payload["resume_plan"] = resume_plan
        payload["restored_state"] = runtime_state
        return payload

    def retry_stage(self, stage: str) -> Dict[str, Any]:
        """Prepare a run to retry an explicit stage; execution remains caller-controlled."""
        if stage == "act":
            raise ValueError("act replay is forbidden; create a new explicit repair task")
        if stage not in {"observe", "analyze", "plan", "verify", "decide"}:
            raise ValueError(f"stage replay is forbidden: {stage}")
        if stage == "decide":
            self.state.transition("decide", status="completed", reason="state_restored")
            self.state.checkpoint(state={"restored": True}, status="completed")
            return self.state.to_dict()
        self.state.transition(stage, status="running", reason="explicit_stage_retry")
        previous = next((item for item in reversed(self.state.checkpoints) if item.stage == stage), None)
        retry_count = (previous.retry_count if previous is not None else 0) + 1
        self.state.checkpoint(
            state={"retry": True},
            status="running",
            retry_count=retry_count,
            idempotency_key=f"{self.state.stage}:{retry_count}",
        )
        return self.state.to_dict()

    def execute_tool(self, name: str, input_data: Dict[str, Any]) -> Dict[str, Any]:
        return self.executor.execute_tool(name, input_data)

    def execute_workflow(self, workflow: str, problem: Dict[str, Any]) -> Dict[str, Any]:
        return self.run(workflow, problem)

    def list_active(self):
        return self.executor.list_active()

    def stream(self, workflow: str, problem: Dict[str, Any]):
        return self.executor.execute_workflow_stream(workflow, problem)

    @property
    def trace(self) -> Any:
        return getattr(self.executor, "last_run_trace", None)
