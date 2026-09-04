"""Canonical failure classification for tool and runtime observations."""
from __future__ import annotations
from typing import Any, Dict

FAILURE_CLASSES = frozenset({"schema_error", "permission_denied", "path_blocked", "stale_file",
    "empty_result", "partial_success", "timeout", "compile_error", "test_failure",
    "reproduce_failure", "user_rejected", "workspace_changed"})

_FALLBACKS = {"stale_file": "read_file", "empty_result": "repo_search", "compile_error": "inspect_build_output",
              "test_failure": "inspect_test_output", "reproduce_failure": "inspect_reproduction_output"}
_RECOVERY_REASONS = {
    "stale_file": "workspace content changed; reread before retrying",
    "compile_error": "inspect compiler diagnostics before editing",
    "test_failure": "inspect failing test output and update the hypothesis",
    "reproduce_failure": "inspect reproducer output and reassess the hypothesis",
    "timeout": "retry with a bounded timeout or a narrower action",
}

def normalize_action_result(result: Any, *, action: str = "") -> Dict[str, Any]:
    value = dict(result) if isinstance(result, dict) else {"error": str(result)}
    status = str(value.get("status") or ("completed" if value.get("success") else "failed")).lower()
    if status in {"success", "passed", "completed"}:
        value.setdefault("status", "completed")
        value.setdefault("retryable", False)
        return value
    if status in {"pending", "approval_required", "blocked"}:
        value.setdefault("retryable", status == "pending")
        return value
    text = " ".join(str(value.get(k) or "") for k in ("error", "summary", "stderr", "failure_class")).lower()
    cls = str(value.get("failure_class") or "").strip().lower()
    if cls not in FAILURE_CLASSES:
        if "stale" in text or "fingerprint" in text: cls = "stale_file"
        elif "permission" in text or "denied" in text: cls = "permission_denied"
        elif "path" in text and "outside" in text: cls = "path_blocked"
        elif "timeout" in text: cls = "timeout"
        elif action in {"run_build", "build"}: cls = "compile_error"
        elif action in {"run_tests", "test"}: cls = "test_failure"
        elif action in {"reproduce_crash", "reproduce"}: cls = "reproduce_failure"
        else: cls = "schema_error" if "schema" in text else "empty_result"
    value.update(status="failed", failure_class=cls,
                 retryable=bool(value.get("retryable", cls in {"timeout", "empty_result"})),
                 fallback_action=value.get("fallback_action") or _FALLBACKS.get(cls),
                 summary=value.get("summary") or value.get("error") or cls)
    value.setdefault("kind", cls)
    value.setdefault("user_visible", cls not in {"empty_result"})
    value.setdefault("tool_visible", True)
    value.setdefault("details", {})
    value.setdefault("recovery", {
        "kind": value.get("fallback_action") or "reassess",
        "reason": _RECOVERY_REASONS.get(cls, "inspect the structured failure before continuing"),
    })
    return value
