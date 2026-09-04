"""Unified repair decide scoring for runtime and offline evaluation."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Literal, Optional


DecisionLiteral = Literal["accept", "reject", "pending", "partial"]


@dataclass
class DecideScore:
    patch_valid: bool
    diff_review_passed: bool
    verification_passed: bool
    diagnosis_stable: bool
    decision: DecisionLiteral
    reasons: List[str] = field(default_factory=list)
    dimensions: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _patch_valid(applied_ai_fixes: Optional[Dict[str, Any]]) -> tuple[bool, Dict[str, Any], List[str]]:
    reasons: List[str] = []
    payload = applied_ai_fixes if isinstance(applied_ai_fixes, dict) else {}
    if not payload:
        return False, {"applied_count": 0, "skipped_count": 0}, ["no_applied_fixes"]
    edits = payload.get("applied") if isinstance(payload.get("applied"), list) else []
    applied_count = sum(1 for item in edits if isinstance(item, dict) and item.get("status") == "applied")
    skipped_count = sum(1 for item in edits if isinstance(item, dict) and item.get("status") != "applied")
    ok = bool(payload.get("success")) and applied_count > 0
    if payload.get("skipped_reason"):
        reasons.append(str(payload.get("skipped_reason")))
    if payload.get("schema_violations"):
        reasons.append("schema_violation")
        ok = False
    if not ok and not reasons:
        reasons.append("patch_not_applied")
    return ok, {"applied_count": applied_count, "skipped_count": skipped_count}, reasons


def _diff_review_passed(diff_review: Optional[Dict[str, Any]]) -> tuple[bool, Dict[str, Any], List[str]]:
    review = diff_review if isinstance(diff_review, dict) else {}
    status = str(review.get("status") or "")
    ok = status in {"passed", ""} or not review
    reasons: List[str] = []
    if review and status == "failed":
        issues = review.get("issues") or review.get("unauthorized_files") or []
        if issues:
            reasons.append(f"diff_review_failed:{issues[0]}")
        else:
            reasons.append("diff_review_failed")
    return ok, {"status": status or "unknown"}, reasons


def _verification_passed(verification: Optional[Dict[str, Any]]) -> tuple[bool, Dict[str, Any], List[str]]:
    ver = verification if isinstance(verification, dict) else {}
    status = str(ver.get("status") or "")
    reasons: List[str] = []
    if status in {"passed", "skipped", ""}:
        return True, {"status": status or "skipped"}, reasons
    if status == "pending":
        reasons.append("verification_pending")
        return False, {"status": status}, reasons
    if status in {"failed", "error", "timeout", "unavailable"}:
        err = str(ver.get("error") or status)
        reasons.append(f"verification_{status}:{err}")
        return False, {"status": status, "error": err}, reasons
    return False, {"status": status or "unknown"}, ["verification_unknown"]


def _diagnosis_stable(
    post_fix_diagnosis: Optional[Dict[str, Any]],
    *,
    verification: Optional[Dict[str, Any]] = None,
) -> tuple[bool, Dict[str, Any], List[str]]:
    if isinstance(verification, dict):
        nested = verification.get("post_fix_diagnosis")
        if isinstance(nested, dict) and not post_fix_diagnosis:
            post_fix_diagnosis = nested
    diag = post_fix_diagnosis if isinstance(post_fix_diagnosis, dict) else {}
    status = str(diag.get("status") or "")
    reasons: List[str] = []
    regression = diag.get("regression") if isinstance(diag.get("regression"), dict) else {}
    if regression.get("detected"):
        reasons.append(str(regression.get("reason") or "diagnosis_regression"))
        return False, {"status": status or "failed", "regression": regression}, reasons
    if status in {"passed", "skipped", ""}:
        return True, {"status": status or "skipped"}, reasons
    if status:
        reasons.append(f"post_fix_diagnosis_{status}")
    return False, {"status": status or "unknown"}, reasons


def score_repair_decision(
    *,
    applied_ai_fixes: Optional[Dict[str, Any]] = None,
    diff_review: Optional[Dict[str, Any]] = None,
    verification: Optional[Dict[str, Any]] = None,
    post_fix_diagnosis: Optional[Dict[str, Any]] = None,
    run_status: str = "",
    pipeline_skipped: bool = False,
    crash_diagnosis: Optional[Dict[str, Any]] = None,
    structured_analysis: Optional[Dict[str, Any]] = None,
    runtime_trace: Optional[Dict[str, Any]] = None,
) -> DecideScore:
    """Aggregate patch/diff/verify/diagnosis signals into a single decision."""
    status = str(run_status or "")
    has_repair_signals = bool(applied_ai_fixes) or bool(verification)
    patch_ok, patch_dim, patch_reasons = _patch_valid(applied_ai_fixes)
    diff_ok, diff_dim, diff_reasons = _diff_review_passed(diff_review)
    verify_ok, verify_dim, verify_reasons = _verification_passed(verification)
    # A repair claim is never accepted without terminal executable feedback.
    # ``skipped`` is valid only for analyze-only runs.
    if applied_ai_fixes and verify_dim.get("status") in {"", "skipped"}:
        verify_ok = False
        verify_dim = {**verify_dim, "status": "skipped"}
        verify_reasons = list(verify_reasons) + ["verification_required_after_repair"]
    diag_ok, diag_dim, diag_reasons = _diagnosis_stable(
        post_fix_diagnosis, verification=verification,
    )
    reasons = list(patch_reasons + diff_reasons + verify_reasons + diag_reasons)
    dimensions = {
        "patch": patch_dim,
        "diff_review": diff_dim,
        "verification": verify_dim,
        "post_fix_diagnosis": diag_dim,
    }
    diag_category = ""
    if isinstance(crash_diagnosis, dict):
        diag_category = str(
            crash_diagnosis.get("category")
            or crash_diagnosis.get("fault_mode")
            or ""
        ).strip()
    structured_category = ""
    if isinstance(structured_analysis, dict):
        structured_category = str(
            structured_analysis.get("root_cause")
            or structured_analysis.get("category")
            or ""
        ).strip()
    category_match = bool(diag_category and structured_category and diag_category.lower() in structured_category.lower())
    dimensions["diagnosis_category_match"] = {
        "expected": diag_category or "unknown",
        "structured": structured_category or "unknown",
        "matched": category_match,
    }
    context_valid_rate = 1.0
    invalid_context = 0
    context_requests = 0
    if isinstance(runtime_trace, dict):
        for event in runtime_trace.get("events") or []:
            if not isinstance(event, dict):
                continue
            if event.get("event") == "agent.context_requests_parsed":
                context_requests += int(event.get("request_count") or 0)
                invalid_context += int(event.get("invalid_count") or 0)
        total = context_requests + invalid_context
        context_valid_rate = float(context_requests) / float(total) if total else 1.0
    dimensions["context_loop"] = {
        "context_request_valid_rate": context_valid_rate,
        "invalid_context_requests": invalid_context,
        "warn_only": True,
    }
    if invalid_context > 0:
        reasons.append(f"context_loop_invalid_requests:{invalid_context}")

    if not has_repair_signals and not pipeline_skipped:
        if status in {"success", "done", ""}:
            return DecideScore(
                patch_valid=True,
                diff_review_passed=True,
                verification_passed=True,
                diagnosis_stable=True,
                decision="accept",
                reasons=[],
                dimensions={"mode": "analyze_only"},
            )

    if pipeline_skipped:
        decision: DecisionLiteral = "partial"
    elif status in {"verification_pending", "approval_required"} or str(verify_dim.get("status") or "") == "pending":
        decision = "pending"
    elif status in {"error", "failed"} or not (patch_ok and diff_ok and verify_ok and diag_ok):
        decision = "reject"
    elif status in {"success", "done", ""}:
        decision = "accept"
    else:
        decision = "reject"

    return DecideScore(
        patch_valid=patch_ok,
        diff_review_passed=diff_ok,
        verification_passed=verify_ok,
        diagnosis_stable=diag_ok,
        decision=decision,
        reasons=reasons,
        dimensions=dimensions,
    )


def apply_decide_to_result(
    result: Dict[str, Any],
    score: DecideScore,
    *,
    report_dir: Optional[Any] = None,
    trace: Any = None,
) -> None:
    """Write decide score into result metadata and optional artifact."""
    metadata = result.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
        result["metadata"] = metadata
    payload = score.to_dict()
    metadata["decide"] = payload
    result["decide"] = payload
    if report_dir is not None:
        from services.stage_artifacts import save_decide_artifact

        save_decide_artifact(report_dir, payload)
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
