"""Comparison helpers for reproducible pre-fix/post-fix verification."""
from __future__ import annotations

from typing import Any, Dict


def compare_verification_runs(baseline: Dict[str, Any], post_fix: Dict[str, Any]) -> Dict[str, Any]:
    before = dict(baseline or {})
    after = dict(post_fix or {})
    before_count = int(before.get("crash_count") or 0)
    after_count = int(after.get("crash_count") or 0)
    before_iterations = int(before.get("iterations") or 0)
    after_iterations = int(after.get("iterations") or 0)
    before_rate = before.get("crash_rate")
    after_rate = after.get("crash_rate")
    if before_rate is None and before_iterations:
        before_rate = before_count / float(before_iterations)
    if after_rate is None and after_iterations:
        after_rate = after_count / float(after_iterations)
    same_plan = before.get("plan_fingerprint") == after.get("plan_fingerprint") if before.get("plan_fingerprint") else False
    level = str(after.get("verification_level") or before.get("verification_level") or "L3").upper()
    verified_status = {
        "L2": "compile_verified", "L3": "native_verified", "L4": "integration_verified",
    }.get(level, "strongly_supported")
    if same_plan and before_count > 0 and after_count == 0:
        final_status = verified_status
    elif same_plan and before_count > 0 and after_count > 0:
        final_status = "contradicted"
    elif before_count == 0:
        final_status = "harness_invalid" if before.get("target_path_reached") is False else "not_triggered"
    else:
        final_status = "inconclusive"
    return {
        "same_plan": same_plan,
        "baseline_status": before.get("status") or "unknown",
        "post_fix_status": after.get("status") or "unknown",
        "crash_count_before": before_count,
        "crash_count_after": after_count,
        "crash_rate_before": before_rate,
        "crash_rate_after": after_rate,
        "crash_rate_delta": (after_rate - before_rate) if before_rate is not None and after_rate is not None else None,
        "stack_signature_match": bool(after.get("stack_signature_match", False)),
        "status": final_status,
        "verification_level": level,
        "environment_same": bool(before.get("environment_fingerprint") and before.get("environment_fingerprint") == after.get("environment_fingerprint")),
    }
