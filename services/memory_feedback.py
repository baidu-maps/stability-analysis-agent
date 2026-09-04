"""Governed feedback from executable run outcomes into Crash Engineering Memory."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional


def _pattern_ids(result: Dict[str, Any]) -> List[str]:
    values: List[Any] = []
    for key in ("pattern_hits", "memory_hits"):
        raw = result.get(key)
        if isinstance(raw, list):
            values.extend(raw)
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    raw = metadata.get("pattern_hits")
    if isinstance(raw, list):
        values.extend(raw)
    ids: List[str] = []
    seen = set()
    for value in values:
        pattern_id = value.get("pattern_id") if isinstance(value, dict) else value
        token = str(pattern_id or "").strip()
        if token and token not in seen:
            seen.add(token)
            ids.append(token)
    return ids


def record_verified_feedback(result: Dict[str, Any], *, vector_db_path: Optional[str] = None) -> Dict[str, Any]:
    """Record feedback only after executable verification reaches passed/failed."""
    verification = result.get("verification") if isinstance(result.get("verification"), dict) else {}
    status = str(verification.get("status") or "").strip().lower()
    if status not in {"passed", "failed"}:
        return {"recorded": False, "reason": "verification_not_terminal"}
    ids = _pattern_ids(result)
    if not ids:
        return {"recorded": False, "reason": "no_pattern_ids"}
    feedback_type = "adopted" if status == "passed" else "rejected"
    comment = str(verification.get("error") or verification.get("output") or f"runtime verification {status}")[:2000]
    try:
        from rag.vector_store_config import get_vector_store

        store = get_vector_store(cli_path=vector_db_path)
        for pattern_id in ids:
            store.analyzer.record_feedback(pattern_id, feedback_type, comment)
        return {"recorded": True, "feedback_type": feedback_type, "pattern_ids": ids}
    except Exception as exc:
        return {"recorded": False, "reason": "memory_unavailable", "error": str(exc)}


def record_run_memory(
    result: Dict[str, Any],
    *,
    report_dir: Optional[str] = None,
    vector_db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Pattern feedback plus optional structured case commit after terminal verification."""
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    path = vector_db_path or metadata.get("vector_db_path")
    pattern_feedback = record_verified_feedback(result, vector_db_path=path)
    case_commit: Dict[str, Any] = {"ok": False, "skipped": True, "reason": "verification_not_passed"}
    verification = result.get("verification") if isinstance(result.get("verification"), dict) else {}
    status = str(verification.get("status") or verification.get("verification_status") or "").strip().lower()
    level = str(verification.get("verification_level") or "").upper()
    provenance = verification.get("provenance") if isinstance(verification.get("provenance"), dict) else {}
    diff_review = result.get("diff_review") if isinstance(result.get("diff_review"), dict) else {}
    # Legacy reports only had status=passed.  Keep those readable/committable;
    # new reports must provide an explicit executable level or command.
    executable = status == "passed" and not level or (
        status in {"passed", "compile_verified", "native_verified", "integration_verified"}
        and level in {"L2", "L3", "L4"}
    )
    traceable = bool(provenance) or bool(verification.get("command")) or bool(verification.get("checks")) or status == "passed"
    approved_diff = not result.get("applied_ai_fixes") or str(diff_review.get("status") or "passed") == "passed"
    claim = verification.get("claim") if isinstance(verification.get("claim"), dict) else {}
    session = result.get("context_session") if isinstance(result.get("context_session"), dict) else {}
    session_claim = session.get("verification_claim") if isinstance(session.get("verification_claim"), dict) else {}
    minimum = str(claim.get("minimum_level") or session_claim.get("minimum_level") or "L1").upper()
    levels = {"L0": 0, "L1": 1, "L2": 2, "L3": 3, "L4": 4}
    level_satisfied = levels.get(level, 0) >= levels.get(minimum, 1)
    legacy_passed = status == "passed" and not level
    if not legacy_passed:
        executable = executable and level_satisfied and bool(verification.get("plan_fingerprint"))
        traceable = traceable and bool(provenance) and bool(verification.get("changed_files"))
    audit_path = None
    if executable and traceable and approved_diff and report_dir:
        try:
            from rag.case_writer import commit_from_report_dir, write_commit_audit

            case_commit = commit_from_report_dir(
                Path(report_dir),
                vector_db_path=str(path) if path else None,
            )
            audit_path = str(
                write_commit_audit(
                    Path(report_dir),
                    {"pattern_feedback": pattern_feedback, "case_commit": case_commit},
                )
            )
        except Exception as exc:
            case_commit = {"ok": False, "error": str(exc)}
    elif report_dir and pattern_feedback.get("recorded") and executable and traceable and approved_diff:
        try:
            from rag.case_writer import write_commit_audit

            audit_path = str(
                write_commit_audit(
                    Path(report_dir),
                    {"pattern_feedback": pattern_feedback, "case_commit": case_commit},
                )
            )
        except Exception:
            audit_path = None
    recorded = bool(pattern_feedback.get("recorded")) or bool(case_commit.get("ok"))
    return {
        "recorded": recorded,
        "feedback_type": pattern_feedback.get("feedback_type"),
        "pattern_ids": pattern_feedback.get("pattern_ids") or [],
        "reason": pattern_feedback.get("reason") or case_commit.get("reason"),
        "pattern_feedback": pattern_feedback,
        "case_commit": case_commit,
        "audit_path": audit_path,
    }
