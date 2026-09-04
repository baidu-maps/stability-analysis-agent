"""Stable, model-independent evaluation result schema."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from services.diff_review import review_changed_files


@dataclass
class EvaluationResult:
    case_id: str
    diagnosis: Dict[str, Any] = field(default_factory=dict)
    repair: Dict[str, Any] = field(default_factory=dict)
    runtime: Dict[str, Any] = field(default_factory=dict)
    judge: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _trace_metrics(metadata: Dict[str, Any]) -> Dict[str, Any]:
    trace = metadata.get("runtime_trace") if isinstance(metadata, dict) else None
    events: List[Dict[str, Any]] = []
    if isinstance(trace, dict):
        raw = trace.get("events")
        if isinstance(raw, list):
            events = [x for x in raw if isinstance(x, dict)]
    budget = trace.get("budget") if isinstance(trace, dict) and isinstance(trace.get("budget"), dict) else {}
    invalid_context = 0
    context_requests = 0
    policy_denials = 0
    tool_successes = 0
    tool_failures = 0
    rollback_triggered = False
    malformed_outputs = 0
    estimated_cost = budget.get("estimated_cost")
    token_usage = budget.get("token_usage") if isinstance(budget.get("token_usage"), dict) else {}
    for event in events:
        name = str(event.get("event") or "")
        if name == "agent.context_requests_parsed":
            context_requests += int(event.get("request_count") or 0)
            invalid_context += int(event.get("invalid_count") or 0)
        if name == "tool.policy" and event.get("status") == "denied":
            policy_denials += 1
        if event.get("kind") in {"tool", "action"} and name.endswith((".finished", ".success", ".failed")):
            if event.get("status") == "success":
                tool_successes += 1
            elif event.get("status") in {"failed", "error"}:
                tool_failures += 1
        if (event.get("name") == "rollback" or event.get("stage") == "rollback") and event.get("status") in {"success", "completed"}:
            rollback_triggered = True
        if name == "schema_violation" or event.get("malformed_output"):
            malformed_outputs += 1
    total_tool_outcomes = tool_successes + tool_failures
    return {
        "tool_calls": int(budget.get("tool_calls") or 0),
        "llm_calls": int(budget.get("llm_calls") or 0),
        "trace_event_count": len(events),
        "context_request_count": context_requests,
        "invalid_context_requests": invalid_context,
        "context_request_valid_rate": (
            float(context_requests) / float(context_requests + invalid_context)
            if context_requests + invalid_context else 1.0
        ),
        "policy_denials": policy_denials,
        "tool_successes": tool_successes,
        "tool_failures": tool_failures,
        "tool_success_rate": float(tool_successes) / float(total_tool_outcomes) if total_tool_outcomes else 1.0,
        "rollback_triggered": rollback_triggered,
        "malformed_output_count": malformed_outputs,
        "token_usage": token_usage,
        "estimated_cost": estimated_cost,
    }


def evaluate_case(
    case_id: str,
    *,
    result: Dict[str, Any],
    allowed_files: Any = None,
    expected_category: str = "",
    expected_file: str = "",
    expected_function: str = "",
    expected_fault_mode: str = "",
    expected_decision: str = "",
    expected_judge_verdict: str = "",
    max_invalid_context_requests: Optional[int] = None,
    duration_ms: int = 0,
) -> EvaluationResult:
    """Compute model-independent regression signals from a run result."""
    applied = result.get("applied_ai_fixes") if isinstance(result, dict) else {}
    applied = applied if isinstance(applied, dict) else {}
    files = [
        str(x.get("file"))
        for x in applied.get("applied", [])
        if isinstance(x, dict) and x.get("file")
    ]
    diagnosis_payload = result.get("crash_diagnosis") if isinstance(result.get("crash_diagnosis"), dict) else {}
    from services.evidence_ingest import normalize_diagnosis_for_evaluation

    normalized = normalize_diagnosis_for_evaluation(diagnosis_payload)
    category = str(
        normalized.get("category")
        or normalized.get("fault_mode")
        or diagnosis_payload.get("category")
        or diagnosis_payload.get("fault_mode")
        or ""
    )
    fault_mode = str(
        normalized.get("fault_mode")
        or normalized.get("category")
        or diagnosis_payload.get("fault_mode")
        or diagnosis_payload.get("category")
        or ""
    )
    location = str(
        normalized.get("file")
        or normalized.get("source_file")
        or diagnosis_payload.get("file")
        or diagnosis_payload.get("source_file")
        or ""
    )
    function_name = str(
        normalized.get("function")
        or normalized.get("function_name")
        or diagnosis_payload.get("function")
        or diagnosis_payload.get("function_name")
        or ""
    )
    expected_mode = expected_fault_mode or expected_category
    verification = result.get("verification") if isinstance(result.get("verification"), dict) else {}
    persisted_review = result.get("diff_review") if isinstance(result.get("diff_review"), dict) else {}
    diff_review = review_changed_files(files, allowed_files or [])
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    evidence_items = metadata.get("evidence_items") if isinstance(metadata.get("evidence_items"), list) else []
    evidence_sources = sorted({str(item.get("source")) for item in evidence_items if isinstance(item, dict) and item.get("source")})
    evidence_reference_count = sum(
        len(item.get("references") or []) for item in evidence_items
        if isinstance(item, dict) and isinstance(item.get("references"), (list, tuple))
    )
    missing_evidence = normalized.get("missing_evidence")
    if not isinstance(missing_evidence, list):
        missing_evidence = diagnosis_payload.get("missing_evidence")
    if not isinstance(missing_evidence, list):
        missing_evidence = []
    evidence_layers_available = normalized.get("evidence_layers_available")
    evidence_layers_total = normalized.get("evidence_layers_total")
    confidence_ceiling = normalized.get("confidence_ceiling")
    trace_stats = _trace_metrics(metadata)
    if duration_ms > 0:
        trace_stats["duration_ms"] = duration_ms
    post_fix = verification.get("post_fix_diagnosis") if isinstance(verification.get("post_fix_diagnosis"), dict) else {}
    reanalyze = verification.get("reanalyze_diagnosis") if isinstance(verification.get("reanalyze_diagnosis"), dict) else {}
    run_status = str(result.get("status") or "") if isinstance(result, dict) else ""
    from services.decide_scorer import score_repair_decision

    decide = score_repair_decision(
        applied_ai_fixes=applied,
        diff_review=persisted_review or diff_review.to_dict(),
        verification=verification,
        post_fix_diagnosis=post_fix,
        run_status=run_status,
        crash_diagnosis=diagnosis_payload,
        structured_analysis=metadata.get("structured_analysis") if isinstance(metadata.get("structured_analysis"), dict) else None,
        runtime_trace=metadata.get("runtime_trace") if isinstance(metadata.get("runtime_trace"), dict) else None,
    )
    persisted_judge = result.get("judge") if isinstance(result.get("judge"), dict) else {}
    observations = metadata.get("observations") if isinstance(metadata.get("observations"), dict) else {}
    session = result.get("context_session") if isinstance(result.get("context_session"), dict) else {}
    graph = session.get("evidence_graph") if isinstance(session.get("evidence_graph"), dict) else {}
    graph_nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    graph_edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
    invalid_count = int(trace_stats.get("invalid_context_requests") or 0)
    expected_decision_norm = str(expected_decision or "").strip().lower()
    expected_judge_norm = str(expected_judge_verdict or "").strip().lower()
    decision_match = (
        "unknown"
        if not expected_decision_norm
        else ("correct" if decide.decision == expected_decision_norm else "incorrect")
    )
    judge_match = (
        "unknown"
        if not expected_judge_norm
        else (
            "correct"
            if str(persisted_judge.get("verdict") or "").lower() == expected_judge_norm
            else "incorrect"
        )
    )
    invalid_context_ok = True
    if max_invalid_context_requests is not None:
        invalid_context_ok = invalid_count <= int(max_invalid_context_requests)
    return EvaluationResult(
        case_id=case_id,
        diagnosis={
            "category": "correct"
            if expected_category and category == expected_category
            else ("unknown" if not expected_category else "incorrect"),
            "fault_mode": "correct"
            if expected_mode and fault_mode == expected_mode
            else ("unknown" if not expected_mode else "incorrect"),
            "location": "correct"
            if expected_file and location == expected_file
            else ("unknown" if not expected_file else "incorrect"),
            "function": "correct"
            if expected_function and function_name == expected_function
            else ("unknown" if not expected_function else "incorrect"),
            "evidence_item_count": len(evidence_items),
            "evidence_sources": evidence_sources,
            "evidence_reference_count": evidence_reference_count,
            "missing_evidence_count": len(missing_evidence),
            "missing_evidence_detected": bool(missing_evidence),
            "evidence_layers_available": evidence_layers_available,
            "evidence_layers_total": evidence_layers_total,
            "confidence_ceiling": confidence_ceiling,
        },
        repair={
            "patch_valid": bool(applied.get("success")),
            "authorized_files": diff_review.status == "passed",
            "diff_review": persisted_review or diff_review.to_dict(),
            "unauthorized_change": bool(
                (persisted_review or diff_review.to_dict()).get("unauthorized_files")
            ),
            "verification": verification.get("status", "skipped"),
            "post_fix_diagnosis_status": post_fix.get("status", "skipped"),
            "reanalyze_diagnosis_status": reanalyze.get("status", "skipped"),
            "run_status": run_status or "unknown",
            "decide": decide.decision,
            "decide_patch_valid": decide.patch_valid,
            "decide_diff_review_passed": decide.diff_review_passed,
            "decide_verification_passed": decide.verification_passed,
            "decide_diagnosis_stable": decide.diagnosis_stable,
            "decide_dimensions": decide.dimensions,
            "judge_verdict": str(persisted_judge.get("verdict") or ""),
            "judge_confidence": str(persisted_judge.get("confidence") or ""),
            "decision_match": decision_match,
            "judge_match": judge_match,
            "repair_edit_rounds": len(result.get("repair_edit_rounds") or [])
            if isinstance(result.get("repair_edit_rounds"), list)
            else 0,
            "auto_verify_used": bool(verification.get("auto_selected")),
            "reproduce_priority_applied": bool(verification.get("reproduce_priority_applied")),
            "verification_level": str(verification.get("verification_level") or "L0"),
            "verification_status": str(verification.get("verification_status") or verification.get("status") or ""),
            "claim_minimum_level": str((session.get("verification_claim") or {}).get("minimum_level") or "L1")
            if isinstance(session.get("verification_claim"), dict) else "L1",
            "investigation_action_count": len(session.get("investigation_actions") or [])
            if isinstance(session.get("investigation_actions"), list) else 0,
            "investigation_plan_count": len((session.get("repo_map") or {}).get("investigation_plan") or [])
            if isinstance(session.get("repo_map"), dict) else 0,
            "evidence_graph_node_count": len(graph_nodes),
            "evidence_graph_edge_count": len(graph_edges),
        },
        runtime={
            **trace_stats,
            "verification_pending": run_status == "verification_pending",
            "approval_required": run_status == "approval_required",
            "engine": str((metadata.get("runtime_trace") or {}).get("engine") or "")
            if isinstance(metadata.get("runtime_trace"), dict) else "",
            "completion_reason": str(result.get("completion_reason") or ""),
            "observation_count": int(observations.get("count") or 0),
            "invalid_context_within_limit": invalid_context_ok,
            "max_invalid_context_requests": max_invalid_context_requests,
        },
        judge=persisted_judge,
    )


def evaluate_report_dir(report_dir: Union[str, Path], *, case_id: Optional[str] = None,
                        expected_category: str = "", expected_file: str = "",
                        expected_function: str = "", expected_fault_mode: str = "",
                        expected_decision: str = "", expected_judge_verdict: str = "",
                        max_invalid_context_requests: Optional[int] = None) -> EvaluationResult:
    """Evaluate persisted artifacts without invoking an LLM or external service."""
    root = Path(report_dir).expanduser().resolve()

    def load(name: str) -> Dict[str, Any]:
        path = root / name
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    summary = load("00_run_summary.json")
    diagnosis = load("04a_crash_diagnosis.json")
    fixes = load("08_apply_ai_fixes.json")
    verification = load("09_verification.json")
    evidence = load("09_evidence.json")
    trace = load("00_runtime_trace.json")
    decide = load("10_decide.json")
    judge_sidecar = load("11_judge.json")
    result = {
        "status": str((summary.get("workflow") or {}).get("status") or summary.get("status") or ""),
        "completion_reason": summary.get("completion_reason"),
        "crash_diagnosis": diagnosis,
        "applied_ai_fixes": fixes,
        "verification": verification,
        "context_session": summary.get("context_session") if isinstance(summary.get("context_session"), dict) else {},
        "diff_review": summary.get("diff_review") if isinstance(summary.get("diff_review"), dict) else {},
        "decide": decide,
        "metadata": {
            "runtime_trace": trace,
            "evidence_items": evidence.get("items", []),
            "evidence_package": evidence.get("evidence_package", {}),
            "observations": summary.get("observations"),
            "context_session": summary.get("context_session"),
        },
        "judge": judge_sidecar or summary.get("judge"),
    }
    if not diagnosis:
        result["crash_diagnosis"] = summary.get("crash_diagnosis", {})
    return evaluate_case(case_id or root.name, result=result,
                         expected_category=expected_category,
                         expected_file=expected_file,
                         expected_function=expected_function,
                         expected_fault_mode=expected_fault_mode,
                         expected_decision=expected_decision,
                         expected_judge_verdict=expected_judge_verdict,
                         max_invalid_context_requests=max_invalid_context_requests)


def evaluate_suite(manifest_path: Union[str, Path], *, report_root: Optional[Union[str, Path]] = None) -> List[EvaluationResult]:
    """Evaluate all cases declared in a machine-readable evaluation manifest."""
    path = Path(manifest_path).expanduser().resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("evaluation manifest must be an object")
    base_root = Path(report_root).expanduser().resolve() if report_root else path.parent
    manifest_root = str(payload.get("report_root") or "").strip()
    if manifest_root:
        base_root = Path(manifest_root).expanduser().resolve()
    results: List[EvaluationResult] = []
    for item in payload.get("cases") or []:
        if not isinstance(item, dict):
            continue
        case_id = str(item.get("id") or item.get("case_id") or "unknown")
        report_subdir = str(item.get("report_subdir") or "").strip()
        report_dir = base_root / report_subdir if report_subdir else base_root / case_id
        if report_dir.is_dir():
            _max_invalid = item.get("max_invalid_context_requests")
            try:
                max_invalid = int(_max_invalid) if _max_invalid is not None else None
            except (TypeError, ValueError):
                max_invalid = None
            results.append(
                evaluate_report_dir(
                    report_dir,
                    case_id=case_id,
                    expected_category=str(item.get("expected_category") or ""),
                    expected_file=str(item.get("expected_file") or ""),
                    expected_function=str(item.get("expected_function") or ""),
                    expected_fault_mode=str(item.get("expected_fault_mode") or item.get("expected_category") or ""),
                    expected_decision=str(item.get("expected_decision") or ""),
                    expected_judge_verdict=str(item.get("expected_judge_verdict") or ""),
                    max_invalid_context_requests=max_invalid,
                )
            )
            continue
        _max_invalid = item.get("max_invalid_context_requests")
        try:
            max_invalid = int(_max_invalid) if _max_invalid is not None else None
        except (TypeError, ValueError):
            max_invalid = None
        results.append(
            evaluate_case(
                case_id,
                result={"status": "missing_report", "metadata": {}},
                expected_category=str(item.get("expected_category") or ""),
                expected_file=str(item.get("expected_file") or ""),
                expected_function=str(item.get("expected_function") or ""),
                expected_fault_mode=str(item.get("expected_fault_mode") or item.get("expected_category") or ""),
                expected_decision=str(item.get("expected_decision") or ""),
                expected_judge_verdict=str(item.get("expected_judge_verdict") or ""),
                max_invalid_context_requests=max_invalid,
            )
        )
    return results


def summarize_matrix(results: List[EvaluationResult]) -> Dict[str, Any]:
    """Aggregate evaluation results into a compact matrix summary."""
    total = len(results)
    diagnosis_ok = sum(1 for item in results if item.diagnosis.get("category") == "correct")
    repair_ok = sum(1 for item in results if item.repair.get("patch_valid"))
    runtime_ok = sum(
        1 for item in results
        if not item.runtime.get("verification_pending") and not item.runtime.get("approval_required")
    )
    evidence_cases = sum(1 for item in results if int(item.diagnosis.get("evidence_item_count") or 0) > 0)
    decide_accept = sum(1 for item in results if item.repair.get("decide") == "accept")
    judge_accept = sum(1 for item in results if item.judge.get("verdict") == "accept")
    decision_matches = [item for item in results if item.repair.get("decision_match") == "correct"]
    judge_matches = [item for item in results if item.repair.get("judge_match") == "correct"]
    decision_expected = sum(1 for item in results if item.repair.get("decision_match") != "unknown")
    judge_expected = sum(1 for item in results if item.repair.get("judge_match") != "unknown")
    context_rates = [
        float((item.repair.get("decide_dimensions") or {}).get("context_loop", {}).get("context_request_valid_rate", 1.0))
        for item in results
        if isinstance(item.repair.get("decide_dimensions"), dict)
    ]
    if not context_rates:
        context_rates = [
            float(item.runtime.get("context_request_valid_rate") or 1.0)
            for item in results
        ]
    return {
        "total_cases": total,
        "diagnosis_correct": diagnosis_ok,
        "repair_valid": repair_ok,
        "runtime_clean": runtime_ok,
        "evidence_coverage": float(evidence_cases) / float(total) if total else 0.0,
        "decide_accept_rate": float(decide_accept) / float(total) if total else 0.0,
        "judge_accept_rate": float(judge_accept) / float(total) if total else 0.0,
        "decision_match_rate": float(len(decision_matches)) / float(decision_expected) if decision_expected else None,
        "judge_match_rate": float(len(judge_matches)) / float(judge_expected) if judge_expected else None,
        "context_loop_valid_rate_avg": sum(context_rates) / len(context_rates) if context_rates else 1.0,
        "cases": [item.to_dict() for item in results],
    }


def write_evaluation_artifact(report_dir: Union[str, Path], result: EvaluationResult) -> Path:
    """Persist evaluation output beside a report directory."""
    root = Path(report_dir).expanduser().resolve()
    path = root / "00_evaluation.json"
    path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
