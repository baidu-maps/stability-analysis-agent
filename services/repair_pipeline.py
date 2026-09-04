"""Act/Verify pipeline: worktree isolation, fix apply, diff review, verification, rollback."""
from __future__ import annotations

import json
import hashlib
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from services.git_worktree_manager import (
    IsolatedCodeWorkspace,
    WorktreeIsolationError,
    map_result_paths,
    prepare_isolated_workspace,
    revision_for_code_roots,
    workspace_revision,
    workspace_source_revision,
    write_workspace_artifacts,
    isolated_workspace_from_dict,
)
from services.repair_actions import RepairActionDeps, build_repair_action_executor, run_reanalyze_on_failure
from services.decide_scorer import score_repair_decision
from services.stage_artifacts import save_decide_artifact
from services.runtime_actions import ApprovalBinding, VERIFICATION_ACTION_TOOLS, pending_tool_action_name
from services.verification import (
    build_verification_config_with_reproduce_priority,
    consume_approval,
    merge_preset_candidates,
    validate_approval,
)
from services.repair_edit_loop import (
    classify_verification_failure,
    resolve_max_repair_edit_rounds,
    run_repair_edit_round,
    should_run_repair_edit_loop,
)
from services.verification_baseline import compare_verification_runs
from tool_system.runtime import RuntimeState


@dataclass
class RepairPipelineResult:
    applied_fix_result: Optional[Dict[str, Any]] = None
    verification_result: Optional[Dict[str, Any]] = None
    diff_review: Optional[Dict[str, Any]] = None
    isolated_workspace: Optional[IsolatedCodeWorkspace] = None
    apply_fix_duration_ms: Optional[int] = None
    result_updates: Dict[str, Any] = field(default_factory=dict)
    pending_tool_approval: Optional[Dict[str, Any]] = None
    runtime_state: Optional[Dict[str, Any]] = None
    baseline_result: Optional[Dict[str, Any]] = None


def _approval_binding_for_action(
    action_name: str,
    approval: Optional[Dict[str, Any]],
    *,
    run_id: str,
) -> Optional[ApprovalBinding]:
    if action_name not in VERIFICATION_ACTION_TOOLS:
        return None
    if not isinstance(approval, dict) or approval.get("status") != "granted":
        return None
    return ApprovalBinding.from_approval(approval, run_id=run_id)


