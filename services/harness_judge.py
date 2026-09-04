"""Deterministic judge that requires executable feedback for strong claims."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class JudgeResult:
    verdict: str
    confidence: str
    gates: Dict[str, bool] = field(default_factory=dict)
    questions: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"schema_version": 1, **asdict(self)}


def judge_run(
    result: Dict[str, Any],
    *,
    verification_status: Optional[str] = None,
) -> JudgeResult:
    """Judge persisted runtime evidence; never invokes an LLM or mutates the run."""
    value = result if isinstance(result, dict) else {}
    metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
    session = value.get("context_session") if isinstance(value.get("context_session"), dict) else {}
    verification = value.get("verification") if isinstance(value.get("verification"), dict) else {}
    status = str(verification_status or verification.get("status") or "skipped").lower()
    verification_level = str(verification.get("verification_level") or "L0").upper()
    claim = verification.get("claim") if isinstance(verification.get("claim"), dict) else {}
    session_claim = session.get("verification_claim") if isinstance(session.get("verification_claim"), dict) else {}
    minimum_level = str(claim.get("minimum_level") or session_claim.get("minimum_level") or "L1").upper()
    levels = {"L0": 0, "L1": 1, "L2": 2, "L3": 3, "L4": 4}
    applied = value.get("applied_ai_fixes") if isinstance(value.get("applied_ai_fixes"), dict) else {}
    repair_attempted = bool(applied.get("success") or applied.get("applied"))
    termination = str(session.get("termination_reason") or value.get("termination_reason") or "")
    structured = metadata.get("structured_analysis") if isinstance(metadata.get("structured_analysis"), dict) else {}
    diagnosis = value.get("crash_diagnosis") if isinstance(value.get("crash_diagnosis"), dict) else {}
    diff_review = value.get("diff_review") if isinstance(value.get("diff_review"), dict) else metadata.get("diff_review")
    diff_review = diff_review if isinstance(diff_review, dict) else {}
    provider = str(verification.get("provider") or "").lower()
    kind = str(verification.get("kind") or verification.get("mode") or "").lower()
    l4_provider = verification_level != "L4" or kind == "integration" or provider in {"device_runner", "integration"}
    same_plan_crash = bool(
        status == "contradicted"
        and isinstance(verification.get("baseline_comparison"), dict)
        and verification["baseline_comparison"].get("same_plan")
    )

    gates = {
        "analysis_completed": termination not in {"invalid_schema", "llm_error", "llm_budget_exhausted"},
        "structured_output": bool(structured) or not value.get("analysis"),
        "diagnosis_evidence": bool(diagnosis),
        "diff_authorized": str(diff_review.get("status") or "") == "passed" if repair_attempted else True,
        "verification_passed": (
            status in {"passed", "compile_verified", "native_verified", "integration_verified", "strongly_supported"}
            and levels.get(verification_level, 0) >= levels.get(minimum_level, 1)
            and l4_provider
        ) if repair_attempted else True,
    }
    questions: List[str] = []
    reasons: List[str] = []
    if not gates["analysis_completed"]:
        questions.append("Can the analysis be rerun with a valid schema and available LLM budget?")
        reasons.append(f"analysis_termination:{termination or 'unknown'}")
    if not gates["structured_output"]:
        questions.append("Does the final analysis satisfy the structured output contract?")
        reasons.append("missing_structured_analysis")
    if not gates["diagnosis_evidence"]:
        questions.append("Which deterministic crash evidence supports the conclusion?")
        reasons.append("missing_deterministic_diagnosis")
    if not gates["diff_authorized"]:
        questions.append("Are all changed files and functions within the authorized repair scope?")
        reasons.append("unauthorized_diff")
    if not gates["verification_passed"] and status not in {"not_configured", "configured_but_unavailable", "not_triggered", "inconclusive", "unavailable", "skipped", "pending"}:
        questions.append("Which executable check proves the proposed repair works?")
        reasons.append(f"verification:{status}")

    if all(gates.values()):
        verdict = "accept"
        confidence = "high" if repair_attempted and verification_level in {"L2", "L3", "L4"} else "medium"
    elif repair_attempted and status in {"pending", "unavailable", "skipped", "not_configured", "configured_but_unavailable", "not_triggered", "harness_invalid", "inconclusive", ""}:
        verdict = "pending"
        confidence = "low"
    elif same_plan_crash:
        verdict = "reject"
        confidence = "high"
    else:
        verdict = "reject"
        confidence = "low"
    return JudgeResult(verdict, confidence, gates, questions, reasons)
