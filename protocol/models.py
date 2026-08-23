from __future__ import annotations

import os
from dataclasses import dataclass, asdict, fields
from typing import Any, Dict, List, Literal, Optional

from .version import PROTOCOL_VERSION


RunStatus = Literal["queued", "running", "done", "error", "canceled"]
OutputFormat = Literal["json", "markdown", "text"]
EngineType = Literal["direct", "langchain", "langgraph"]
ScopeType = Literal["full", "gen_prompt_only", "parse_stack_only", "parse_log_only"]
PromptModeType = Literal["analysis", "fix"]
DEFAULT_PROMPT_MODE: PromptModeType = "fix"
AgentLoopType = Literal["single", "context_loop"]
LlmModeType = Literal["fixed", "auto"]
LlmProfileType = Literal["default", "strong", "fast"]


def normalize_run_code_roots(req: "RunRequest") -> List[str]:
    """
    解析 RunRequest 中的代码根目录列表。
    - 优先使用 code_roots（可多根，顺序有意义）；
    - 若未提供则回退到单独的 code_root（兼容旧客户端）。
    返回去重后的绝对路径列表（保持顺序）。
    """
    roots: List[str] = []
    crs = getattr(req, "code_roots", None)
    if crs:
        roots.extend(str(r).strip() for r in crs if r and str(r).strip())
    if not roots and req.code_root and str(req.code_root).strip():
        roots.append(str(req.code_root).strip())
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
    兼容旧客户端 engine=sequential → direct。
    """
    payload = dict(data or {})
    # 旧字段归并（与 daemon _start_run 一致，便于单测复用）
    if "scope" not in payload:
        if bool(payload.get("parse_stack_only", False)):
            payload["scope"] = "parse_stack_only"
        elif bool(payload.get("skip_ai", False)):
            payload["scope"] = "gen_prompt_only"
    payload.pop("skip_ai", None)
    payload.pop("parse_stack_only", None)

    engine = payload.get("engine")
    if engine == "sequential":
        payload["engine"] = "direct"

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
    code_root: Optional[str] = None  # 单根（兼容旧协议）；与 code_roots 二选一或并存时以 code_roots 为准
    code_roots: Optional[List[str]] = None  # 多根，顺序 = 查找优先级
    config: Optional[str] = None  # 配置文件路径

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
    code_context_timeout_sec: Optional[float] = None
    find_source_timeout_sec: Optional[float] = None

    # 兼容旧客户端字段：当前 CLI 已无 --consultation，daemon 忽略这些键
    consultation: bool = False
    prompt: Optional[str] = None

    model: Optional[str] = None
    engine: EngineType = "direct"

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
