"""Standard action boundary for the repair portion of AgentRuntime."""
from __future__ import annotations

from dataclasses import dataclass
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from services.verification import validate_approval
from tool_system.runtime import RunTrace, RuntimeState, value_hash

ACTION_NAMES = (
    "apply_patch", "inspect_diff", "rollback", "verify",
    "post_fix_diagnosis", "sync_worktree",
    "run_build", "run_tests", "run_static_check", "reproduce_crash",
)

VERIFICATION_ACTION_TOOLS = frozenset({"run_build", "run_tests", "run_static_check", "reproduce_crash"})
READ_ONLY_ACTIONS = frozenset({"inspect_diff", "post_fix_diagnosis", "sync_worktree"})

def _stage_gate(name: str, stage: str) -> Optional[str]:
    stage = str(stage or "").strip().lower()
    if stage in {"diagnose", "evidence_review", "judge"} and name == "apply_patch":
        return f"stage {stage} is read-only"
    if stage == "repair_proposal":
        return "repair_proposal accepts a structured plan; runtime actions are not allowed"
    if stage == "diff_review" and name == "apply_patch":
        return "diff_review is read-only"
    if stage == "verify" and name == "apply_patch":
        return "verify stage cannot modify workspace"
    if stage == "verify" and name not in VERIFICATION_ACTION_TOOLS | {"verify", "inspect_diff"}:
        return f"action {name} is not allowed during verify"
    return None

# Pending tool-approval names that map to registered runtime actions (verify uses its own resume path).
PENDING_TOOL_TO_ACTION = {
    "fix_code_applier": "apply_patch",
    **{name: name for name in VERIFICATION_ACTION_TOOLS},
}


def pending_tool_action_name(tool_name: str) -> Optional[str]:
    """Resolve a pending tool approval name to a runtime action, if supported."""
    key = str(tool_name or "").strip()
    if not key or key == "verify":
        return None
    return PENDING_TOOL_TO_ACTION.get(key)


@dataclass(frozen=True)
class ApprovalBinding:
    run_id: str
    tool_call_id: str
    fingerprint: str
    scope: str = "single_command"
    approval_id: Optional[str] = None

    @classmethod
    def from_approval(cls, approval: Mapping[str, Any], *, run_id: str) -> "ApprovalBinding":
        return cls(
            run_id=str(run_id),
            tool_call_id=str(approval.get("tool_call_id") or ""),
            fingerprint=str(approval.get("command_fingerprint") or ""),
            scope=str(approval.get("scope") or "single_command"),
            approval_id=str(approval.get("approval_id") or "") or None,
        )


def _validate_action_approval(approval: Mapping[str, Any], binding: ApprovalBinding) -> None:
    checked = validate_approval(
        approval,
        fingerprint=binding.fingerprint,
        run_id=binding.run_id,
        tool_call_id=binding.tool_call_id,
        scope=binding.scope,
    )
    if checked.get("status") != "granted":
        reason = str(checked.get("validation_error") or "approval invalid")
        raise PermissionError(f"runtime action approval invalid: {reason}")
    if binding.approval_id and str(approval.get("approval_id") or "") != binding.approval_id:
        raise PermissionError("runtime action approval_id mismatch")


@dataclass(frozen=True)
class RuntimeAction:
    name: str
    handler: Callable[[Dict[str, Any]], Dict[str, Any]]
    requires_approval: bool = False
    idempotent: bool = True
    risk: str = "read_only"
    side_effect: bool = False
    input_schema: Mapping[str, type] = None

    def validate(self, payload: Mapping[str, Any], *, policy: Any = None) -> None:
        if not isinstance(payload, Mapping):
            raise ValueError(f"runtime action '{self.name}' input must be an object")
        for key, expected_type in (self.input_schema or {}).items():
            if key not in payload:
                raise ValueError(f"runtime action '{self.name}' missing input: {key}")
            if not isinstance(payload[key], expected_type):
                raise ValueError(
                    f"runtime action '{self.name}' input '{key}' must be {expected_type.__name__}"
                )
        _validate_common_action_fields(self.name, payload, policy=policy)


