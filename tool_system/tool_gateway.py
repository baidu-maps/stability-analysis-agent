"""Single policy, validation and trace boundary for tool execution."""
from __future__ import annotations

import concurrent.futures
import datetime
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .runtime import RunTrace, value_hash
from services.policy_paths import extract_workspace_paths, paths_within_allowed_roots


@dataclass(frozen=True)
class RuntimeAuthorization:
    """In-process capability issued only after Runtime policy/approval checks."""
    run_id: str
    scope: str
    approval_id: Optional[str] = None


def _definition_allowed_roots(definition: Any) -> List[Path]:
    roots: List[Path] = []
    if definition is None:
        return roots
    for item in list(getattr(definition, "allowed_roots", []) or []):
        text = str(item or "").strip()
        if text:
            roots.append(Path(text).expanduser().resolve())
    meta = getattr(definition, "metadata", None)
    if isinstance(meta, dict):
        for item in meta.get("allowed_roots") or []:
            text = str(item or "").strip()
            if text:
                roots.append(Path(text).expanduser().resolve())
    return roots


def _effective_allowed_roots(policy: Any, definition: Any) -> List[Path]:
    roots = _definition_allowed_roots(definition)
    if policy is not None:
        roots.extend(list(getattr(policy, "allowed_roots", []) or []))
    deduped: List[Path] = []
    seen = set()
    for root in roots:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(root)
    return deduped


def _resolve_timeout_enforcement(tool: Any, policy_meta: Dict[str, Any]) -> str:
    explicit = str(policy_meta.get("timeout_enforcement") or "").strip()
    if explicit in {"subprocess", "best_effort", "none"}:
        return explicit
    definition = getattr(tool, "definition", None)
    if definition is not None:
        value = str(getattr(definition, "timeout_enforcement", "") or "").strip()
        if value in {"subprocess", "best_effort", "none"}:
            return value
        meta = getattr(definition, "metadata", None)
        if isinstance(meta, dict):
            nested = str(meta.get("timeout_enforcement") or "").strip()
            if nested in {"subprocess", "best_effort", "none"}:
                return nested
    return "best_effort"


def _execute_with_timeout(
    tool: Any,
    input_data: Dict[str, Any],
    timeout_sec: Optional[float],
    *,
    enforcement: str = "best_effort",
) -> Dict[str, Any]:
    limit = float(timeout_sec or 0)
    if limit <= 0 or enforcement == "none":
        result = tool.execute(input_data)
    elif enforcement == "subprocess":
        payload = dict(input_data)
        payload.setdefault("timeout_sec", limit)
        payload["_gateway_timeout_sec"] = limit
        result = tool.execute(payload)
    else:
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(tool.execute, input_data)
        try:
            result = future.result(timeout=limit)
        except concurrent.futures.TimeoutError as exc:
            pool.shutdown(wait=False, cancel_futures=True)
            raise TimeoutError(f"tool execution exceeded timeout_sec={limit}") from exc
        pool.shutdown(wait=False, cancel_futures=True)
    if not isinstance(result, dict):
        raise TypeError("tool must return an object")
    return result


