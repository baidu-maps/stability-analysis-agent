from __future__ import annotations

import os
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Literal, Optional

from .version import PROTOCOL_VERSION


RunStatus = Literal["queued", "running", "done", "error", "canceled"]
OutputFormat = Literal["json", "markdown", "text"]
EngineType = Literal["sequential", "langgraph"]
ScopeType = Literal["full", "gen_prompt_only", "parse_stack_only", "parse_log_only"]
PromptModeType = Literal["analysis", "fix"]
AgentLoopType = Literal["single", "context_loop"]


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


@dataclass(frozen=True)
class RunRequest:
    """daemon/CLI 的统一输入协议（最小集）。"""

    crash_log: Optional[str] = None  # 文件路径；或 "-" 表示 stdin
    crash_log_content: Optional[str] = None  # 直接传内容（daemon 会走 stdin）
    library_dir: Optional[str] = None
    code_root: Optional[str] = None  # 单根（兼容旧协议）；与 code_roots 二选一或并存时以 code_roots 为准
    code_roots: Optional[List[str]] = None  # 多根，顺序 = 查找优先级
    config: Optional[str] = None  # 配置文件路径

    output_format: OutputFormat = "markdown"

    scope: ScopeType = "full"
    prompt_mode: PromptModeType = "analysis"
    agent_loop: Optional[AgentLoopType] = None  # None=随 prompt_mode 决定（analysis→context_loop）
    max_agent_rounds: int = 1
    max_context_requests_per_round: int = 5
    optimized: bool = False
    streaming: bool = False

    consultation: bool = False
    prompt: Optional[str] = None

    model: Optional[str] = None
    engine: EngineType = "sequential"

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


