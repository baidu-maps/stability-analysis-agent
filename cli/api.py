#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
稳定、可编程调用的 CLI 能力接口。

用途：
- 闭源/企业包装器可自定义交互菜单，再调用本模块执行与开源 `sa-agent` 相同的分析链路。
- 避免复制 `cli/main.py` 或依赖 subprocess 转发。

推荐用法（自定义菜单后执行）::

    from cli.api import build_parser, execute_analysis, interactive_state_to_argv

    state = {...}  # crash_log, library_dir, code_roots, engine, scope
    parser = build_parser()
    args = parser.parse_args(interactive_state_to_argv(state))
    raise SystemExit(execute_analysis(args))

或一行::

    from cli.api import run_from_interactive_state
    raise SystemExit(run_from_interactive_state(state))
"""

from __future__ import annotations

import argparse
from typing import Any, Dict, List, Optional

from cli.main import (
    build_parser,
    collect_interactive_run_state,
    execute_analysis,
    main as run_cli_main,
)

__all__ = [
    "build_parser",
    "collect_interactive_run_state",
    "execute_analysis",
    "interactive_state_to_argv",
    "parse_analysis_args",
    "run_from_interactive_state",
    "run_cli_main",
]


_VALID_SCOPES = {"full", "gen_prompt_only", "parse_stack_only", "parse_log_only"}


def _resolve_state_scope(state: Dict[str, Any]) -> str:
    """Resolve scope from the current interactive state contract."""
    raw_scope = str(state.get("scope") or "").strip()
    if raw_scope in _VALID_SCOPES:
        return raw_scope
    return "full"


def interactive_state_to_argv(state: Dict[str, Any]) -> List[str]:
    """
    将交互采集得到的 state 转为与 `sa-agent` 等价的 argv 片段（不含程序名）。

    state 键与 `collect_interactive_run_state()` 返回值一致：
    crash_log, library_dir, code_roots, engine, scope
    """
    argv: List[str] = [
        "--crash-log-file",
        str(state["crash_log"]),
        "--engine",
        str(state.get("engine") or "direct"),
        "--no-interactive",
    ]
    lib = str(state.get("library_dir") or "").strip()
    if lib:
        argv.extend(["--library-dir", lib])
    for root in state.get("code_roots") or []:
        if root and str(root).strip():
            argv.extend(["--code-roots", str(root)])
    scope = _resolve_state_scope(state)
    if scope != "full":
        argv.extend(["--scope", scope])
    prompt_mode = str(state.get("prompt_mode") or "").strip()
    if prompt_mode in {"analysis", "fix"} and prompt_mode != "fix":
        argv.extend(["--prompt-mode", prompt_mode])
    if "agent_loop" in state:
        agent_loop = str(state.get("agent_loop") or "").strip()
        if agent_loop in {"single", "context_loop"}:
            argv.extend(["--agent-loop", agent_loop])
    if state.get("max_agent_rounds") is not None:
        argv.extend(["--max-agent-rounds", str(state.get("max_agent_rounds"))])
    if state.get("max_context_requests_per_round") is not None:
        argv.extend(
            [
                "--max-context-requests-per-round",
                str(state.get("max_context_requests_per_round")),
            ]
        )
    return argv


def parse_analysis_args(
    argv: Optional[List[str]] = None,
    *,
    strip_run_prefix: bool = True,
) -> argparse.Namespace:
    """
    仅解析分析相关扁平参数（等价于 `sa-agent run ...` 去掉子命令后的部分）。

    - strip_run_prefix: 若 argv 首项为 ``run`` 则跳过（与 `main()` 行为一致）。
    """
    raw = list(argv if argv is not None else [])
    if strip_run_prefix and raw and raw[0] == "run":
        raw = raw[1:]
    parser = build_parser()
    return parser.parse_args(raw)


def run_from_interactive_state(state: Dict[str, Any]) -> int:
    """由交互 state 构建参数并执行完整分析，返回进程退出码。"""
    parser = build_parser()
    args = parser.parse_args(interactive_state_to_argv(state))
    return execute_analysis(args)
