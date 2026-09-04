"""Small, dependency-free runtime primitives shared by workflows and shells.

The runtime deliberately stores metadata and events rather than model-specific
messages.  This keeps replay and observability useful across direct,
LangChain, and LangGraph backends.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

RUN_STAGES = ("observe", "analyze", "diagnose", "evidence_review", "plan",
              "repair_proposal", "act", "diff_review", "verify", "judge",
              "rollback", "sync_worktree", "post_fix_diagnosis", "decide")
LEGACY_STAGE_ALIASES = {"analysis": "analyze", "planning": "plan", "repair": "act"}
CANONICAL_EVENTS = {
    "session.started": "session_started",
    "tool.started": "tool_call", "tool.success": "tool_result", "tool.failed": "tool_result",
    "action.started": "tool_call", "action.finished": "tool_result",
    "agent.context_requests_parsed": "agent_decision", "agent.context_resolved": "observation",
    "verification.completed": "verification", "decision.final": "termination",
}


def _short_hash(value: Any) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    except Exception:
        encoded = str(value).encode("utf-8", errors="replace")
    return hashlib.sha256(encoded).hexdigest()[:16]


@dataclass
class RuntimeBudget:
    """Hard limits for one run; zero means unlimited."""

    max_llm_calls: int = 0
    max_tool_calls: int = 0
    max_total_seconds: float = 0.0
    max_total_tokens: int = 0
    max_estimated_cost: float = 0.0
    max_cost_class: Dict[str, int] = field(default_factory=dict)
    llm_calls: int = 0
    tool_calls: int = 0
    cost_class_counts: Dict[str, int] = field(default_factory=dict)
    token_usage: Dict[str, int] = field(default_factory=lambda: {
        "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
    })
    estimated_cost: float = 0.0
    started_perf: float = field(default_factory=time.perf_counter)

    def consume(self, kind: str) -> None:
        if kind == "llm":
            self.llm_calls += 1
            limit = self.max_llm_calls
            current = self.llm_calls
        elif kind == "tool":
            self.tool_calls += 1
            limit = self.max_tool_calls
            current = self.tool_calls
        else:
            return
        if limit > 0 and current > limit:
            raise RuntimeError(f"runtime budget exceeded: {kind}_calls>{limit}")
        if self.max_total_seconds > 0 and time.perf_counter() - self.started_perf > self.max_total_seconds:
            raise RuntimeError("runtime budget exceeded: total_seconds")

    def record_usage(self, usage: Optional[Dict[str, Any]], estimated_cost: Optional[float] = None) -> None:
        usage = usage if isinstance(usage, dict) else {}
        input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or input_tokens + output_tokens)
        self.token_usage["input_tokens"] += max(0, input_tokens)
        self.token_usage["output_tokens"] += max(0, output_tokens)
        self.token_usage["total_tokens"] += max(0, total_tokens)
        if estimated_cost is not None:
            self.estimated_cost += max(0.0, float(estimated_cost))
        if self.max_total_tokens > 0 and self.token_usage["total_tokens"] > self.max_total_tokens:
            raise RuntimeError("runtime budget exceeded: total_tokens")
        if self.max_estimated_cost > 0 and self.estimated_cost > self.max_estimated_cost:
            raise RuntimeError("runtime budget exceeded: estimated_cost")

    def consume_tool_cost(self, cost_class: str) -> None:
        self.consume("tool")
        key = str(cost_class or "low").strip().lower() or "low"
        self.cost_class_counts[key] = int(self.cost_class_counts.get(key, 0)) + 1
        limit = int(self.max_cost_class.get(key) or 0) if isinstance(self.max_cost_class, dict) else 0
        if limit > 0 and self.cost_class_counts[key] > limit:
            raise RuntimeError(f"runtime budget exceeded: cost_class>{key}:{limit}")


class RunTrace:
    """In-memory event stream for one execution, suitable for JSON sidecars."""

    def __init__(self, run_id: Optional[str] = None, budget: Optional[RuntimeBudget] = None,
                 *, engine: Optional[str] = None):
        self.run_id = run_id or f"run_{uuid.uuid4().hex[:16]}"
        self.budget = budget or RuntimeBudget()
        self.engine = engine
        self.stage = "observe"
        self.events: List[Dict[str, Any]] = []

    @classmethod
    def from_dict(cls, payload: Any, *, run_id: Optional[str] = None,
                  engine: Optional[str] = None) -> "RunTrace":
        value = payload if isinstance(payload, dict) else {}
        budget_value = value.get("budget") if isinstance(value.get("budget"), dict) else {}
        budget = RuntimeBudget(
            max_llm_calls=int(budget_value.get("max_llm_calls") or 0),
            max_tool_calls=int(budget_value.get("max_tool_calls") or 0),
            max_total_seconds=float(budget_value.get("max_total_seconds") or 0),
            max_total_tokens=int(budget_value.get("max_total_tokens") or 0),
            max_estimated_cost=float(budget_value.get("max_estimated_cost") or 0),
            llm_calls=int(budget_value.get("llm_calls") or 0),
            tool_calls=int(budget_value.get("tool_calls") or 0),
        )
        usage = budget_value.get("token_usage")
        if isinstance(usage, dict):
            budget.token_usage = {
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
            }
        budget.estimated_cost = float(budget_value.get("estimated_cost") or 0)
        trace = cls(
            run_id=run_id or str(value.get("run_id") or "") or None,
            budget=budget,
            engine=engine if engine is not None else value.get("engine"),
        )
        trace.stage = str(value.get("stage") or "observe")
        trace.events = [dict(item) for item in value.get("events", []) if isinstance(item, dict)]
        return trace

    def emit(self, event: str, *, kind: str = "runtime", name: str = "", status: str = "success", **data: Any) -> Dict[str, Any]:
        parent_event_id = data.pop("parent_event_id", None)
        seq = len(self.events) + 1
        payload: Dict[str, Any] = {
            "schema_version": 1,
            "seq": seq,
            "step_id": f"step_{seq:06d}",
            "event_id": f"evt_{seq:06d}",
            "run_id": self.run_id,
            "event": event,
            "kind": kind,
            "name": name,
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "engine": self.engine,
            "tool_call_id": None,
            "duration_ms": 0,
            "input_hash": None,
            "output_hash": None,
            "artifact_path": None,
            "retry_count": 0,
            "failover_from": None,
            "token_usage": {},
            "estimated_cost": 0.0,
            "approval_id": None,
            "termination_reason": None,
        }
        payload["canonical_event"] = CANONICAL_EVENTS.get(event, event)
        payload["stage"] = data.pop("stage", None) or self.stage
        if "step_id" in data:
            payload["step_id"] = data.pop("step_id")
        if parent_event_id:
            payload["parent_event_id"] = parent_event_id
            payload["parent_step_id"] = parent_event_id.replace("evt_", "step_")
        # Trace is an index, not a second artifact store. Keep large values out
        # of the event stream while retaining a stable hash for replay.
        for key, value in data.items():
            if value is None:
                continue
            if key in {"input", "output", "content", "response", "stdout", "stderr"}:
                payload[f"{key}_hash"] = _short_hash(value)
                continue
            payload[key] = value
        if kind == "llm" and status == "success":
            self.budget.record_usage(
                payload.get("token_usage"), payload.get("estimated_cost"),
            )
        try:
            from protocol.models import AgentEvent
            payload["agent_event"] = AgentEvent.from_trace_payload(payload).to_dict()
        except Exception:
            pass
        if kind == "tool" and name:
            try:
                from protocol.models import ToolCall
                payload["tool_call"] = ToolCall.from_event(payload).to_dict()
            except Exception:
                pass
        self.events.append(payload)
        return payload

    def snapshot(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "engine": self.engine,
            "stage": self.stage,
            "events": list(self.events),
            "budget": {
                "max_llm_calls": self.budget.max_llm_calls,
                "max_tool_calls": self.budget.max_tool_calls,
                "max_total_seconds": self.budget.max_total_seconds,
                "max_total_tokens": self.budget.max_total_tokens,
                "max_estimated_cost": self.budget.max_estimated_cost,
                "llm_calls": self.budget.llm_calls,
                "tool_calls": self.budget.tool_calls,
                "token_usage": dict(self.budget.token_usage),
                "estimated_cost": self.budget.estimated_cost,
            },
        }

    def emit_llm(self, *, status: str = "success", duration_ms: int = 0,
                 token_usage: Optional[Dict[str, Any]] = None,
                 estimated_cost: Optional[float] = None, **data: Any) -> Dict[str, Any]:
        """Record a normalized LLM event without storing the model response."""
        if token_usage:
            data["token_usage"] = dict(token_usage)
        if estimated_cost is not None:
            data["estimated_cost"] = float(estimated_cost)
        return self.emit("llm.call", kind="llm", status=status,
                         duration_ms=duration_ms, **data)

    def write_artifact(self, directory: Any, name: str, value: Any) -> str:
        """Persist a JSON artifact and return its path for trace/checkpoints."""
        path = Path(directory).expanduser().resolve() / str(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        return str(path)


@dataclass
class RuntimeCheckpoint:
    """Serializable checkpoint for resuming a harness run."""
    stage: str
    status: str = "completed"
    state: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    checkpoint_id: str = field(default_factory=lambda: f"ckpt_{uuid.uuid4().hex[:16]}")
    source_revision: Optional[str] = None
    worktree_revision: Optional[str] = None
    idempotency_key: Optional[str] = None
    retry_count: int = 0
    input_artifact: Optional[str] = None
    output_artifact: Optional[str] = None
    tool_call_id: Optional[str] = None
    event_seq: int = 0
    focus_chain: List[Dict[str, Any]] = field(default_factory=list)
    file_context: List[Dict[str, Any]] = field(default_factory=list)
    context_parts: List[Dict[str, Any]] = field(default_factory=list)
    snapshot_id: Optional[str] = None
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)
    hook_state: Dict[str, Any] = field(default_factory=dict)
    abort_requested: bool = False
    verification_capabilities: List[Dict[str, Any]] = field(default_factory=list)
    verification_claim: Dict[str, Any] = field(default_factory=dict)
    reproduction_plan: Dict[str, Any] = field(default_factory=dict)
    verification_plan_fingerprint: str = ""
    pending_approvals: List[Dict[str, Any]] = field(default_factory=list)
    pending_actions: List[Dict[str, Any]] = field(default_factory=list)
    context_session_hash: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"checkpoint_id": self.checkpoint_id, "stage": self.stage, "status": self.status,
                "state": dict(self.state), "created_at": self.created_at,
                "source_revision": self.source_revision, "worktree_revision": self.worktree_revision,
                "idempotency_key": self.idempotency_key, "retry_count": self.retry_count,
                "input_artifact": self.input_artifact, "output_artifact": self.output_artifact,
                "tool_call_id": self.tool_call_id, "event_seq": self.event_seq,
                "focus_chain": list(self.focus_chain), "file_context": list(self.file_context),
                "pending_approvals": list(self.pending_approvals), "pending_actions": list(self.pending_actions),
                "context_session_hash": self.context_session_hash,
                "context_parts": list(self.context_parts), "snapshot_id": self.snapshot_id,
                "diagnostics": list(self.diagnostics), "hook_state": dict(self.hook_state),
                "abort_requested": self.abort_requested,
                "verification_capabilities": list(self.verification_capabilities),
                "verification_claim": dict(self.verification_claim),
                "reproduction_plan": dict(self.reproduction_plan),
                "verification_plan_fingerprint": self.verification_plan_fingerprint}


@dataclass
class RuntimeState:
    """Model-independent lifecycle state shared by CLI, daemon and plugins."""
    stage: str = "observe"
    status: str = "running"
    reason: Optional[str] = None
    decision: Optional[str] = None
    checkpoints: List[RuntimeCheckpoint] = field(default_factory=list)
    approval: Optional[Dict[str, Any]] = None
    session_id: Optional[str] = field(default_factory=lambda: f"session_{uuid.uuid4().hex[:16]}")
    last_event_seq: int = 0
    source_revision: Optional[str] = None
    worktree_revision: Optional[str] = None
    pending_actions: List[Dict[str, Any]] = field(default_factory=list)
    active_hypotheses: List[Dict[str, Any]] = field(default_factory=list)
    termination_reason: Optional[str] = None
    replay_safe: bool = True
    focus_chain: List[Dict[str, Any]] = field(default_factory=list)
    file_context: List[Dict[str, Any]] = field(default_factory=list)
    context_parts: List[Dict[str, Any]] = field(default_factory=list)
    snapshot_id: Optional[str] = None
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)
    hook_state: Dict[str, Any] = field(default_factory=dict)
    abort_requested: bool = False
    verification_capabilities: List[Dict[str, Any]] = field(default_factory=list)
    verification_claim: Dict[str, Any] = field(default_factory=dict)
    reproduction_plan: Dict[str, Any] = field(default_factory=dict)
    verification_plan_fingerprint: str = ""

    def transition(self, stage: str, *, status: str = "running", reason: Optional[str] = None) -> None:
        if stage not in RUN_STAGES:
            raise ValueError(f"unknown runtime stage: {stage}")
        self.stage, self.status, self.reason = stage, status, reason
        if reason:
            self.termination_reason = reason

    def checkpoint(self, *, state: Optional[Dict[str, Any]] = None, status: str = "completed",
                   source_revision: Optional[str] = None, worktree_revision: Optional[str] = None,
                   idempotency_key: Optional[str] = None, retry_count: int = 0,
                   input_artifact: Optional[str] = None, output_artifact: Optional[str] = None,
                   tool_call_id: Optional[str] = None, focus_chain: Optional[List[Dict[str, Any]]] = None,
                   file_context: Optional[List[Dict[str, Any]]] = None,
                   pending_approvals: Optional[List[Dict[str, Any]]] = None,
                   context_session_hash: Optional[str] = None,
                   context_parts: Optional[List[Dict[str, Any]]] = None,
                   snapshot_id: Optional[str] = None,
                   diagnostics: Optional[List[Dict[str, Any]]] = None,
                   hook_state: Optional[Dict[str, Any]] = None,
                   abort_requested: bool = False,
                   verification_capabilities: Optional[List[Dict[str, Any]]] = None,
                   verification_claim: Optional[Dict[str, Any]] = None,
                   reproduction_plan: Optional[Dict[str, Any]] = None,
                   verification_plan_fingerprint: Optional[str] = None) -> RuntimeCheckpoint:
        checkpoint_state = state or {}
        stable_key = idempotency_key or f"{self.stage}:{_short_hash(checkpoint_state)}"
        item = RuntimeCheckpoint(self.stage, status=status, state=checkpoint_state,
                                 source_revision=source_revision, worktree_revision=worktree_revision,
                                 idempotency_key=stable_key, retry_count=int(retry_count or 0),
                                 input_artifact=input_artifact, output_artifact=output_artifact,
                                 tool_call_id=tool_call_id, event_seq=self.last_event_seq,
                                 focus_chain=list(focus_chain or self.focus_chain),
                                 file_context=list(file_context or self.file_context),
                                 pending_approvals=list(pending_approvals or ([] if not self.approval else [self.approval])),
                                 pending_actions=list(self.pending_actions), context_session_hash=context_session_hash)
        item.context_parts = list(context_parts if context_parts is not None else self.context_parts)
        item.snapshot_id = snapshot_id if snapshot_id is not None else self.snapshot_id
        item.diagnostics = list(diagnostics if diagnostics is not None else self.diagnostics)
        item.hook_state = dict(hook_state if hook_state is not None else self.hook_state)
        item.abort_requested = bool(abort_requested or self.abort_requested)
        item.verification_capabilities = list(verification_capabilities if verification_capabilities is not None else self.verification_capabilities)
        item.verification_claim = dict(verification_claim if verification_claim is not None else self.verification_claim)
        item.reproduction_plan = dict(reproduction_plan if reproduction_plan is not None else self.reproduction_plan)
        item.verification_plan_fingerprint = str(verification_plan_fingerprint if verification_plan_fingerprint is not None else self.verification_plan_fingerprint)
        self.checkpoints.append(item)
        return item

    def to_dict(self) -> Dict[str, Any]:
        return {"stage": self.stage, "status": self.status, "reason": self.reason,
                "decision": self.decision,
                "approval": self.approval,
                "session_id": self.session_id, "last_event_seq": self.last_event_seq,
                "source_revision": self.source_revision, "worktree_revision": self.worktree_revision,
                "pending_actions": list(self.pending_actions), "active_hypotheses": list(self.active_hypotheses),
                "termination_reason": self.termination_reason, "replay_safe": self.replay_safe,
                "focus_chain": list(self.focus_chain), "file_context": list(self.file_context),
                "context_parts": list(self.context_parts), "snapshot_id": self.snapshot_id,
                "diagnostics": list(self.diagnostics), "hook_state": dict(self.hook_state),
                "abort_requested": self.abort_requested,
                "verification_capabilities": list(self.verification_capabilities),
                "verification_claim": dict(self.verification_claim),
                "reproduction_plan": dict(self.reproduction_plan),
                "verification_plan_fingerprint": self.verification_plan_fingerprint,
                "checkpoints": [item.to_dict() for item in self.checkpoints]}

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "RuntimeState":
        if not isinstance(payload, dict):
            raise ValueError("runtime state must be an object")
        stage = LEGACY_STAGE_ALIASES.get(str(payload.get("stage") or "observe"), str(payload.get("stage") or "observe"))
        state = cls(stage=stage,
                    status=str(payload.get("status") or "running"),
                    reason=payload.get("reason"),
                    decision=payload.get("decision"),
                    approval=payload.get("approval") if isinstance(payload.get("approval"), dict) else None)
        state.session_id = payload.get("session_id") or state.session_id
        state.last_event_seq = int(payload.get("last_event_seq") or 0)
        state.source_revision = payload.get("source_revision")
        state.worktree_revision = payload.get("worktree_revision")
        state.pending_actions = list(payload.get("pending_actions") or [])
        state.active_hypotheses = list(payload.get("active_hypotheses") or [])
        state.termination_reason = payload.get("termination_reason")
        state.replay_safe = bool(payload.get("replay_safe", True))
        state.focus_chain = list(payload.get("focus_chain") or [])
        state.file_context = list(payload.get("file_context") or [])
        state.context_parts = list(payload.get("context_parts") or [])
        state.snapshot_id = payload.get("snapshot_id")
        state.diagnostics = list(payload.get("diagnostics") or [])
        state.hook_state = dict(payload.get("hook_state") or {})
        state.abort_requested = bool(payload.get("abort_requested", False))
        state.verification_capabilities = list(payload.get("verification_capabilities") or [])
        state.verification_claim = dict(payload.get("verification_claim") or {})
        state.reproduction_plan = dict(payload.get("reproduction_plan") or {})
        state.verification_plan_fingerprint = str(payload.get("verification_plan_fingerprint") or "")
        if state.stage not in RUN_STAGES:
            raise ValueError(f"unknown runtime stage: {state.stage}")
        for item in payload.get("checkpoints") or []:
            if isinstance(item, dict):
                checkpoint_stage = LEGACY_STAGE_ALIASES.get(str(item.get("stage") or "observe"), str(item.get("stage") or "observe"))
                if checkpoint_stage not in RUN_STAGES:
                    continue
                state.checkpoints.append(RuntimeCheckpoint(
                    stage=checkpoint_stage, status=str(item.get("status") or "completed"),
                    state=item.get("state") if isinstance(item.get("state"), dict) else {},
                    created_at=str(item.get("created_at") or datetime.now(timezone.utc).isoformat()),
                    checkpoint_id=str(item.get("checkpoint_id") or f"ckpt_{uuid.uuid4().hex[:16]}"),
                    source_revision=item.get("source_revision"), worktree_revision=item.get("worktree_revision"),
                    idempotency_key=item.get("idempotency_key"), retry_count=int(item.get("retry_count") or 0),
                    input_artifact=item.get("input_artifact"), output_artifact=item.get("output_artifact"),
                    tool_call_id=item.get("tool_call_id"), event_seq=int(item.get("event_seq") or 0),
                    focus_chain=list(item.get("focus_chain") or []), file_context=list(item.get("file_context") or []),
                    context_parts=list(item.get("context_parts") or []), snapshot_id=item.get("snapshot_id"),
                    diagnostics=list(item.get("diagnostics") or []), hook_state=dict(item.get("hook_state") or {}),
                    abort_requested=bool(item.get("abort_requested", False)),
                    verification_capabilities=list(item.get("verification_capabilities") or []),
                    verification_claim=dict(item.get("verification_claim") or {}),
                    reproduction_plan=dict(item.get("reproduction_plan") or {}),
                    verification_plan_fingerprint=str(item.get("verification_plan_fingerprint") or ""),
                    pending_approvals=list(item.get("pending_approvals") or []),
                    pending_actions=list(item.get("pending_actions") or []),
                    context_session_hash=item.get("context_session_hash"),
                ))
        return state


def value_hash(value: Any) -> str:
    """Return a stable, non-sensitive identifier for event correlation."""
    return _short_hash(value)