class ToolExecutionGateway:
    """Execute tools through one auditable boundary."""

    def __init__(self, policy: Any = None, trace: Optional[RunTrace] = None, security_analyzer: Any = None):
        self.policy = policy
        self.trace = trace
        self.security_analyzer = security_analyzer

    @staticmethod
    def _observe(input_data: Dict[str, Any], **payload: Any) -> None:
        store = input_data.get("_observation_store") if isinstance(input_data, dict) else None
        if store is not None and hasattr(store, "record"):
            store.record(**payload)

    def execute(self, name: str, tool: Any, input_data: Dict[str, Any]) -> Dict[str, Any]:
        definition = getattr(tool, "definition", None)
        policy_meta = getattr(tool, "runtime_policy", lambda: {})()
        cost_class = str(policy_meta.get("cost_class") or "low")
        if self.security_analyzer is not None and definition is not None and getattr(definition, "side_effect", False):
            workspace = str(input_data.get("workspace") or (input_data.get("code_roots") or [""])[0])
            if workspace:
                security = self.security_analyzer.analyze_action({"name": name, **dict(input_data)}, workspace,
                                                                  authorization=input_data.get("_runtime_authorization"))
                if self.trace is not None:
                    self.trace.emit("tool.security", kind="policy", name=name,
                                    status="allowed" if security.allowed else "denied",
                                    decision=security.to_dict())
                if not security.allowed:
                    self._observe(input_data, kind="policy_decision", source="security_analyzer",
                                  status="denied", summary=security.reason,
                                  details=security.to_dict(), actionable=True)
                    raise PermissionError(f"Tool '{name}' blocked: {security.reason}")
        timeout_sec = policy_meta.get("timeout_sec")
        if timeout_sec is None and definition is not None:
            timeout_sec = getattr(definition, "timeout_sec", None)
        if self.trace is not None:
            if hasattr(self.trace.budget, "consume_tool_cost"):
                self.trace.budget.consume_tool_cost(cost_class)
            else:
                self.trace.budget.consume("tool")
        timeout_enforcement = _resolve_timeout_enforcement(tool, policy_meta)
        if definition is not None and self.policy is not None:
            authorization = input_data.get("_runtime_authorization")
            approved = isinstance(authorization, RuntimeAuthorization)
            isolated = bool(approved and authorization.scope == "isolated_worktree")
            workspace_paths = extract_workspace_paths(input_data)
            allowed_roots = _effective_allowed_roots(self.policy, definition)
            if allowed_roots and workspace_paths:
                if not paths_within_allowed_roots(workspace_paths, allowed_roots):
                    decision = {
                        "allowed": False,
                        "decision": "denied",
                        "reason": "tool paths are outside allowed roots",
                    }
                    if self.trace is not None:
                        self.trace.emit(
                            "tool.policy",
                            kind="policy",
                            name=name,
                            status="denied",
                            decision=decision,
                            input_hash=value_hash(input_data),
                            cost_class=cost_class,
                            timeout_sec=timeout_sec,
                        )
                    self._observe(
                        input_data,
                        kind="policy_decision",
                        source="tool_gateway",
                        status="denied",
                        summary=decision["reason"],
                        details={"tool": name, "decision": decision},
                        actionable=True,
                    )
                    raise PermissionError(f"Tool '{name}' blocked: tool paths are outside allowed roots")
            decision_obj = self.policy.check_tool(
                risk=getattr(definition, "risk", "read_only"),
                side_effect=bool(getattr(definition, "side_effect", False)),
                approved=approved,
                requires_approval=bool(getattr(definition, "requires_approval", False)),
                workspace_paths=workspace_paths,
                isolated=isolated,
            )
            decision = decision_obj.to_dict()
            if self.trace is not None:
                self.trace.emit(
                    "tool.policy",
                    kind="policy",
                    name=name,
                    status="allowed" if decision_obj.allowed else "denied",
                    decision=decision,
                    input_hash=value_hash(input_data),
                    cost_class=cost_class,
                    timeout_sec=timeout_sec,
                )
            if not decision_obj.allowed:
                self._observe(
                    input_data,
                    kind="policy_decision",
                    source="tool_gateway",
                    status="denied",
                    summary=decision_obj.reason,
                    details={"tool": name, "decision": decision},
                    actionable=True,
                )
                raise PermissionError(f"Tool '{name}' blocked: {decision_obj.reason}")
            self._observe(
                input_data,
                kind="policy_decision",
                source="tool_gateway",
                status="allowed",
                summary=decision_obj.reason,
                details={"tool": name, "decision": decision},
                actionable=False,
            )
        started = time.perf_counter()
        started_at = datetime.datetime.now().astimezone().isoformat()
        exec_input = {
            key: value for key, value in dict(input_data or {}).items()
            if key != "_observation_store"
        }
        if self.trace is not None and "_runtime_trace" not in exec_input:
            exec_input["_runtime_trace"] = self.trace
        try:
            validator = getattr(tool, "validate_input", None)
            if callable(validator):
                valid, error = validator(exec_input)
                if not valid:
                    raise ValueError(f"Tool '{name}' input validation failed: {error}")
            result = _execute_with_timeout(
                tool, exec_input, timeout_sec, enforcement=timeout_enforcement,
            )
        except Exception as exc:
            self._observe(
                input_data,
                kind="tool_error",
                source=name,
                status="failed",
                summary=str(exc),
                details={"tool": name},
                actionable=True,
            )
            if self.trace is not None:
                self.trace.emit(
                    "tool.failed",
                    kind="tool",
                    name=name,
                    status="failed",
                    started_at=started_at,
                    duration_ms=int(round((time.perf_counter() - started) * 1000)),
                    error=str(exc),
                    input_hash=value_hash(input_data),
                    cost_class=cost_class,
                    timeout_sec=timeout_sec,
                    timeout_enforcement=timeout_enforcement,
                    timed_out=isinstance(exc, TimeoutError),
                )
            raise
        if self.trace is not None:
            self.trace.emit(
                "tool.success",
                kind="tool",
                name=name,
                status="success",
                started_at=started_at,
                duration_ms=int(round((time.perf_counter() - started) * 1000)),
                input_hash=value_hash(input_data),
                output_hash=value_hash(result),
                risk=policy_meta.get("risk"),
                idempotent=policy_meta.get("idempotent"),
                cost_class=cost_class,
                timeout_sec=timeout_sec,
                timeout_enforcement=timeout_enforcement,
            )
        self._observe(
            input_data,
            kind="tool_result",
            source=name,
            status="success",
            summary=f"{name} completed",
            details={"tool": name, "output_hash": value_hash(result)},
            actionable=False,
        )
        return result

    def authorize(self, name: str, tool: Any, input_data: Dict[str, Any]) -> None:
        """Apply the same policy gate to streaming tools."""
        definition = getattr(tool, "definition", None)
        if definition is None or self.policy is None:
            return
        authorization = input_data.get("_runtime_authorization")
        approved = isinstance(authorization, RuntimeAuthorization)
        workspace_paths = extract_workspace_paths(input_data)
        allowed_roots = _effective_allowed_roots(self.policy, definition)
        if allowed_roots and workspace_paths:
            if not paths_within_allowed_roots(workspace_paths, allowed_roots):
                self._observe(
                    input_data,
                    kind="policy_decision",
                    source="tool_gateway",
                    status="denied",
                    summary="tool paths are outside allowed roots",
                    details={"tool": name},
                    actionable=True,
                )
                raise PermissionError(f"Tool '{name}' blocked: tool paths are outside allowed roots")
        decision = self.policy.check_tool(
            risk=getattr(definition, "risk", "read_only"),
            side_effect=bool(getattr(definition, "side_effect", False)),
            approved=approved,
            requires_approval=bool(getattr(definition, "requires_approval", False)),
            workspace_paths=workspace_paths,
            isolated=bool(approved and authorization.scope == "isolated_worktree"),
        )
        policy_meta = getattr(tool, "runtime_policy", lambda: {})()
        if self.trace is not None:
            self.trace.emit(
                "tool.policy",
                kind="policy",
                name=name,
                status="allowed" if decision.allowed else "denied",
                decision=decision.to_dict(),
                input_hash=value_hash(input_data),
                cost_class=str(policy_meta.get("cost_class") or "low"),
                timeout_sec=policy_meta.get("timeout_sec"),
            )
        if not decision.allowed:
            self._observe(
                input_data,
                kind="policy_decision",
                source="tool_gateway",
                status="denied",
                summary=decision.reason,
                details={"tool": name, "decision": decision.to_dict()},
                actionable=True,
            )
            raise PermissionError(f"Tool '{name}' blocked: {decision.reason}")
        self._observe(
            input_data,
            kind="policy_decision",
            source="tool_gateway",
            status="allowed",
            summary=decision.reason,
            details={"tool": name, "decision": decision.to_dict()},
            actionable=False,
        )

    def execute_stream(self, name: str, tool: Any, input_data: Dict[str, Any]):
        """Execute a streaming tool through the same policy, budget and audit boundary."""
        definition = getattr(tool, "definition", None)
        policy_meta = getattr(tool, "runtime_policy", lambda: {})()
        cost_class = str(policy_meta.get("cost_class") or "low")
        timeout_sec = policy_meta.get("timeout_sec")
        if timeout_sec is None and definition is not None:
            timeout_sec = getattr(definition, "timeout_sec", None)
        if self.trace is not None:
            if hasattr(self.trace.budget, "consume_tool_cost"):
                self.trace.budget.consume_tool_cost(cost_class)
            else:
                self.trace.budget.consume("tool")
        self.authorize(name, tool, input_data)
        exec_input = {
            key: value for key, value in dict(input_data or {}).items()
            if key != "_observation_store"
        }
        if self.trace is not None and "_runtime_trace" not in exec_input:
            exec_input["_runtime_trace"] = self.trace
        started = time.perf_counter()
        started_at = datetime.datetime.now().astimezone().isoformat()
        output_digest = hashlib.sha256()
        try:
            validator = getattr(tool, "validate_input", None)
            if callable(validator):
                valid, error = validator(exec_input)
                if not valid:
                    raise ValueError(f"Tool '{name}' input validation failed: {error}")
            for chunk in tool.execute_stream(exec_input):
                output_digest.update(str(chunk).encode("utf-8", errors="replace"))
                yield chunk
        except Exception as exc:
            self._observe(
                input_data,
                kind="tool_error",
                source=name,
                status="failed",
                summary=str(exc),
                details={"tool": name, "stream": True},
                actionable=True,
            )
            if self.trace is not None:
                self.trace.emit(
                    "tool.failed",
                    kind="tool",
                    name=name,
                    status="failed",
                    started_at=started_at,
                    duration_ms=int(round((time.perf_counter() - started) * 1000)),
                    error=str(exc),
                    input_hash=value_hash(input_data),
                    cost_class=cost_class,
                    timeout_sec=timeout_sec,
                    streaming=True,
                )
            raise
        output_hash = output_digest.hexdigest()[:16]
        if self.trace is not None:
            self.trace.emit(
                "tool.success",
                kind="tool",
                name=name,
                status="success",
                started_at=started_at,
                duration_ms=int(round((time.perf_counter() - started) * 1000)),
                input_hash=value_hash(input_data),
                output_hash=output_hash,
                cost_class=cost_class,
                timeout_sec=timeout_sec,
                streaming=True,
            )
        self._observe(
            input_data,
            kind="tool_result",
            source=name,
            status="success",
            summary=f"{name} stream completed",
            details={"tool": name, "output_hash": output_hash, "stream": True},
            actionable=False,
        )