def _discovered_verification_candidates(
    *,
    code_roots: List[str],
    verification_config: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    workspace = ""
    if isinstance(verification_config, dict) and verification_config.get("workspace"):
        workspace = str(verification_config.get("workspace") or "")
    if not workspace and code_roots:
        workspace = str(code_roots[0])
    if not workspace:
        return []
    presets = None
    if isinstance(verification_config, dict):
        presets = verification_config.get("presets")
    return [item.to_dict() for item in merge_preset_candidates(workspace, presets)]


def _attach_verification_candidates(
    verification_result: Optional[Dict[str, Any]],
    *,
    code_roots: List[str],
    verification_config: Optional[Dict[str, Any]] = None,
) -> None:
    if not isinstance(verification_result, dict):
        return
    if verification_result.get("status") != "pending":
        return
    candidates = _discovered_verification_candidates(
        code_roots=code_roots,
        verification_config=verification_config,
    )
    if candidates:
        verification_result["discovered_candidates"] = candidates


def should_run_repair_pipeline(
    *,
    apply_ai_fixes: bool,
    result: Dict[str, Any],
    scope: str,
) -> bool:
    if not apply_ai_fixes or result.get("status") != "success" or scope != "full":
        return False
    meta = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    if meta.get("pipeline_skipped") or meta.get("llm_skipped"):
        return False
    termination = str(result.get("termination_reason") or "").strip().lower()
    if termination in {"insufficient_evidence", "invalid_schema"}:
        return False
    analysis_text = str(result.get("analysis") or "")
    if re.search(r'"agent_can_fetch_more"\s*:\s*true', analysis_text, re.I):
        return False
    crash_diagnosis = result.get("crash_diagnosis") if isinstance(result.get("crash_diagnosis"), dict) else {}
    if crash_diagnosis:
        from tools.diagnosis.repair_gate import evaluate_repair_gate

        gate = evaluate_repair_gate(crash_diagnosis)
        if not gate.allowed and termination not in {"ready_to_fix"}:
            result.setdefault("metadata", {})["repair_gate"] = gate.to_dict()
            return False
    return True


def _tool_approval_granted(verification_config: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(verification_config, dict):
        return None
    approval = verification_config.get("approval")
    return approval if isinstance(approval, dict) and approval.get("status") == "granted" else None


def unisolated_workspace_fingerprint(run_id: str, code_roots: List[str]) -> str:
    """Bind the high-risk approval to this run and exact source roots."""
    normalized = sorted(str(Path(item).expanduser().resolve()) for item in code_roots if str(item))
    raw = json.dumps({"run_id": str(run_id), "code_roots": normalized}, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _load_json_object(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json_object(path: Path, value: Dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _persist_verification_resume(report_dir: Path, out: RepairPipelineResult, trace: Any) -> None:
    """Update the runtime-owned report set without discarding prior evidence."""
    _write_json_object(report_dir / "09_verification.json", out.verification_result or {})
    if out.pending_tool_approval is not None:
        _write_json_object(report_dir / "09_pending_tool_approval.json", out.pending_tool_approval)
    else:
        pending_path = report_dir / "09_pending_tool_approval.json"
        if pending_path.is_file():
            pending_path.unlink()
    trace_payload = trace.snapshot() if trace is not None and hasattr(trace, "snapshot") else {}
    if trace_payload:
        _write_json_object(report_dir / "00_runtime_trace.json", trace_payload)
    summary_path = report_dir / "00_run_summary.json"
    summary = _load_json_object(summary_path)
    summary.update({
        "status": out.result_updates.get("status") or summary.get("status"),
        "completion_reason": out.result_updates.get("completion_reason") or summary.get("completion_reason"),
        "verification": out.verification_result,
        "runtime_state": out.runtime_state,
    })
    if trace_payload:
        summary["trace"] = trace_payload
    if out.result_updates.get("error"):
        summary["error"] = out.result_updates["error"]
    elif summary.get("status") == "success":
        summary.pop("error", None)
    _write_json_object(summary_path, summary)


def _load_repair_analysis_result(report_dir: Path) -> Dict[str, Any]:
    result: Dict[str, Any] = {"status": "success"}
    summary = _load_json_object(report_dir / "00_run_summary.json")
    workflow = summary.get("workflow") if isinstance(summary.get("workflow"), dict) else {}
    if isinstance(workflow, dict):
        for key in ("analysis", "code_context"):
            if workflow.get(key) is not None:
                result[key] = workflow[key]
    gen_res = report_dir / "round_0" / "07_ai_gen_res.md"
    if gen_res.is_file():
        try:
            result["analysis"] = gen_res.read_text(encoding="utf-8")
        except OSError:
            pass
    code_ctx_path = report_dir / "04b_code_context.json"
    if code_ctx_path.is_file():
        loaded = _load_json_object(code_ctx_path)
        if loaded:
            result["code_context"] = loaded
    return result


def _finalize_repair_decide(
    out: RepairPipelineResult,
    *,
    report_dir: Path,
    trace: Any = None,
    runtime_state: Optional[RuntimeState] = None,
) -> RepairPipelineResult:
    updates = out.result_updates if isinstance(out.result_updates, dict) else {}
    verification = updates.get("verification") or out.verification_result
    post_fix = verification.get("post_fix_diagnosis") if isinstance(verification, dict) else None
    score = score_repair_decision(
        applied_ai_fixes=updates.get("applied_ai_fixes") or out.applied_fix_result,
        diff_review=updates.get("diff_review") or out.diff_review,
        verification=verification if isinstance(verification, dict) else None,
        post_fix_diagnosis=post_fix if isinstance(post_fix, dict) else None,
        run_status=str(updates.get("status") or ""),
    )
    payload = score.to_dict()
    updates["decide"] = payload
    out.result_updates = updates
    save_decide_artifact(report_dir, payload)
    terminal = str(updates.get("status") or "")
    if runtime_state is not None and terminal not in {"verification_pending", "approval_required"}:
        runtime_state.transition(
            "decide",
            status="completed" if score.decision == "accept" else "error",
            reason=score.decision,
        )
        runtime_state.decision = score.decision
    if trace is not None:
        trace.emit(
            "decision.scored",
            kind="decision",
            name=score.decision,
            status=score.decision,
            decision=score.decision,
            dimensions=score.dimensions,
            reasons=score.reasons,
        )
    return out


def _continue_repair_after_apply_patch(
    out: RepairPipelineResult,
    *,
    deps: RepairActionDeps,
    actions: Any,
    action_context: Dict[str, Any],
    workspace_action_approval: Optional[Dict[str, Any]],
    verification_config: Optional[Dict[str, Any]],
    code_roots: List[str],
    isolated_workspace: Optional[IsolatedCodeWorkspace],
    unisolated_approval: Optional[Dict[str, Any]],
    report_dir: Path,
    post_fix_diagnosis: bool = True,
    trace: Any = None,
    runtime_state: Optional[RuntimeState] = None,
) -> RepairPipelineResult:
    if not (out.applied_fix_result and out.applied_fix_result.get("success") and deps.changed_files):
        if out.applied_fix_result is not None:
            out.verification_result = {
                "status": "skipped",
                "provider": "none",
                "mode": "auto",
                "checks": [],
                "duration_ms": 0,
                "error": "未产生可验证的已应用修改",
            }
        out.result_updates = {
            "applied_ai_fixes": out.applied_fix_result,
            "verification": out.verification_result,
        }
        if unisolated_approval is not None:
            out.result_updates["unisolated_approval"] = unisolated_approval
        if isolated_workspace is not None:
            write_workspace_artifacts(isolated_workspace, report_dir)
        return _finalize_repair_decide(
            out, report_dir=report_dir, trace=trace, runtime_state=runtime_state,
        )

    diff_review = actions.execute("inspect_diff", {**action_context, "artifact_dir": str(report_dir / "artifacts")})
    out.diff_review = deps.diff_review

    if diff_review.get("status") != "passed":
        rollback = actions.execute(
            "rollback",
            {**action_context, "artifact_dir": str(report_dir / "artifacts")},
            approval=workspace_action_approval,
        )
        out.applied_fix_result = deps.applied_fix_result
        out.applied_fix_result["skipped_reason"] = "diff review rejected"
        out.applied_fix_result["error"] = "; ".join(diff_review.get("issues") or [])
        out.verification_result = {
            "status": "skipped",
            "provider": "diff_review",
            "mode": "gate",
            "error": "修复 diff 未通过门禁，已拒绝后续验证",
            "diff_review": out.diff_review,
            "rollback": rollback,
        }
        out.result_updates = {
            "status": "error",
            "completion_reason": "diff_review_failed",
            "review_required": True,
            "applied_ai_fixes": out.applied_fix_result,
            "diff_review": out.diff_review,
            "verification": out.verification_result,
        }
        if unisolated_approval is not None:
            out.result_updates["unisolated_approval"] = unisolated_approval
        if isolated_workspace is not None:
            write_workspace_artifacts(isolated_workspace, report_dir)
        return _finalize_repair_decide(
            out, report_dir=report_dir, trace=trace, runtime_state=runtime_state,
        )

    workspace_path = str(
        action_context.get("workspace")
        or (isolated_workspace.root if isolated_workspace is not None else "")
        or (code_roots[0] if code_roots else "")
    )
    problem = deps.request_record if isinstance(deps.request_record, dict) else {}
    effective_verification = build_verification_config_with_reproduce_priority(
        verification_config,
        workspace=workspace_path,
        code_roots=code_roots,
        problem=problem,
    )
    if effective_verification.get("skip_verify"):
        out.verification_result = {
            "status": "skipped",
            "provider": "none",
            "mode": "auto",
            "checks": [],
            "duration_ms": 0,
            "error": "verify skipped by --skip-verify",
        }
        out.result_updates = {
            "applied_ai_fixes": out.applied_fix_result,
            "diff_review": out.diff_review,
            "verification": out.verification_result,
            "status": "success",
            "completion_reason": "verify_skipped",
        }
        if unisolated_approval is not None:
            out.result_updates["unisolated_approval"] = unisolated_approval
        if isolated_workspace is not None:
            write_workspace_artifacts(isolated_workspace, report_dir)
        return _finalize_repair_decide(
            out, report_dir=report_dir, trace=trace, runtime_state=runtime_state,
        )
    deps.verification_config = effective_verification
    max_edit_rounds = resolve_max_repair_edit_rounds(problem)
    edit_round = 0
    status_updates: Dict[str, Any] = {
        "applied_ai_fixes": out.applied_fix_result,
        "diff_review": out.diff_review,
    }
    while True:
        verify_payload = {**action_context, "artifact_dir": str(report_dir / "artifacts")}
        verify_payload["verification"] = dict(effective_verification or {})
        out.verification_result = actions.execute("verify", verify_payload, approval=deps.approval)
        if out.baseline_result is not None:
            out.verification_result["baseline_comparison"] = compare_verification_runs(
                out.baseline_result, out.verification_result
            )
        status_updates["verification"] = out.verification_result
        verify_status = str(out.verification_result.get("status") or "").lower()

        if verify_status == "pending":
            approval = out.verification_result.get("approval")
            if isinstance(approval, dict) and approval.get("status") in {"required", "expired", "invalid"}:
                out.pending_tool_approval = {
                    "tool": "verify",
                    "tool_call_id": approval.get("tool_call_id"),
                    "fingerprint": approval.get("command_fingerprint"),
                    "input": {"verification": effective_verification or {}},
                    "approval": approval,
                }
                status_updates["status"] = "approval_required"
                status_updates["completion_reason"] = "approval_required"
                status_updates["pending_tool_approval"] = out.pending_tool_approval
            else:
                status_updates["status"] = "verification_pending"
                status_updates["completion_reason"] = "verification_pending"
                _attach_verification_candidates(
                    out.verification_result,
                    code_roots=code_roots,
                    verification_config=effective_verification,
                )
            break

        if verify_status == "passed":
            break

        if verify_status in {"failed", "timeout"}:
            failure_class = classify_verification_failure(out.verification_result)
            out.verification_result["failure_class"] = failure_class
            if should_run_repair_edit_loop(failure_class, edit_round, max_edit_rounds):
                edit_out = run_repair_edit_round(
                    deps=deps,
                    round_index=edit_round,
                    verification_result=out.verification_result,
                    failure_class=failure_class,
                    report_dir=report_dir,
                    trace=trace,
                )
                edit_round += 1
                status_updates.setdefault("repair_edit_rounds", []).append(edit_out.to_dict())
                if edit_out.success and isinstance(edit_out.applied_fix_result, dict):
                    out.applied_fix_result = edit_out.applied_fix_result
                    deps.applied_fix_result = edit_out.applied_fix_result
                    deps.changed_files = [
                        str(item.get("file"))
                        for item in (edit_out.applied_fix_result.get("applied") or [])
                        if isinstance(item, dict) and item.get("status") == "applied" and item.get("file")
                    ]
                    action_context["changed_files"] = list(deps.changed_files)
                    status_updates["applied_ai_fixes"] = out.applied_fix_result
                    action_state = getattr(actions, "state", None)
                    if action_state is not None and hasattr(action_state, "transition"):
                        action_state.transition("act", status="running")
                    continue
            action_state = getattr(actions, "state", None)
            if action_state is not None and hasattr(action_state, "transition"):
                action_state.transition("act", status="running")
            rollback = actions.execute(
                "rollback",
                {**action_context, "artifact_dir": str(report_dir / "artifacts")},
                approval=workspace_action_approval,
            )
            out.verification_result["rollback"] = {
                "attempted": True,
                "enabled": True,
                **rollback,
            }
            out.applied_fix_result = deps.applied_fix_result
            out.applied_fix_result["skipped_reason"] = (
                "验证失败后已回滚" if rollback.get("files") else "验证失败但没有可用备份，无法回滚"
            )
            reanalyze = run_reanalyze_on_failure(deps, actions)
            out.verification_result["post_fix_diagnosis"] = reanalyze
            status_updates["status"] = "error"
            status_updates["error"] = out.verification_result.get("error") or "验证失败"
            status_updates["completion_reason"] = "verification_failed"
            status_updates["_feedback_mode"] = (
                "diagnosis_feedback"
                if failure_class in {"test_failure", "reproduce_failure"}
                else "edit_feedback"
            )
            if reanalyze.get("status") not in {"passed", "skipped"}:
                status_updates["error"] = "验证失败且复诊未成功"
                status_updates["completion_reason"] = "post_fix_diagnosis_failed"
            break

        if verify_status == "unavailable" and out.verification_result.get("provider") != "none":
            status_updates["status"] = "error"
            status_updates["error"] = out.verification_result.get("error") or "验证 provider 不可用"
            status_updates["completion_reason"] = "verification_unavailable"
            break

        break

    if out.verification_result.get("status") == "passed":
        if post_fix_diagnosis:
            diagnosis = actions.execute(
                "post_fix_diagnosis", {**action_context, "artifact_dir": str(report_dir / "artifacts")},
            )
            out.verification_result["post_fix_diagnosis"] = diagnosis
            if diagnosis.get("status") not in {"passed", "skipped"}:
                status_updates["status"] = "error"
                status_updates["error"] = "验证通过，但修复后复诊未成功"
                status_updates["completion_reason"] = "post_fix_diagnosis_failed"
        if status_updates.get("status") not in {"error", "review_required"}:
            if isolated_workspace is not None:
                sync = actions.execute("sync_worktree", {**action_context, "artifact_dir": str(report_dir / "artifacts")})
                out.verification_result["worktree_sync"] = sync
            status_updates["status"] = "success"
            status_updates["completion_reason"] = "verification_passed"

    out.result_updates = status_updates
    if unisolated_approval is not None:
        out.result_updates["unisolated_approval"] = unisolated_approval
    if isolated_workspace is not None:
        write_workspace_artifacts(isolated_workspace, report_dir)
    return _finalize_repair_decide(
        out, report_dir=report_dir, trace=trace, runtime_state=runtime_state,
    )


def run_repair_pipeline(
    *,
    result: Dict[str, Any],
    code_roots: List[str],
    report_dir: Path,
    run_id: str,
    verification_config: Optional[Dict[str, Any]],
    llm_adapter: Any,
    backup_original_sources: bool = True,
    uaf_nullptr_guard_policy: Optional[Callable[..., Any]] = None,
    trace: Any = None,
    request_record: Optional[Dict[str, Any]] = None,
    post_fix_diagnosis: bool = True,
    apply_fix_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    tool_executor: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
    runtime_state: Optional[RuntimeState] = None,
    policy: Any = None,
) -> RepairPipelineResult:
    """Apply fixes in an isolated worktree, verify, rollback on failure, sync on pass."""
    out = RepairPipelineResult()
    verification_config = dict(verification_config or {})
    session = result.get("context_session") if isinstance(result.get("context_session"), dict) else {}
    selection = session.get("reproduction_plan") if isinstance(session.get("reproduction_plan"), dict) else verification_config.get("reproduction_plan")
    claim = session.get("verification_claim") if isinstance(session.get("verification_claim"), dict) else verification_config.get("verification_claim")
    if verification_config.get("checks") and isinstance(selection, dict) and selection.get("check_id"):
        try:
            from services.verification_plan import build_verification_plan
            bound_plan = build_verification_plan(claim or {"statement": "execute declared verification"}, verification_config, selection)
            verification_config["check_id"] = str(selection["check_id"])
            verification_config["reproduction_plan"] = dict(selection)
            verification_config["verification_claim"] = bound_plan.claim.to_dict()
            verification_config["plan_fingerprint"] = bound_plan.fingerprint
            session["verification_plan_fingerprint"] = bound_plan.fingerprint
        except (TypeError, ValueError) as exc:
            out.verification_result = {"status": "not_configured", "verification_status": "inconclusive",
                                       "failure_class": "schema_error", "error": str(exc)}
            out.result_updates = {"verification": out.verification_result, "status": "verification_pending",
                                  "completion_reason": "invalid_reproduction_plan"}
            return out
    requested_unisolated = isinstance(verification_config, dict) and verification_config.get("isolate_worktree") is False
    unisolated_approval: Optional[Dict[str, Any]] = None
    if requested_unisolated and isinstance(verification_config, dict) and verification_config.get("allow_unisolated") is True:
        supplied = verification_config.get("unisolated_approval")
        fingerprint = unisolated_workspace_fingerprint(run_id, code_roots)
        checked = validate_approval(
            supplied if isinstance(supplied, dict) else {},
            fingerprint=fingerprint,
            run_id=run_id,
            tool_call_id="unisolated_workspace",
            scope="unisolated_workspace",
        )
        if checked.get("status") == "granted":
            unisolated_approval = consume_approval(
                checked,
                fingerprint=fingerprint,
                run_id=run_id,
                tool_call_id="unisolated_workspace",
                scope="unisolated_workspace",
            )
            if trace is not None:
                trace.emit(
                    "approval.consumed", kind="approval", name="unisolated_workspace",
                    status="success", approval_id=unisolated_approval.get("approval_id"),
                    command_fingerprint=fingerprint,
                )
    isolate_worktree = unisolated_approval is None
    isolated_workspace: Optional[IsolatedCodeWorkspace] = None
    fix_code_roots = list(code_roots)
    fix_result_input = result

    if isolate_worktree:
        try:
            isolated_workspace = prepare_isolated_workspace(str(run_id or uuid.uuid4().hex), code_roots)
            fix_code_roots = isolated_workspace.isolated_code_roots
            fix_result_input = map_result_paths(isolated_workspace, result)
            write_workspace_artifacts(isolated_workspace, report_dir)
        except WorktreeIsolationError as exc:
            out.applied_fix_result = {
                "success": False,
                "applied": [],
                "skipped_reason": "无法创建隔离 worktree",
                "error": str(exc),
            }
            out.verification_result = {
                "status": "unavailable",
                "provider": "worktree",
                "mode": "auto",
                "checks": [],
                "duration_ms": 0,
                "error": str(exc),
            }
            out.result_updates = {
                "applied_ai_fixes": out.applied_fix_result,
                "verification": out.verification_result,
            }
            return out

    state = runtime_state or RuntimeState()
    state.verification_capabilities = list(session.get("verification_capabilities") or [])
    state.verification_claim = dict(session.get("verification_claim") or {})
    state.reproduction_plan = dict(session.get("reproduction_plan") or {})
    state.verification_plan_fingerprint = str(session.get("verification_plan_fingerprint") or verification_config.get("plan_fingerprint") or "")
    if trace is not None:
        trace.stage = "act"
    replay_approval = _tool_approval_granted(verification_config)
    def current_revisions():
        if isolated_workspace is not None:
            return (
                workspace_source_revision(isolated_workspace),
                workspace_revision(isolated_workspace),
            )
        return (
            revision_for_code_roots(fix_code_roots, include_diff=False),
            revision_for_code_roots(fix_code_roots, include_diff=True),
        )

    from tool_system.tool_gateway import RuntimeAuthorization
    nested_authorization = RuntimeAuthorization(
        run_id=str(run_id),
        scope="isolated_worktree" if isolate_worktree else "unisolated_workspace",
        approval_id=str((unisolated_approval or {}).get("approval_id") or "") or None,
    )
    deps = RepairActionDeps(
        result=fix_result_input,
        code_roots=fix_code_roots,
        report_dir=report_dir,
        run_id=run_id,
        verification_config=verification_config,
        llm_adapter=llm_adapter,
        backup_original_sources=backup_original_sources,
        uaf_nullptr_guard_policy=uaf_nullptr_guard_policy,
        request_record=request_record,
        isolated_workspace=isolated_workspace,
        tool_executor=tool_executor,
        tool_authorization=nested_authorization,
        approval=_tool_approval_granted(verification_config),
        policy=policy,
        revision_provider=current_revisions,
        apply_fix_fn=apply_fix_fn,
        trace=trace,
    )
    actions = build_repair_action_executor(state=state, trace=trace, deps=deps, policy=policy)
    source_revision, worktree_revision = current_revisions()
    action_context = {
        "source_revision": source_revision,
        "worktree_revision": worktree_revision,
        "isolated_worktree": isolate_worktree,
        "verification_configured": bool(isinstance(verification_config, dict) and (verification_config.get("command") or verification_config.get("checks"))),
    }
    if unisolated_approval is not None:
        action_context["unisolated_approval"] = unisolated_approval
    workspace_action_approval = unisolated_approval or deps.approval

    # A baseline is opt-in: only an explicit profile request can execute a
    # pre-fix command. This prevents the agent from inventing a verification
    # command when no external runtime is configured.
    if isinstance(verification_config, dict) and verification_config.get("pre_fix_baseline"):
        baseline = actions.execute(
            "verify", {**action_context, "verification": verification_config,
                        "changed_files": [], "artifact_dir": str(report_dir / "artifacts")},
            approval=deps.approval,
        )
        out.baseline_result = baseline
        if baseline.get("status") in {"pending", "verification_pending"}:
            out.result_updates = {"status": "verification_pending",
                                  "completion_reason": "pre_fix_baseline_pending",
                                  "baseline": baseline}
            return out
        if baseline.get("status") not in {"passed", "failed", "timeout", "not_triggered", "unavailable"}:
            out.result_updates = {"status": "error", "completion_reason": "pre_fix_baseline_invalid",
                                  "baseline": baseline}
            return out

    apply_fix_started = time.perf_counter()
    try:
        if isolated_workspace is not None or not isolate_worktree:
            out.applied_fix_result = actions.execute(
                "apply_patch",
                {**action_context, "code_roots": fix_code_roots, "report_dir": str(report_dir), "artifact_dir": str(report_dir / "artifacts")},
                approval=workspace_action_approval,
            )
    except PermissionError as exc:
        if "approval" in str(exc).lower():
            out.pending_tool_approval = {
                "tool": "fix_code_applier",
                "error": str(exc),
                "input": {"code_roots": fix_code_roots},
            }
            out.result_updates = {
                "status": "approval_required",
                "completion_reason": "approval_required",
                "pending_tool_approval": out.pending_tool_approval,
            }
            return out
        raise
    out.apply_fix_duration_ms = int(round((time.perf_counter() - apply_fix_started) * 1000))
    out.isolated_workspace = isolated_workspace
    out.applied_fix_result = deps.applied_fix_result
    return _continue_repair_after_apply_patch(
        out,
        deps=deps,
        actions=actions,
        action_context=action_context,
        workspace_action_approval=workspace_action_approval,
        verification_config=verification_config,
        code_roots=fix_code_roots,
        isolated_workspace=isolated_workspace,
        unisolated_approval=unisolated_approval,
        report_dir=report_dir,
        post_fix_diagnosis=post_fix_diagnosis,
        trace=trace,
        runtime_state=runtime_state,
    )


def resume_tool_approval_from_report(
    *,
    report_dir: Path,
    tool_name: str,
    pending: Dict[str, Any],
    approval: Dict[str, Any],
    trace: Any = None,
    run_id: Optional[str] = None,
    tool_executor: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
    runtime_state: Optional[Dict[str, Any]] = None,
    policy: Any = None,
    llm_adapter: Any = None,
) -> RepairPipelineResult:
    """Resume a pending tool approval through RuntimeActionExecutor."""
    action_name = pending_tool_action_name(tool_name)
    if not action_name:
        raise ValueError(f"unsupported pending tool approval: {tool_name}")

    out = RepairPipelineResult()
    report_dir = Path(report_dir).expanduser().resolve()
    request_record = _load_json_object(report_dir / "00_run_request.json")
    summary = _load_json_object(report_dir / "00_run_summary.json")
    workspace_payload = _load_json_object(report_dir / "09_ai_fix_workspace.json")
    replay_run_id = str(run_id or approval.get("run_id") or request_record.get("run_id") or "replay")
    isolated_workspace = isolated_workspace_from_dict(workspace_payload)
    pending_input = pending.get("input") if isinstance(pending.get("input"), dict) else {}
    replay_code_roots = list(
        pending_input.get("code_roots")
        or (isolated_workspace.isolated_code_roots if isolated_workspace is not None else [])
        or request_record.get("code_roots")
        or []
    )
    verification_config = pending_input.get("verification")
    if not isinstance(verification_config, dict):
        verification_config = request_record.get("verification") if isinstance(request_record.get("verification"), dict) else {}
    replay_state = RuntimeState.from_dict(runtime_state or summary.get("runtime_state") or {})

    from tool_system.tool_gateway import RuntimeAuthorization

    scope = str(approval.get("scope") or "single_tool")
    nested_authorization = RuntimeAuthorization(
        run_id=replay_run_id,
        scope=scope,
        approval_id=approval.get("approval_id"),
    )

    def current_revisions():
        if isolated_workspace is not None:
            return (
                workspace_source_revision(isolated_workspace),
                workspace_revision(isolated_workspace),
            )
        return (
            revision_for_code_roots(replay_code_roots, include_diff=False),
            revision_for_code_roots(replay_code_roots, include_diff=True),
        )

    deps = RepairActionDeps(
        result=_load_repair_analysis_result(report_dir),
        code_roots=replay_code_roots,
        report_dir=report_dir,
        run_id=replay_run_id,
        verification_config=verification_config,
        llm_adapter=llm_adapter,
        request_record=request_record,
        isolated_workspace=isolated_workspace,
        tool_executor=tool_executor,
        tool_authorization=nested_authorization,
        approval=approval,
        policy=policy,
        revision_provider=current_revisions,
        trace=trace,
    )
    actions = build_repair_action_executor(state=replay_state, trace=trace, deps=deps, policy=policy)
    source_revision, worktree_revision = current_revisions()
    action_context = {
        "source_revision": source_revision,
        "worktree_revision": worktree_revision,
        "isolated_worktree": isolated_workspace is not None,
        "verification_configured": bool(isinstance(verification_config, dict) and (verification_config.get("command") or verification_config.get("checks"))),
        "workspace": str(isolated_workspace.workspace_root if isolated_workspace is not None else (replay_code_roots[0] if replay_code_roots else report_dir)),
        "changed_files": deps.changed_files,
    }
    artifact_dir = str(report_dir / "artifacts")
    action_payload = {
        **action_context,
        **pending_input,
        "code_roots": replay_code_roots,
        "report_dir": str(report_dir),
        "artifact_dir": artifact_dir,
        "tool_call_id": str(pending.get("tool_call_id") or approval.get("tool_call_id") or f"tc_{uuid.uuid4().hex[:16]}"),
    }

    try:
        if action_name == "apply_patch":
            out.applied_fix_result = actions.execute("apply_patch", action_payload, approval=approval)
            out.isolated_workspace = isolated_workspace
            pipeline = _continue_repair_after_apply_patch(
                out,
                deps=deps,
                actions=actions,
                action_context=action_context,
                workspace_action_approval=approval,
                verification_config=verification_config,
                code_roots=replay_code_roots,
                isolated_workspace=isolated_workspace,
                unisolated_approval=None,
                report_dir=report_dir,
            )
        else:
            tool_out = actions.execute(
                action_name,
                action_payload,
                approval=approval,
                approval_binding=_approval_binding_for_action(action_name, approval, run_id=replay_run_id),
            )
            out.verification_result = tool_out
            out.result_updates = {
                "verification": tool_out,
                "status": "success" if tool_out.get("status") == "passed" else "error",
            }
            pipeline = out
    except PermissionError as exc:
        if "approval" in str(exc).lower():
            out.pending_tool_approval = {
                "tool": tool_name,
                "error": str(exc),
                "input": dict(pending_input),
                "approval": approval,
            }
            out.result_updates = {
                "status": "approval_required",
                "completion_reason": "approval_required",
                "pending_tool_approval": out.pending_tool_approval,
            }
            out.runtime_state = replay_state.to_dict()
            _persist_verification_resume(report_dir, out, trace)
            return out
        raise

    pipeline.runtime_state = replay_state.to_dict()
    _persist_verification_resume(report_dir, pipeline, trace)
    if action_name == "apply_patch" and pipeline.applied_fix_result:
        _write_json_object(report_dir / "08_apply_ai_fixes.json", pipeline.applied_fix_result)
    return pipeline


def resume_verification_from_report(
    *,
    report_dir: Path,
    verification_config: Dict[str, Any],
    trace: Any = None,
    run_id: Optional[str] = None,
) -> RepairPipelineResult:
    """Resume verify stage from an existing report directory (replay v2/v3)."""
    out = RepairPipelineResult()
    replay_approval = _tool_approval_granted(verification_config)
    applied_path = report_dir / "08_apply_ai_fixes.json"
    workspace_path = report_dir / "09_ai_fix_workspace.json"
    if not applied_path.is_file():
        out.verification_result = {"status": "skipped", "error": "no applied fixes artifact"}
        out.result_updates = {"verification": out.verification_result}
        return out
    applied = json.loads(applied_path.read_text(encoding="utf-8"))
    out.applied_fix_result = applied if isinstance(applied, dict) else {}
    workspace_payload: Dict[str, Any] = {}
    if workspace_path.is_file():
        loaded_workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
        workspace_payload = loaded_workspace if isinstance(loaded_workspace, dict) else {}
    out.isolated_workspace = isolated_workspace_from_dict(workspace_payload)
    request_record: Dict[str, Any] = {}
    request_path = report_dir / "00_run_request.json"
    if request_path.is_file():
        loaded_request = json.loads(request_path.read_text(encoding="utf-8"))
        request_record = loaded_request if isinstance(loaded_request, dict) else {}
    replay_run_id = str(run_id or (replay_approval or {}).get("run_id") or request_record.get("run_id") or "replay")
    replay_code_roots = (
        list(out.isolated_workspace.isolated_code_roots)
        if out.isolated_workspace is not None
        else list(request_record.get("code_roots") or [])
    )
    deps = RepairActionDeps(
        result={},
        code_roots=replay_code_roots,
        report_dir=report_dir,
        run_id=replay_run_id,
        verification_config=verification_config,
        applied_fix_result=out.applied_fix_result,
        changed_files=[
            str(item.get("file"))
            for item in (out.applied_fix_result or {}).get("applied", [])
            if isinstance(item, dict) and item.get("file")
        ],
        approval=replay_approval,
        isolated_workspace=out.isolated_workspace,
        request_record=request_record,
        trace=trace,
    )
    if not deps.changed_files:
        out.verification_result = {"status": "skipped", "error": "no changed files to verify"}
        out.result_updates = {"verification": out.verification_result}
        return out
    workspace_dir = str(verification_config.get("workspace") or "")
    if not workspace_dir:
        workspace_dir = str(workspace_payload.get("workspace_root") or "")
    summary_payload = _load_json_object(report_dir / "00_run_summary.json")
    persisted_state = summary_payload.get("runtime_state") or request_record.get("runtime_state") or {}
    try:
        replay_state = RuntimeState.from_dict(persisted_state) if isinstance(persisted_state, dict) and persisted_state else RuntimeState()
    except ValueError:
        replay_state = RuntimeState()
    if out.isolated_workspace is not None:
        deps.revision_provider = lambda: (
            workspace_source_revision(out.isolated_workspace),
            workspace_revision(out.isolated_workspace),
        )
    else:
        deps.revision_provider = lambda: (
            revision_for_code_roots(deps.code_roots, include_diff=False),
            revision_for_code_roots(deps.code_roots, include_diff=True),
        )
    actions = build_repair_action_executor(state=replay_state, trace=trace, deps=deps, policy=None)
    out.verification_result = actions.execute(
        "verify",
        {
            "workspace": workspace_dir or str(report_dir),
            "changed_files": deps.changed_files,
            "tool_call_id": str((replay_approval or {}).get("tool_call_id") or "verification"),
            "verification_configured": bool(verification_config.get("command") or verification_config.get("checks")),
            "artifact_dir": str(report_dir / "artifacts"),
        },
        approval=replay_approval,
    )
    out.result_updates = {
        "applied_ai_fixes": out.applied_fix_result,
        "verification": out.verification_result,
        "baseline": out.baseline_result,
    }
    if out.verification_result.get("status") == "pending":
        approval = out.verification_result.get("approval")
        if isinstance(approval, dict) and approval.get("status") in {"required", "expired", "invalid"}:
            out.pending_tool_approval = {
                "tool": "verify", "tool_call_id": approval.get("tool_call_id"),
                "fingerprint": approval.get("command_fingerprint"),
                "input": {"verification": verification_config}, "approval": approval,
            }
            out.result_updates.update(
                status="approval_required", completion_reason="approval_required",
                pending_tool_approval=out.pending_tool_approval,
            )
        else:
            _attach_verification_candidates(
                out.verification_result,
                code_roots=code_roots,
                verification_config=verification_config,
            )
            out.result_updates.update(status="verification_pending", completion_reason="verification_pending")
    elif out.verification_result.get("status") in {"failed", "timeout"}:
        rollback = actions.execute("rollback", {"artifact_dir": str(report_dir / "artifacts")}, approval=replay_approval)
        out.verification_result["rollback"] = {"attempted": True, **rollback}
        diagnosis = run_reanalyze_on_failure(deps, actions)
        out.verification_result["post_fix_diagnosis"] = diagnosis
        out.result_updates.update(
            status="error", completion_reason="verification_failed",
            error=out.verification_result.get("error") or "验证失败",
        )
        if diagnosis.get("status") not in {"passed", "skipped"}:
            out.result_updates.update(
                completion_reason="post_fix_diagnosis_failed",
                error="验证失败且复诊未成功",
            )
    elif out.verification_result.get("status") == "passed":
        diagnosis = actions.execute("post_fix_diagnosis", {"artifact_dir": str(report_dir / "artifacts")})
        out.verification_result["post_fix_diagnosis"] = diagnosis
        if diagnosis.get("status") not in {"passed", "skipped"}:
            out.result_updates.update(
                status="error", completion_reason="post_fix_diagnosis_failed",
                error="验证通过，但修复后复诊未成功",
            )
        else:
            if out.isolated_workspace is not None:
                sync = actions.execute("sync_worktree", {"artifact_dir": str(report_dir / "artifacts")})
                out.verification_result["worktree_sync"] = sync
            out.result_updates.update(status="success", completion_reason="verification_passed")
    terminal_status = str(out.result_updates.get("status") or "")
    if terminal_status not in {"verification_pending", "approval_required"}:
        replay_state.transition(
            "decide",
            status="completed" if terminal_status == "success" else "error",
            reason=str(out.result_updates.get("completion_reason") or "") or None,
        )
        replay_state.checkpoint(
            state={
                "verification_status": out.verification_result.get("status"),
                "completion_reason": out.result_updates.get("completion_reason"),
            },
            status="completed" if terminal_status == "success" else "error",
        )
    out.runtime_state = replay_state.to_dict()
    _persist_verification_resume(report_dir, out, trace)
    return out