def _authorized_roots(payload: Mapping[str, Any], policy: Any = None) -> List[Path]:
    roots: List[Path] = []
    for value in (payload.get("workspace"), payload.get("report_dir"), payload.get("artifact_dir")):
        if isinstance(value, str) and value.strip():
            roots.append(Path(value).expanduser().resolve())
    for key in ("code_roots", "allowed_roots"):
        values = payload.get(key)
        if isinstance(values, (list, tuple)):
            for value in values:
                if isinstance(value, str) and value.strip():
                    roots.append(Path(value).expanduser().resolve())
    if policy is not None:
        for value in getattr(policy, "allowed_roots", ()) or ():
            try:
                roots.append(Path(value).expanduser().resolve())
            except (OSError, ValueError):
                continue
    result: List[Path] = []
    seen = set()
    for root in roots:
        if str(root) not in seen:
            seen.add(str(root))
            result.append(root)
    return result


def _path_under(path: Path, roots: Iterable[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _validate_common_action_fields(name: str, payload: Mapping[str, Any], *, policy: Any = None) -> None:
    """Validate shared action fields before invoking a handler."""
    path_values: Dict[str, List[str]] = {}
    for field_name in ("artifact_dir", "report_dir", "workspace"):
        value = payload.get(field_name)
        if value is not None:
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ValueError(f"runtime action '{name}' field '{field_name}' must be a safe path")
            if any(part == ".." for part in Path(value).parts):
                raise ValueError(f"runtime action '{name}' field '{field_name}' contains unsafe path")
            path_values[field_name] = [value]
    for field_name in ("code_roots", "changed_files", "workspace_paths"):
        value = payload.get(field_name)
        if value is not None:
            if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
                raise ValueError(f"runtime action '{name}' field '{field_name}' must be a string list")
            if any("\x00" in item or any(part == ".." for part in Path(item).parts) for item in value):
                raise ValueError(f"runtime action '{name}' field '{field_name}' contains unsafe path")
            path_values[field_name] = list(value)
    if "command" in payload:
        command = payload.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
            raise ValueError(f"runtime action '{name}' command must be a non-empty argv list")
    for field_name in ("source_revision", "worktree_revision", "tool_call_id", "idempotency_key"):
        if field_name in payload and payload.get(field_name) is not None:
            if not isinstance(payload[field_name], str) or not payload[field_name].strip():
                raise ValueError(f"runtime action '{name}' field '{field_name}' must be a non-empty string")
    if "verification" in payload and payload.get("verification") is not None:
        verification = payload.get("verification")
        if not isinstance(verification, Mapping):
            raise ValueError(f"runtime action '{name}' verification must be an object")
        if "command" in verification:
            command = verification.get("command")
            if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
                raise ValueError(f"runtime action '{name}' verification.command must be a non-empty argv list")
    roots = _authorized_roots(payload, policy)
    if roots:
        workspace = payload.get("workspace")
        workspace_roots: List[Path] = []
        if isinstance(workspace, str) and workspace.strip():
            workspace_roots.append(Path(workspace).expanduser().resolve())
        else:
            values = payload.get("code_roots")
            if isinstance(values, (list, tuple)):
                workspace_roots.extend(
                    Path(value).expanduser().resolve()
                    for value in values
                    if isinstance(value, str) and value.strip()
                )
        for field_name, values in path_values.items():
            field_roots = workspace_roots if field_name in {"changed_files", "workspace_paths"} else roots
            for raw in values:
                candidate = Path(raw).expanduser()
                if not candidate.is_absolute() and isinstance(workspace, str) and workspace.strip():
                    candidate = Path(workspace) / candidate
                try:
                    resolved = candidate.resolve()
                except (OSError, ValueError) as exc:
                    raise ValueError(f"runtime action '{name}' field '{field_name}' has invalid path") from exc
                if not _path_under(resolved, field_roots or roots):
                    raise ValueError(
                        f"runtime action '{name}' field '{field_name}' is outside authorized roots: {raw}"
                    )


class RuntimeActionExecutor:
    """Validate, authorize, execute and checkpoint one named runtime action."""

    def __init__(self, *, state: RuntimeState, trace: Optional[RunTrace] = None,
                 policy: Any = None,
                 revision_provider: Optional[Callable[[], Tuple[Optional[str], Optional[str]]]] = None,
                 security_analyzer: Any = None, file_context_tracker: Any = None):
        self.state = state
        self.trace = trace
        self.policy = policy
        self.revision_provider = revision_provider
        self.security_analyzer = security_analyzer
        self.file_context_tracker = file_context_tracker
        self._actions: Dict[str, RuntimeAction] = {}
        self._before_hooks: List[Callable[[str, Dict[str, Any]], Any]] = []
        self._after_hooks: List[Callable[[str, Dict[str, Any], Dict[str, Any]], Any]] = []
        self._failure_hooks: List[Callable[[str, Dict[str, Any], BaseException], Any]] = []

    def register_before_action_hook(self, callback: Callable[[str, Dict[str, Any]], Any]) -> None:
        if callable(callback):
            self._before_hooks.append(callback)

    def register_after_action_hook(self, callback: Callable[[str, Dict[str, Any], Dict[str, Any]], Any]) -> None:
        if callable(callback):
            self._after_hooks.append(callback)

    def register_failure_hook(self, callback: Callable[[str, Dict[str, Any], BaseException], Any]) -> None:
        if callable(callback):
            self._failure_hooks.append(callback)

    @staticmethod
    def _run_hooks(hooks: Iterable[Callable[..., Any]], *args: Any) -> None:
        for callback in list(hooks):
            try:
                callback(*args)
            except Exception:
                # Observability hooks are best-effort and must not alter action semantics.
                continue

    def register(self, action: RuntimeAction) -> None:
        if action.name not in ACTION_NAMES:
            raise ValueError(f"unsupported runtime action: {action.name}")
        self._actions[action.name] = action

    def execute(self, name: str, input_data: Mapping[str, Any], *,
                approval: Optional[Mapping[str, Any]] = None,
                approval_binding: Optional[ApprovalBinding] = None) -> Dict[str, Any]:
        action = self._actions.get(name)
        if action is None:
            raise ValueError(f"runtime action is not registered: {name}")
        payload = dict(input_data or {})
        if not isinstance(payload, dict):
            raise ValueError("runtime action input must be an object")
        try:
            action.validate(payload, policy=self.policy)
        except Exception as exc:
            self._run_hooks(self._failure_hooks, name, dict(payload), exc)
            tool_call_id = str(payload.get("tool_call_id") or f"tc_{uuid.uuid4().hex[:16]}")
            reason = str(exc)
            if self.trace:
                self.trace.emit(
                    "action.schema_violation", kind="action", name=name,
                    status="failed", tool_call_id=tool_call_id,
                    input_hash=value_hash(payload), error=reason,
                    termination_reason="schema_violation",
                )
            self.state.checkpoint(
                state={"action": name, "error": reason, "schema_violation": True},
                status="error", idempotency_key=str(payload.get("idempotency_key") or value_hash(payload)),
                input_artifact=payload.get("input_artifact"),
                tool_call_id=tool_call_id,
            )
            raise
        gate_error = _stage_gate(name, getattr(self.state, "stage", ""))
        if gate_error:
            self._run_hooks(self._failure_hooks, name, dict(payload), PermissionError(gate_error))
            if self.trace:
                self.trace.emit("stage.violation", kind="policy", name=name, status="denied", error=gate_error)
            self.state.checkpoint(state={"action": name, "stage_violation": gate_error}, status="blocked",
                                  idempotency_key=str(payload.get("idempotency_key") or value_hash(payload)))
            raise PermissionError(gate_error)
        approval_granted = isinstance(approval, Mapping) and approval.get("status") == "granted"
        if action.requires_approval and not approval_granted:
            raise PermissionError(f"runtime action requires approval: {name}")
        if action.requires_approval and approval_granted:
            if approval_binding is None:
                raise PermissionError(f"runtime action requires approval_binding: {name}")
            _validate_action_approval(approval, approval_binding)
        if self.security_analyzer is not None:
            workspace = str(payload.get("workspace") or (payload.get("code_roots") or [""])[0])
            security = self.security_analyzer.analyze_action(
                {"name": name, **payload}, workspace,
                authorization=approval if isinstance(approval, Mapping) else None,
            )
            if self.trace:
                self.trace.emit("action.security", kind="policy", name=name,
                                status="allowed" if security.allowed else "denied",
                                decision=security.to_dict())
            if not security.allowed:
                self._run_hooks(self._failure_hooks, name, dict(payload), PermissionError(security.reason))
                self.state.checkpoint(state={"action": name, "security": security.to_dict()}, status="blocked",
                                      idempotency_key=str(payload.get("idempotency_key") or value_hash(payload)),
                                      tool_call_id=str(payload.get("tool_call_id") or ""))
                raise PermissionError(f"runtime action blocked: {security.reason}")
        if self.policy is not None and hasattr(self.policy, "check_permission"):
            permission = "write" if name == "apply_patch" else ("verify" if name in VERIFICATION_ACTION_TOOLS | {"verify"} else "read")
            target = str((payload.get("changed_files") or payload.get("workspace") or ""))
            permission_decision = self.policy.check_permission(permission, target)
            if self.trace:
                self.trace.emit("action.permission", kind="policy", name=name,
                                status="allowed" if permission_decision.allowed else "denied",
                                decision=permission_decision.to_dict())
            if not permission_decision.allowed and not approval_granted:
                self._run_hooks(self._failure_hooks, name, dict(payload), PermissionError(permission_decision.reason))
                raise PermissionError(f"runtime action permission blocked: {permission_decision.reason}")
        if self.policy is not None:
            decision = self.policy.check_tool(
                # An unapproved verify call performs provider validation only.
                # The provider executes a command only after bound approval is
                # validated and consumed inside the handler.
                risk=("read_only" if name == "verify" and not approval_granted else action.risk),
                side_effect=(False if name == "verify" and not approval_granted else action.side_effect),
                requires_approval=action.requires_approval,
                approved=bool(approval_granted),
                workspace_paths=payload.get("workspace_paths") or payload.get("code_roots"),
                isolated=bool(payload.get("isolated_worktree")),
            )
            if self.trace:
                self.trace.emit("action.policy", kind="policy", name=name,
                                status="allowed" if decision.allowed else "denied",
                                decision=decision.to_dict(),
                                approval_id=(approval or {}).get("approval_id"))
            if not decision.allowed:
                raise PermissionError(f"runtime action blocked: {decision.reason}")
        tool_call_id = str(payload.get("tool_call_id") or f"tc_{uuid.uuid4().hex[:16]}")
        payload["tool_call_id"] = tool_call_id
        payload.setdefault("action_id", str(payload.get("idempotency_key") or f"act_{uuid.uuid4().hex[:16]}"))
        source_revision = payload.get("source_revision")
        worktree_revision = payload.get("worktree_revision")
        if self.revision_provider is not None:
            source_revision, worktree_revision = self.revision_provider()
        try:
            action_stage = {
                "apply_patch": "act", "inspect_diff": "diff_review",
                "verify": "verify", "rollback": "rollback",
                "sync_worktree": "sync_worktree", "post_fix_diagnosis": "post_fix_diagnosis",
                "run_build": "verify", "run_tests": "verify",
                "run_static_check": "verify", "reproduce_crash": "verify",
            }[name]
            self.state.transition(action_stage, status="running")
            if self.trace:
                self.trace.stage = action_stage
                self.trace.emit("stage.transition", kind="stage", name=action_stage,
                                status="running", tool_call_id=tool_call_id)
        except (KeyError, ValueError):
            pass
        if self.trace:
            self.trace.emit("action.started", kind="action", name=name,
                            status="running", input_hash=value_hash(payload),
                            tool_call_id=tool_call_id,
                            approval_id=(approval or {}).get("approval_id"))
        self._run_hooks(self._before_hooks, name, dict(payload))
        try:
            result = action.handler(payload)
            if not isinstance(result, dict):
                raise TypeError("runtime action must return an object")
            from services.action_failures import normalize_action_result
            # Verification without a provider is a resumable pending state,
            # retained for compatibility with the repair pipeline.
            if name == "verify" and not result.get("status") and result.get("provider") == "none":
                result["status"] = "pending"
            result = normalize_action_result(result, action=name)
            if result.get("status") == "failed":
                self._run_hooks(self._failure_hooks, name, dict(payload), RuntimeError(
                    str(result.get("summary") or result.get("failure_class") or "action failed")))
            if self.file_context_tracker is not None and name in {"read_file", "snippet_extractor"}:
                file_path = payload.get("file") or payload.get("path")
                content = result.get("content") or result.get("snippet")
                if file_path and isinstance(content, str):
                    self.file_context_tracker.record_read(str(file_path), content,
                                                          payload.get("line_start", 0), payload.get("line_end", 0),
                                                          worktree_revision)
            self._run_hooks(self._after_hooks, name, dict(payload), dict(result))
        except Exception as exc:
            self._run_hooks(self._failure_hooks, name, dict(payload), exc)
            failed_artifact = None
            artifact_dir = payload.get("artifact_dir")
            if artifact_dir and self.trace is not None:
                failed_artifact = self.trace.write_artifact(
                    artifact_dir, f"action_{name}_{tool_call_id}_failed.json",
                    {"error": str(exc), "action": name},
                )
            after_source, after_worktree = source_revision, worktree_revision
            if self.revision_provider is not None:
                after_source, after_worktree = self.revision_provider()
            self.state.checkpoint(
                state={"action": name, "error": str(exc)}, status="error",
                idempotency_key=str(payload.get("idempotency_key") or value_hash(payload)),
                source_revision=after_source, worktree_revision=after_worktree,
                input_artifact=payload.get("input_artifact"), output_artifact=failed_artifact,
                tool_call_id=tool_call_id,
            )
            if self.trace:
                self.trace.emit("action.finished", kind="action", name=name,
                                status="failed", error=str(exc),
                                input_hash=value_hash(payload), tool_call_id=tool_call_id,
                                artifact_path=failed_artifact)
            raise
        artifact_dir = payload.get("artifact_dir")
        artifact_path = None
        if artifact_dir and self.trace is not None:
            artifact_path = self.trace.write_artifact(
                artifact_dir, f"action_{name}_{tool_call_id}.json", result
            )
            self.trace.emit("artifact.written", kind="artifact", name=name,
                            status="success", artifact_path=artifact_path,
                            output_hash=value_hash(result), tool_call_id=tool_call_id)
        after_source, after_worktree = source_revision, worktree_revision
        if self.revision_provider is not None:
            after_source, after_worktree = self.revision_provider()
        self.state.checkpoint(
            state={"action": name, "output_hash": value_hash(result)},
            status="completed", idempotency_key=str(payload.get("idempotency_key") or value_hash(payload)),
            source_revision=after_source,
            worktree_revision=after_worktree,
            input_artifact=payload.get("input_artifact"), output_artifact=artifact_path or payload.get("output_artifact"),
            tool_call_id=tool_call_id,
        )
        if self.trace:
            self.trace.emit("action.finished", kind="action", name=name,
                            status="success", input_hash=value_hash(payload),
                            output_hash=value_hash(result),
                            tool_call_id=tool_call_id,
                            approval_id=(approval or {}).get("approval_id"))
        return result
