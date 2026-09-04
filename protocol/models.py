from __future__ import annotations

import os
from dataclasses import dataclass, asdict, fields
from typing import Any, Dict, List, Literal, Optional

from .version import PROTOCOL_VERSION


RunStatus = Literal["queued", "running", "approval_required", "verification_pending", "done", "error", "canceled"]
EngineType = Literal["direct", "langchain", "langgraph"]
OutputFormat = Literal["json", "markdown", "text"]
ScopeType = Literal["full", "gen_prompt_only", "parse_stack_only", "parse_log_only"]
PromptModeType = Literal["analysis", "fix"]
DEFAULT_PROMPT_MODE: PromptModeType = "fix"
AgentLoopType = Literal["single", "context_loop"]
LlmModeType = Literal["fixed", "auto"]
LlmProfileType = Literal["default", "strong", "fast"]


def normalize_run_code_roots(req: "RunRequest") -> List[str]:
    """Return the normalized, ordered ``code_roots`` protocol field."""
    roots: List[str] = []
    crs = req.code_roots
    if crs:
        roots.extend(str(r).strip() for r in crs if r and str(r).strip())
    seen: set[str] = set()
    out: List[str] = []
    for r in roots:
        a = os.path.abspath(os.path.expanduser(r))
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out


def run_request_from_dict(data: Dict[str, Any]) -> "RunRequest":
    """
    从 dict 构造 RunRequest：只保留 dataclass 已知字段，忽略未知 key。
    只保留当前协议定义的字段；未知字段直接丢弃。
    """
    payload = dict(data or {})
    removed = {"code_root", "skip_ai", "parse_stack_only", "run_scope",
               "legacy_result", "crash_log_legacy"} & set(payload)
    if removed:
        raise ValueError("removed protocol fields: " + ", ".join(sorted(removed)))
    if "engine" in payload and payload["engine"] not in {"direct", "langchain", "langgraph"}:
        raise ValueError("engine must be one of: direct, langchain, langgraph")

    allowed = {f.name for f in fields(RunRequest)}
    filtered = {k: v for k, v in payload.items() if k in allowed}
    return RunRequest(**filtered)


@dataclass(frozen=True)
class RunRequest:
    """daemon/CLI/Web 的统一输入协议。"""

    crash_log: Optional[str] = None  # 文件路径；或 "-" 表示 stdin
    crash_log_content: Optional[str] = None  # 直接传内容（daemon 会走 stdin）
    crash_log_dir: Optional[str] = None  # 批量分析目录
    library_dir: Optional[str] = None
    code_roots: Optional[List[str]] = None  # 多根，顺序 = 查找优先级
    config: Optional[str] = None  # 配置文件路径
    # 显式验证配置；候选发现不会自动填充或执行此字段
    verification: Optional[Dict[str, Any]] = None
    engine: EngineType = "direct"

    output_format: OutputFormat = "markdown"

    scope: ScopeType = "full"
    prompt_mode: PromptModeType = DEFAULT_PROMPT_MODE
    agent_loop: Optional[AgentLoopType] = None  # None=随 prompt_mode 决定（analysis→context_loop，fix→single）
    # None/省略 = 不传 CLI 旗标，沿用 argparse 默认（0=随 prompt_mode：analysis=3，其它=1）
    max_agent_rounds: Optional[int] = None
    max_context_requests_per_round: Optional[int] = None
    optimized: bool = False
    # None/省略 = 沿用 provider 配置；True/False 分别透传 --streaming / --no-streaming
    streaming: Optional[bool] = None

    apply_ai_fixes: bool = True
    backup_original_sources: bool = True

    force_disassembly: bool = False
    force_anr_analysis: bool = False
    force_memory_analysis: bool = False
    force_timeline_analysis: bool = False
    native_leak_dir: Optional[str] = None
    native_leak_trace_db: Optional[str] = None

    llm_mode: Optional[LlmModeType] = None
    llm_profile: Optional[LlmProfileType] = None
    include_memory_in_05: bool = False
    # Explicit opt-in for generating the handoff bundle used to compare this
    # agent with an external general-purpose coding agent.
    external_agent_evaluation: bool = False

    vector_db_path: Optional[str] = None
    vector_db_max_results: Optional[int] = None
    vector_db_record_usage: bool = False
    rule_confidence_threshold: Optional[float] = None
    use_ctags_index: bool = False
    plugin_modules: Optional[List[str]] = None
    max_sibling_member_functions: Optional[int] = None
    max_stack_frames_symbol_enrich: Optional[int] = None
    max_stack_frames_in_prompt: Optional[int] = None
    max_shared_var_related_functions: Optional[int] = None
    min_key_read_related_functions: Optional[int] = None
    max_symbol_only_rescues: Optional[int] = None
    max_crash_caller_search_files: Optional[int] = None
    code_context_timeout_sec: Optional[float] = None
    find_source_timeout_sec: Optional[float] = None

    model: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["protocol_version"] = PROTOCOL_VERSION
        return d


@dataclass(frozen=True)
class RunEvent:
    """流式事件协议（SSE / JSON-lines）。"""

    run_id: str
    type: str
    data: Dict[str, Any]
    seq: int = 0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["protocol_version"] = PROTOCOL_VERSION
        return d


@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: RunStatus
    output_format: OutputFormat
    output: str
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["protocol_version"] = PROTOCOL_VERSION
        return d


@dataclass(frozen=True)
class ToolCall:
    """Structured tool invocation record for harness trace consumers."""

    tool_call_id: str
    tool: str
    input_hash: str
    status: str  # success / failed / denied
    duration_ms: int = 0
    output_hash: str = ""
    risk: str = "read_only"
    artifact_path: Optional[str] = None
    retry_count: int = 0
    approval_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["protocol_version"] = PROTOCOL_VERSION
        return d

    @classmethod
    def from_event(cls, payload: Dict[str, Any]) -> "ToolCall":
        return cls(
            tool_call_id=str(payload.get("event_id") or payload.get("tool_call_id") or ""),
            tool=str(payload.get("name") or payload.get("tool") or ""),
            input_hash=str(payload.get("input_hash") or ""),
            status=str(payload.get("status") or "unknown"),
            duration_ms=int(payload.get("duration_ms") or 0),
            output_hash=str(payload.get("output_hash") or ""),
            risk=str(payload.get("risk") or "read_only"),
            artifact_path=payload.get("artifact_path"),
            retry_count=int(payload.get("retry_count") or 0),
            approval_id=payload.get("approval_id"),
        )


@dataclass(frozen=True)
class AgentEvent:
    """Structured harness event for replay and audit."""

    event_id: str
    run_id: str
    event: str
    kind: str
    stage: str
    status: str
    timestamp: str
    data: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "event": self.event,
            "kind": self.kind,
            "stage": self.stage,
            "status": self.status,
            "timestamp": self.timestamp,
            "data": dict(self.data),
            "protocol_version": PROTOCOL_VERSION,
        }
        return d

    @classmethod
    def from_trace_payload(cls, payload: Dict[str, Any]) -> "AgentEvent":
        data = {k: v for k, v in payload.items()
                if k not in {"event_id", "run_id", "event", "kind", "stage", "status", "timestamp", "seq"}}
        return cls(
            event_id=str(payload.get("event_id") or ""),
            run_id=str(payload.get("run_id") or ""),
            event=str(payload.get("event") or ""),
            kind=str(payload.get("kind") or "runtime"),
            stage=str(payload.get("stage") or "observe"),
            status=str(payload.get("status") or "success"),
            timestamp=str(payload.get("timestamp") or ""),
            data=data,
        )
