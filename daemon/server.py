#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地 daemon（HTTP）：
- 负责任务 run_id 管理、启动/取消、流式事件输出
- 通过调用 CLI（tools/cli/main.py）复用核心能力

不依赖任何第三方 Web 框架，便于本地落地与后续迁移到服务端。
"""

from __future__ import annotations

import argparse
import contextvars
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLI_ENTRY = PROJECT_ROOT / "cli" / "main.py"
WEB_ROOT = PROJECT_ROOT / "web"
DEFAULT_REPORT_DIR = PROJECT_ROOT / "reports"

# 允许从任意 cwd 直接运行：python3 daemon/server.py
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT.parent))

from protocol.models import (
    RunEvent,
    RunRequest,
    RunResult,
    normalize_run_code_roots,
    run_request_from_dict,
)
from protocol.version import PROTOCOL_VERSION


# ---------------------------------------------------------------------------
# Tool System executor（用于 /tool-system/* 端点）
# ---------------------------------------------------------------------------
_ts_executor = None
_ts_lock = threading.Lock()
_worktree_setup_lock = threading.Lock()


def _get_ts_executor():
    """延迟初始化 tool_system ConfigDrivenExecutor（仅在首次调用 /tool-system/* 时触发）。"""
    global _ts_executor
    if _ts_executor is not None:
        return _ts_executor
    with _ts_lock:
        if _ts_executor is not None:
            return _ts_executor
        try:
            from tool_system import (
                ToolAndWorkflowRegistry, SystemConfig, LLMConfig,
                ToolConfig, WorkflowConfig, ConfigDrivenExecutor,
                LLMAdapterFactory, register_all_tools_and_workflows,
            )
            registry = ToolAndWorkflowRegistry()
            register_all_tools_and_workflows(registry)
            config = SystemConfig(
                tools=[
                    ToolConfig(name="crash_log_parser", enabled=True),
                    ToolConfig(name="add2line_resolver", enabled=True),
                    ToolConfig(name="code_content_provider", enabled=True),
                ],
                workflows=[
                    WorkflowConfig(name="crash_analysis", enabled=True),
                    WorkflowConfig(name="anr_freeze_analysis", enabled=True),
                ],
            )
            llm_adapter = None
            api_key = os.environ.get("WENXIN_API_KEY") or os.environ.get("OPENAI_API_KEY")
            if api_key:
                try:
                    llm_cfg = {
                        "engine": "direct",
                        "provider": "openai",
                        "model": os.environ.get("OPENAI_MODEL", "glm-4"),
                        "api_key": api_key,
                        "base_url": os.environ.get("OPENAI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"),
                    }
                    llm_adapter = LLMAdapterFactory.create(llm_cfg)
                    config.llm = LLMConfig(**llm_cfg)
                except Exception:
                    pass
            _ts_executor = ConfigDrivenExecutor(registry, config, llm_adapter)
        except Exception as e:
            raise RuntimeError(f"tool_system 初始化失败: {e}") from e
    return _ts_executor


@dataclass
class RunState:
    run_id: str
    status: str  # queued/running/done/error/canceled
    created_at: float
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    exit_code: Optional[int] = None
    error: Optional[str] = None
    output_format: str = "markdown"
    report_dir: Optional[str] = None
    workspace_dir: Optional[str] = None
    original_code_roots: List[str] = field(default_factory=list)
    isolated_code_roots: List[str] = field(default_factory=list)
    workspace_manifest: Optional[str] = None
    patch_path: Optional[str] = None
    last_progress: Optional[str] = None

    process: Optional[subprocess.Popen] = None
    events: "queue.Queue[RunEvent]" = field(default_factory=queue.Queue)
    result: Optional[RunResult] = None


class RunManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: Dict[str, RunState] = {}

    def create_run(self, req: RunRequest) -> RunState:
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        st = RunState(
            run_id=run_id,
            status="queued",
            created_at=time.time(),
            output_format=req.output_format,
        )
        with self._lock:
            self._runs[run_id] = st
        return st

    def get(self, run_id: str) -> Optional[RunState]:
        with self._lock:
            return self._runs.get(run_id)

    def list(self) -> Dict[str, RunState]:
        with self._lock:
            return dict(self._runs)

    def discard(self, run_id: str) -> None:
        """Drop a run that never entered the worker queue."""
        with self._lock:
            self._runs.pop(run_id, None)


RUNS = RunManager()


def _env_int(name: str, default: int) -> int:
    """Read a non-negative int from the environment."""
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


class SchedulerBusy(Exception):
    """Raised when the in-memory run queue is full."""

    def __init__(self, queued: int, max_queue: int) -> None:
        self.queued = int(queued)
        self.max_queue = int(max_queue)
        super().__init__(f"run queue is full ({self.queued}/{self.max_queue})")


@dataclass
class _QueuedJob:
    run: RunState
    req: RunRequest
    context: contextvars.Context


class RunScheduler:
    """Bounded worker pool: POST /runs enqueues, workers execute CLI subprocesses."""

    def __init__(
        self,
        *,
        max_workers: int = 2,
        max_queue: int = 32,
        run_timeout_sec: int = 0,
    ) -> None:
        self.max_workers = max(1, int(max_workers))
        self.max_queue = max(1, int(max_queue))
        self.run_timeout_sec = max(0, int(run_timeout_sec))
        self._jobs: "queue.Queue[Optional[_QueuedJob]]" = queue.Queue()
        self._pending = 0
        self._active = 0
        self._lock = threading.Lock()
        self._started = False

    def start(self) -> None:
        """Start worker threads once."""
        if self._started:
            return
        self._started = True
        for index in range(self.max_workers):
            threading.Thread(
                target=self._loop,
                name=f"sa-daemon-worker-{index}",
                daemon=True,
            ).start()

    def snapshot(self) -> Dict[str, int]:
        """Return queue counters for /health and GET /runs."""
        with self._lock:
            return {
                "queued": int(self._pending),
                "running": int(self._active),
                "max_workers": int(self.max_workers),
                "max_queue": int(self.max_queue),
                "run_timeout_sec": int(self.run_timeout_sec),
            }

    def submit(self, run: RunState, req: RunRequest) -> None:
        """Enqueue a run; copy ContextVar bindings from the HTTP thread."""
        ctx = contextvars.copy_context()
        with self._lock:
            if self._pending >= self.max_queue:
                raise SchedulerBusy(self._pending, self.max_queue)
            self._pending += 1
        self._jobs.put(_QueuedJob(run=run, req=req, context=ctx))

    def _loop(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            with self._lock:
                self._pending = max(0, self._pending - 1)
            run = job.run
            if run.status == "canceled":
                self._jobs.task_done()
                continue
            with self._lock:
                self._active += 1
            try:
                job.context.run(_run_worker, run, job.req)
            finally:
                with self._lock:
                    self._active = max(0, self._active - 1)
                self._jobs.task_done()


SCHEDULER: Optional[RunScheduler] = None
_SCHEDULER_LOCK = threading.Lock()


def ensure_run_scheduler(
    *,
    max_workers: Optional[int] = None,
    max_queue: Optional[int] = None,
    run_timeout_sec: Optional[int] = None,
) -> RunScheduler:
    """Create the process-wide worker pool on first use."""
    global SCHEDULER
    with _SCHEDULER_LOCK:
        if SCHEDULER is None:
            SCHEDULER = RunScheduler(
                max_workers=(
                    int(max_workers)
                    if max_workers is not None
                    else _env_int("STABILITY_AGENT_DAEMON_MAX_WORKERS", 2)
                ),
                max_queue=(
                    int(max_queue)
                    if max_queue is not None
                    else _env_int("STABILITY_AGENT_DAEMON_MAX_QUEUE", 32)
                ),
                run_timeout_sec=(
                    int(run_timeout_sec)
                    if run_timeout_sec is not None
                    else _env_int("STABILITY_AGENT_DAEMON_RUN_TIMEOUT", 0)
                ),
            )
            SCHEDULER.start()
        return SCHEDULER


def reset_run_scheduler_for_tests(
    *,
    max_workers: int = 2,
    max_queue: int = 32,
    run_timeout_sec: int = 0,
) -> RunScheduler:
    """Replace the process-wide pool. Test-only; in-flight jobs on the old pool are abandoned."""
    global SCHEDULER
    with _SCHEDULER_LOCK:
        old = SCHEDULER
        SCHEDULER = RunScheduler(
            max_workers=max_workers,
            max_queue=max_queue,
            run_timeout_sec=run_timeout_sec,
        )
        SCHEDULER.start()
        if old is not None:
            for _ in range(old.max_workers):
                old._jobs.put(None)
        return SCHEDULER


def _send_cors_headers(handler: BaseHTTPRequestHandler) -> None:
    """Allow intranet digital-employee / browser clients to call the daemon."""
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Access-Control-Max-Age", "600")


def _json_response(
    handler: BaseHTTPRequestHandler,
    code: int,
    payload: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    _send_cors_headers(handler)
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(data)


def _read_json_body(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(length) if length > 0 else b"{}"
    return json.loads(raw.decode("utf-8") or "{}")


_CLI_ANALYSIS_DEFAULTS = None


def _cli_analysis_defaults():
    """CLI `build_parser().parse_args([])` 的默认值，保证 daemon 省略旗标时与直接跑 CLI 一致。"""
    global _CLI_ANALYSIS_DEFAULTS
    if _CLI_ANALYSIS_DEFAULTS is None:
        from cli.main import build_parser

        _CLI_ANALYSIS_DEFAULTS = build_parser().parse_args([])
    return _CLI_ANALYSIS_DEFAULTS


def _append_nondefault_value(cmd: list, flag: str, value: Any, default: Any) -> None:
    if value is None:
        return
    if default is not None and value == default:
        return
    cmd += [flag, str(value)]


def _append_store_true(cmd: list, flag: str, value: bool, default: bool) -> None:
    if bool(value) and not bool(default):
        cmd += [flag]


def _build_cli_cmd(req: RunRequest) -> Tuple[list, Optional[str]]:
    """
    将 RunRequest 转成 CLI 命令行参数。
    返回 (cmd, stdin_text)

    与 CLI argparse 默认值对齐：请求未给或值等于 CLI 默认时不拼该旗标，
    让子进程走 `cli/main.py` 的默认逻辑。daemon 仅额外固定
    `--no-interactive --no-save-to-vector-db`（HTTP 非交互 + 写库走独立 API）。
    """
    cmd = [
        "python3",
        "-u",
        str(CLI_ENTRY),
    ]

    stdin_text: Optional[str] = None
    defaults = _cli_analysis_defaults()

    # analysis 模式：crash_log_dir 优先；否则 file / content
    crash_log_dir = (getattr(req, "crash_log_dir", None) or "").strip()
    if crash_log_dir:
        cmd += ["--crash-log-dir", crash_log_dir]
    elif req.crash_log_content is not None:
        cmd += ["--crash-log-file", "-"]
        stdin_text = req.crash_log_content
    elif req.crash_log:
        cmd += ["--crash-log-file", req.crash_log]
    else:
        # 交给 CLI 自己报错
        cmd += ["--crash-log-file", ""]

    if req.library_dir:
        cmd += ["--library-dir", req.library_dir]
    for cr in normalize_run_code_roots(req):
        cmd += ["--code-root", cr]
    if req.config:
        cmd += ["--config", req.config]

    output_format = req.output_format or getattr(defaults, "output_format", "markdown")
    if output_format != getattr(defaults, "output_format", "markdown"):
        cmd += ["--output-format", output_format]

    engine = str(getattr(req, "engine", None) or "direct")
    if engine == "sequential":
        engine = "direct"
    default_engine = str(getattr(defaults, "engine", "direct") or "direct")
    if engine and engine != default_engine:
        cmd += ["--engine", engine]

    scope = str(getattr(req, "scope", "full") or "full")
    default_scope = str(getattr(defaults, "scope", "full") or "full")
    if scope and scope != default_scope:
        cmd += ["--scope", scope]
    prompt_mode = str(getattr(req, "prompt_mode", "fix") or "fix")
    default_prompt_mode = str(getattr(defaults, "prompt_mode", "fix") or "fix")
    if prompt_mode and prompt_mode != default_prompt_mode:
        cmd += ["--prompt-mode", prompt_mode]
    agent_loop = getattr(req, "agent_loop", None)
    if agent_loop in {"single", "context_loop"}:
        cmd += ["--agent-loop", str(agent_loop)]

    default_max_rounds = int(getattr(defaults, "max_agent_rounds", 0) or 0)
    if req.max_agent_rounds is not None and int(req.max_agent_rounds) != default_max_rounds:
        cmd += ["--max-agent-rounds", str(int(req.max_agent_rounds))]
    default_max_ctx = int(getattr(defaults, "max_context_requests_per_round", 5) or 5)
    if (
        req.max_context_requests_per_round is not None
        and int(req.max_context_requests_per_round) != default_max_ctx
    ):
        cmd += ["--max-context-requests-per-round", str(int(req.max_context_requests_per_round))]

    _append_store_true(cmd, "--optimized", bool(req.optimized), bool(getattr(defaults, "optimized", False)))
    if req.streaming is True:
        cmd += ["--streaming"]
    elif req.streaming is False:
        cmd += ["--no-streaming"]

    if bool(getattr(req, "apply_ai_fixes", True)) != bool(getattr(defaults, "apply_ai_fixes", True)):
        cmd += ["--apply-ai-fixes" if req.apply_ai_fixes else "--no-apply-ai-fixes"]
    if bool(getattr(req, "backup_original_sources", True)) != bool(
        getattr(defaults, "backup_original_sources", True)
    ):
        cmd += [
            "--backup-original-sources"
            if req.backup_original_sources
            else "--no-backup-original-sources"
        ]

    _append_store_true(
        cmd, "--force-disassembly", bool(req.force_disassembly), bool(getattr(defaults, "force_disassembly", False))
    )
    _append_store_true(
        cmd,
        "--force-anr-analysis",
        bool(req.force_anr_analysis),
        bool(getattr(defaults, "force_anr_analysis", False)),
    )
    _append_store_true(
        cmd,
        "--force-memory-analysis",
        bool(req.force_memory_analysis),
        bool(getattr(defaults, "force_memory_analysis", False)),
    )
    _append_store_true(
        cmd,
        "--force-timeline-analysis",
        bool(req.force_timeline_analysis),
        bool(getattr(defaults, "force_timeline_analysis", False)),
    )

    native_leak_dir = getattr(req, "native_leak_dir", None)
    if native_leak_dir:
        cmd += ["--native-leak-dir", str(native_leak_dir)]
    native_leak_trace_db = getattr(req, "native_leak_trace_db", None)
    if native_leak_trace_db:
        cmd += ["--native-leak-trace-db", str(native_leak_trace_db)]

    llm_mode = getattr(req, "llm_mode", None)
    if llm_mode in {"fixed", "auto"}:
        cmd += ["--llm-mode", str(llm_mode)]
    llm_profile = getattr(req, "llm_profile", None)
    if llm_profile in {"default", "strong", "fast"}:
        cmd += ["--llm-profile", str(llm_profile)]
    if bool(getattr(req, "include_memory_in_05", False)) != bool(
        getattr(defaults, "include_memory_in_05", False)
    ):
        cmd += ["--include-memory-in-05" if req.include_memory_in_05 else "--no-include-memory-in-05"]

    _append_nondefault_value(cmd, "--vector-db-path", req.vector_db_path, getattr(defaults, "vector_db_path", None))
    _append_nondefault_value(
        cmd,
        "--vector-db-max-results",
        req.vector_db_max_results,
        getattr(defaults, "vector_db_max_results", None),
    )
    _append_store_true(
        cmd,
        "--vector-db-record-usage",
        bool(req.vector_db_record_usage),
        bool(getattr(defaults, "vector_db_record_usage", False)),
    )
    _append_nondefault_value(
        cmd,
        "--rule-confidence-threshold",
        req.rule_confidence_threshold,
        getattr(defaults, "rule_confidence_threshold", None),
    )
    _append_store_true(
        cmd, "--use-ctags-index", bool(req.use_ctags_index), bool(getattr(defaults, "use_ctags_index", False))
    )
    _append_nondefault_value(
        cmd,
        "--max-sibling-member-functions",
        req.max_sibling_member_functions,
        getattr(defaults, "max_sibling_member_functions", None),
    )
    _append_nondefault_value(
        cmd,
        "--max-stack-frames-symbol-enrich",
        req.max_stack_frames_symbol_enrich,
        getattr(defaults, "max_stack_frames_symbol_enrich", None),
    )
    _append_nondefault_value(
        cmd,
        "--max-stack-frames-in-prompt",
        req.max_stack_frames_in_prompt,
        getattr(defaults, "max_stack_frames_in_prompt", None),
    )
    _append_nondefault_value(
        cmd,
        "--max-shared-var-related-functions",
        req.max_shared_var_related_functions,
        getattr(defaults, "max_shared_var_related_functions", None),
    )
    _append_nondefault_value(
        cmd,
        "--min-key-read-related-functions",
        req.min_key_read_related_functions,
        getattr(defaults, "min_key_read_related_functions", None),
    )
    _append_nondefault_value(
        cmd,
        "--code-context-timeout-sec",
        req.code_context_timeout_sec,
        getattr(defaults, "code_context_timeout_sec", None),
    )
    _append_nondefault_value(
        cmd,
        "--find-source-timeout-sec",
        req.find_source_timeout_sec,
        getattr(defaults, "find_source_timeout_sec", None),
    )
    for module in req.plugin_modules or []:
        module_text = str(module or "").strip()
        if module_text:
            cmd += ["--plugin-module", module_text]

    cmd += ["--no-interactive", "--no-save-to-vector-db"]

    return cmd, stdin_text


def _persist_result(run: RunState) -> None:
    try:
        from cli.report_paths import ensure_reports_migrated

        ensure_reports_migrated(PROJECT_ROOT)
    except Exception:
        pass
    DEFAULT_REPORT_DIR.mkdir(exist_ok=True)
    suffix = {"json": "json", "markdown": "md", "text": "txt"}.get(run.output_format, "txt")
    out_path = DEFAULT_REPORT_DIR / f"{run.run_id}.{suffix}"
    try:
        if run.result is None:
            return
        out_path.write_text(run.result.output, encoding="utf-8")
        run.events.put(RunEvent(run.run_id, "artifact_written", {"path": str(out_path)}))
    except Exception as e:
        run.events.put(RunEvent(run.run_id, "artifact_write_error", {"error": str(e)}))


def _capture_report_dir_from_line(run: RunState, line: str) -> None:
    text = str(line or "").strip()
    if not text:
        return
    marker = "report 已保存到:"
    if marker in text:
        run.report_dir = text.split(marker, 1)[1].strip()
        return
    if "reports/" in text and "report" in text.lower():
        import re

        m = re.search(r"(reports/[^\s]+)", text)
        if m:
            run.report_dir = m.group(1).strip().rstrip("/")


def _emit_stdout_line(run: RunState, stdout_acc: list[str], line: str) -> None:
    text = line.rstrip("\n")
    stdout_acc.append(text + "\n")
    _capture_report_dir_from_line(run, text)
    _capture_progress(run, text)
    if text.startswith("AI_STREAM_DATA:"):
        payload_text = text[len("AI_STREAM_DATA:"):].strip()
        try:
            payload = json.loads(payload_text)
            run.events.put(
                RunEvent(
                    run.run_id,
                    "ai_stream",
                    {
                        "payload": payload,
                        "raw": text,
                    },
                )
            )
            return
        except Exception as exc:
            run.events.put(
                RunEvent(
                    run.run_id,
                    "stream_error",
                    {"which": "ai_stream", "error": str(exc), "raw": text},
                )
            )
    run.events.put(RunEvent(run.run_id, "stdout", {"line": text}))


def _subprocess_env_for_run(run_id: Optional[str] = None) -> dict:
    """子进程环境：透传 Web UI 中禁用的 skill 列表给 CLI。"""
    from daemon.web_preferences import load_web_preferences

    env = os.environ.copy()
    disabled = [str(x).strip() for x in (load_web_preferences().get("disabled_skills") or []) if str(x).strip()]
    if disabled:
        env["STABILITY_AGENT_DISABLED_SKILLS"] = ",".join(disabled)
    if run_id:
        env["STABILITY_AGENT_RUN_ID"] = run_id
    return env


def _prepare_isolated_run(run: RunState, req: RunRequest):
    code_roots = normalize_run_code_roots(req)
    should_isolate = bool(req.apply_ai_fixes) and str(req.scope or "full") == "full" and bool(code_roots)
    if not should_isolate:
        return req, None

    from services.git_worktree_manager import prepare_isolated_workspace

    # Git serializes parts of worktree administration through shared metadata.
    # Keep setup short and deterministic when several runs target the same repo.
    with _worktree_setup_lock:
        workspace = prepare_isolated_workspace(run.run_id, code_roots)
    run.workspace_dir = str(workspace.root)
    run.original_code_roots = list(workspace.original_code_roots)
    run.isolated_code_roots = list(workspace.isolated_code_roots)
    run.events.put(
        RunEvent(
            run.run_id,
            "workspace_prepared",
            workspace.to_dict(),
        )
    )
    isolated_req = replace(
        req,
        code_root=None,
        code_roots=list(workspace.isolated_code_roots),
    )
    return isolated_req, workspace


def _write_run_workspace_artifacts(run: RunState, workspace) -> None:
    from services.git_worktree_manager import write_workspace_artifacts

    if run.report_dir:
        report_dir = Path(run.report_dir).expanduser()
        if not report_dir.is_absolute():
            report_dir = PROJECT_ROOT / report_dir
        report_dir = report_dir.resolve()
    else:
        report_dir = (DEFAULT_REPORT_DIR / run.run_id).resolve()
        run.report_dir = str(report_dir)
    artifacts = write_workspace_artifacts(workspace, report_dir)
    run.workspace_manifest = artifacts.get("manifest_path")
    run.patch_path = artifacts.get("patch_path")
    run.events.put(
        RunEvent(
            run.run_id,
            "workspace_artifacts_written",
            {
                "workspace_dir": run.workspace_dir,
                "manifest_path": run.workspace_manifest,
                "patch_path": run.patch_path,
            },
        )
    )


def _run_worker(run: RunState, req: RunRequest) -> None:
    if run.status == "canceled":
        return
    run.status = "running"
    run.started_at = time.time()
    run.events.put(RunEvent(run.run_id, "run_started", {"request": req.to_dict()}))

    workspace = None
    try:
        effective_req, workspace = _prepare_isolated_run(run, req)
    except Exception as exc:
        run.status = "error"
        run.error = f"failed to prepare isolated Git worktree: {exc}"
        run.finished_at = time.time()
        run.result = RunResult(
            run_id=run.run_id,
            status="error",
            output_format=req.output_format,
            output="",
            error=run.error,
        )
        run.events.put(RunEvent(run.run_id, "workspace_error", {"error": run.error}))
        run.events.put(RunEvent(run.run_id, "run_finished", {"status": run.status, "exit_code": None}))
        return

    cmd, stdin_text = _build_cli_cmd(effective_req)
    run.events.put(RunEvent(run.run_id, "process_spawn", {"cmd": cmd, "cwd": str(PROJECT_ROOT)}))

    try:
        p = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.PIPE if stdin_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=_subprocess_env_for_run(run.run_id),
        )
        run.process = p

        if stdin_text is not None and p.stdin is not None:
            p.stdin.write(stdin_text)
            p.stdin.close()

        stdout_acc: list[str] = []

        def _pump_stdout(stream):
            try:
                for line in iter(stream.readline, ""):
                    if not line:
                        break
                    _emit_stdout_line(run, stdout_acc, line)
            except Exception as e:
                run.events.put(RunEvent(run.run_id, "stream_error", {"which": "stdout", "error": str(e)}))

        def _pump_stderr(stream):
            try:
                for line in iter(stream.readline, ""):
                    if not line:
                        break
                    _capture_report_dir_from_line(run, line)
                    _capture_progress(run, line.rstrip("\n"))
                    run.events.put(RunEvent(run.run_id, "stderr", {"line": line.rstrip("\n")}))
            except Exception as e:
                run.events.put(RunEvent(run.run_id, "stream_error", {"which": "stderr", "error": str(e)}))

        t_out = threading.Thread(target=_pump_stdout, args=(p.stdout,), daemon=True)
        t_err = threading.Thread(target=_pump_stderr, args=(p.stderr,), daemon=True)
        t_out.start()
        t_err.start()

        exit_code = _wait_cli_process(p, run)
        run.exit_code = exit_code
        run.finished_at = time.time()

        # Ensure report-dir discovery and all streamed output are complete before
        # generating workspace artifacts.
        t_out.join()
        t_err.join()

        # 读取剩余输出（保险）
        try:
            remaining_out = p.stdout.read() if p.stdout else ""
            if remaining_out:
                for line in remaining_out.splitlines():
                    _emit_stdout_line(run, stdout_acc, line + "\n")
        except Exception:
            pass

        status = "canceled" if run.status == "canceled" else ("done" if exit_code == 0 else "error")
        run.status = status

        output_text = "".join(stdout_acc)
        if run.status == "canceled":
            result_error = run.error or "canceled"
        elif exit_code == 0:
            result_error = None
        else:
            result_error = f"process exit {exit_code}"
        run.result = RunResult(
            run_id=run.run_id,
            status=run.status,
            output_format=req.output_format,
            output=output_text,
            error=result_error,
        )
        if workspace is not None:
            try:
                _write_run_workspace_artifacts(run, workspace)
            except Exception as exc:
                run.events.put(
                    RunEvent(run.run_id, "workspace_artifact_error", {"error": str(exc)})
                )
        _persist_result(run)
    except Exception as e:
        run.status = "error"
        run.error = str(e)
        run.finished_at = time.time()
        run.events.put(RunEvent(run.run_id, "run_error", {"error": str(e)}))
    finally:
        run.events.put(RunEvent(run.run_id, "run_finished", {"status": run.status, "exit_code": run.exit_code}))


def _wait_cli_process(process: subprocess.Popen, run: RunState) -> int:
    """Wait for the CLI subprocess, optionally enforcing a run timeout."""
    timeout = int(SCHEDULER.run_timeout_sec) if SCHEDULER is not None else 0
    if timeout <= 0:
        return int(process.wait())
    try:
        return int(process.wait(timeout=timeout))
    except subprocess.TimeoutExpired:
        run.status = "canceled"
        run.error = f"run timed out after {timeout}s"
        try:
            process.terminate()
        except Exception:
            pass
        time.sleep(0.2)
        if process.poll() is None:
            try:
                process.kill()
            except Exception:
                pass
        try:
            return int(process.wait(timeout=10))
        except subprocess.TimeoutExpired:
            return -15


_REPORT_MARKS = ("# 崩溃分析结果", "# Crash Analysis Result", "# Crash analysis")


def _capture_progress(run: RunState, text: str) -> None:
    """Keep the latest human-readable stage line for GET /status."""
    stripped = str(text or "").strip()
    if not stripped:
        return
    if (
        stripped.startswith("[阶段")
        or stripped.startswith("[sdk-release]")
        or stripped.startswith("ERROR:")
    ):
        run.last_progress = stripped[:500]


def _extract_report(output: str) -> str:
    """Strip SDK-status preface; keep the Markdown report body."""
    text = str(output or "")
    for mark in _REPORT_MARKS:
        index = text.find(mark)
        if index >= 0:
            return text[index:]
    return text


def _queue_position(run: RunState) -> Optional[int]:
    """1-based position among currently queued runs, or None if not queued."""
    if run.status != "queued":
        return None
    ahead = 0
    for other in RUNS.list().values():
        if other.status == "queued" and other.created_at <= run.created_at:
            ahead += 1
    return ahead


def _status_message(run: RunState) -> str:
    """Short Chinese summary for digital-employee polling."""
    if run.status == "queued":
        position = _queue_position(run)
        if position:
            return f"排队中（第 {position} 位）"
        return "排队等待执行"
    if run.status == "running":
        return run.last_progress or "分析进行中"
    if run.status == "done":
        return "分析完成"
    if run.status == "canceled":
        return run.error or "已取消"
    return run.error or "分析失败"


def _run_links(run_id: str) -> Dict[str, str]:
    """Public URLs for status / result / cancel (short aliases + canonical)."""
    return {
        "status": f"/status/{run_id}",
        "result": f"/result/{run_id}",
        "cancel": f"/cancel/{run_id}",
        "events": f"/runs/{run_id}/events",
    }


def _single_id_path(path: str, prefix: str) -> Optional[str]:
    """Return the id if path is exactly ``{prefix}{id}`` with one segment."""
    if not path.startswith(prefix):
        return None
    rest = path[len(prefix):].strip("/")
    if not rest or "/" in rest:
        return None
    return rest


def _run_public_dict(run: RunState) -> Dict[str, Any]:
    """JSON view of a run for GET /runs, GET /runs/<id> and GET /status/<id>."""
    return {
        "run_id": run.run_id,
        "status": run.status,
        "message": _status_message(run),
        "progress": run.last_progress,
        "queue_position": _queue_position(run),
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "exit_code": run.exit_code,
        "error": run.error,
        "output_format": run.output_format,
        "report_dir": run.report_dir,
        "workspace_dir": run.workspace_dir,
        "original_code_roots": run.original_code_roots,
        "isolated_code_roots": run.isolated_code_roots,
        "workspace_manifest": run.workspace_manifest,
        "patch_path": run.patch_path,
        "links": _run_links(run.run_id),
    }


def _result_public_dict(run: RunState) -> Dict[str, Any]:
    """JSON view of a finished run, including a cleaned ``report`` field."""
    payload = run.result.to_dict() if run.result is not None else {}
    output = str(payload.get("output") or "")
    payload["report"] = _extract_report(output)
    payload["links"] = _run_links(run.run_id)
    return payload


def _respond_status(handler: BaseHTTPRequestHandler, run_id: str) -> None:
    """GET /status/{id} and GET /runs/{id}."""
    run = RUNS.get(run_id)
    if not run:
        _json_response(handler, HTTPStatus.NOT_FOUND, {"error": "run_not_found"})
        return
    _json_response(handler, HTTPStatus.OK, _run_public_dict(run))


def _respond_result(handler: BaseHTTPRequestHandler, run_id: str) -> None:
    """GET /result/{id} and GET /runs/{id}/result."""
    run = RUNS.get(run_id)
    if not run:
        _json_response(handler, HTTPStatus.NOT_FOUND, {"error": "run_not_found"})
        return
    if not run.result:
        _json_response(
            handler,
            HTTPStatus.ACCEPTED,
            {
                "status": run.status,
                "message": _status_message(run),
                "progress": run.last_progress,
            },
        )
        return
    _json_response(handler, HTTPStatus.OK, _result_public_dict(run))


def _respond_cancel(handler: BaseHTTPRequestHandler, run_id: str) -> None:
    """POST /cancel/{id} and POST /runs/{id}/cancel."""
    run = RUNS.get(run_id)
    if not run:
        _json_response(handler, HTTPStatus.NOT_FOUND, {"error": "run_not_found"})
        return
    try:
        _cancel_run(run)
    except Exception as exc:
        _json_response(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
        return
    _json_response(
        handler,
        HTTPStatus.OK,
        {"run_id": run_id, "status": run.status, "message": _status_message(run)},
    )


def _scheduler_snapshot() -> Dict[str, int]:
    """Queue counters; start the pool lazily so /health works before the first POST."""
    return ensure_run_scheduler().snapshot()


def _cancel_run(run: RunState) -> None:
    """Cancel a queued job in-place, or terminate a running CLI subprocess."""
    if run.status == "queued":
        run.status = "canceled"
        run.finished_at = time.time()
        run.error = "canceled before start"
        run.result = RunResult(
            run_id=run.run_id,
            status="canceled",
            output_format=run.output_format,
            output="",
            error=run.error,
        )
        run.events.put(RunEvent(run.run_id, "run_canceled", {"phase": "queued"}))
        run.events.put(
            RunEvent(run.run_id, "run_finished", {"status": "canceled", "exit_code": None})
        )
        return
    if run.process and run.status == "running":
        run.process.terminate()
        time.sleep(0.2)
        if run.process.poll() is None:
            run.process.kill()
        run.status = "canceled"
        run.finished_at = time.time()
        run.events.put(RunEvent(run.run_id, "run_canceled", {"phase": "running"}))


def _start_run(req_dict: Dict[str, Any]) -> RunState:
    """Enqueue a run on the worker pool. Copies HTTP-thread ContextVars into the worker."""
    scheduler = ensure_run_scheduler()
    req = run_request_from_dict(req_dict)
    run = RUNS.create_run(req)
    try:
        scheduler.submit(run, req)
    except SchedulerBusy:
        RUNS.discard(run.run_id)
        raise
    return run


def _skill_manager():
    from skill_system.manager import SkillManager

    return SkillManager()


def _static_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".html": "text/html; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".ico": "image/x-icon",
        ".json": "application/json; charset=utf-8",
        ".md": "text/markdown; charset=utf-8",
    }.get(suffix, "application/octet-stream")


def _serve_web_static(handler: BaseHTTPRequestHandler, url_path: str) -> bool:
    """Serve files under web/. Returns True if handled."""
    raw = url_path.split("?", 1)[0]
    if raw in ("/", ""):
        rel = "index.html"
    elif raw.startswith("/"):
        rel = raw[1:]
    else:
        rel = raw

    if ".." in Path(rel).parts:
        return False

    candidate = (WEB_ROOT / rel).resolve()
    try:
        candidate.relative_to(WEB_ROOT.resolve())
    except ValueError:
        return False
    if not candidate.is_file():
        return False

    data = candidate.read_bytes()
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", _static_content_type(candidate))
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    handler.wfile.write(data)
    return True


def _handle_skills_get(handler: BaseHTTPRequestHandler, path: str) -> bool:
    if path == "/skills":
        try:
            manager = _skill_manager()
            _json_response(handler, HTTPStatus.OK, {"skills": _skills_with_prefs(manager)})
        except Exception as e:
            _json_response(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})
        return True

    if path.startswith("/skills/"):
        name = path[len("/skills/"):].strip("/")
        if not name or "/" in name:
            return False
        try:
            manager = _skill_manager()
            bundle = manager.resolve(name)
            payload = {
                "summary": bundle.to_summary().to_dict(),
                "frontmatter": bundle.frontmatter.to_dict(),
                "package": bundle.package.to_dict(),
                "body": bundle.body,
            }
            _json_response(handler, HTTPStatus.OK, payload)
        except KeyError as e:
            _json_response(handler, HTTPStatus.NOT_FOUND, {"error": str(e)})
        except Exception as e:
            _json_response(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})
        return True

    return False


def _handle_skills_post(handler: BaseHTTPRequestHandler, path: str) -> bool:
    if path == "/skills/install":
        try:
            body = _read_json_body(handler)
            source = body.get("source")
            if not source:
                _json_response(handler, HTTPStatus.BAD_REQUEST, {"error": "missing source"})
                return True
            manager = _skill_manager()
            result = manager.install_from_path(
                source,
                overwrite=bool(body.get("overwrite", False)),
            )
            _json_response(handler, HTTPStatus.OK, result.to_dict())
        except FileNotFoundError as e:
            _json_response(handler, HTTPStatus.NOT_FOUND, {"error": str(e)})
        except FileExistsError as e:
            _json_response(handler, HTTPStatus.CONFLICT, {"error": str(e)})
        except Exception as e:
            _json_response(handler, HTTPStatus.BAD_REQUEST, {"error": str(e)})
        return True

    if path == "/skills/lint":
        try:
            body = _read_json_body(handler)
            source = body.get("source")
            if not source:
                _json_response(handler, HTTPStatus.BAD_REQUEST, {"error": "missing source"})
                return True
            manager = _skill_manager()
            issues = [issue.to_dict() for issue in manager.lint(source)]
            _json_response(handler, HTTPStatus.OK, {"issues": issues})
        except FileNotFoundError as e:
            _json_response(handler, HTTPStatus.NOT_FOUND, {"error": str(e)})
        except Exception as e:
            _json_response(handler, HTTPStatus.BAD_REQUEST, {"error": str(e)})
        return True

    if path == "/skills/uninstall":
        try:
            body = _read_json_body(handler)
            name = body.get("name")
            if not name:
                _json_response(handler, HTTPStatus.BAD_REQUEST, {"error": "missing name"})
                return True
            manager = _skill_manager()
            ok = manager.uninstall(str(name))
            if not ok:
                _json_response(handler, HTTPStatus.NOT_FOUND, {"error": f"skill not found: {name}"})
                return True
            _json_response(handler, HTTPStatus.OK, {"ok": True, "name": name})
        except Exception as e:
            _json_response(handler, HTTPStatus.BAD_REQUEST, {"error": str(e)})
        return True

    return False


def _handle_web_get(handler: BaseHTTPRequestHandler, path: str) -> bool:
    if path == "/web/preferences":
        try:
            from daemon.web_preferences import load_web_preferences

            _json_response(handler, HTTPStatus.OK, load_web_preferences())
        except Exception as e:
            _json_response(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})
        return True
    return False


def _handle_web_post(handler: BaseHTTPRequestHandler, path: str) -> bool:
    if path == "/web/preferences":
        try:
            from daemon.web_preferences import load_web_preferences, save_web_preferences, toggle_skill

            body = _read_json_body(handler)
            if "skill" in body and "enabled" in body:
                prefs = toggle_skill(str(body["skill"]), enabled=bool(body["enabled"]))
            else:
                prefs = save_web_preferences(body)
            _json_response(handler, HTTPStatus.OK, prefs)
        except Exception as e:
            _json_response(handler, HTTPStatus.BAD_REQUEST, {"error": str(e)})
        return True
    return False


def _skills_with_prefs(manager) -> list:
    from daemon.web_preferences import load_web_preferences

    disabled = set(load_web_preferences().get("disabled_skills") or [])
    out = []
    for summary in manager.summaries_installed_as_dicts():
        item = dict(summary)
        name = str(item.get("command_name") or item.get("display_name") or "")
        item["enabled"] = name not in disabled
        out.append(item)
    return out


def _resolve_report_dir(raw: Optional[str]) -> Optional[Path]:
    if not raw:
        return None
    p = Path(str(raw).strip()).expanduser()
    if not p.is_absolute():
        p = (PROJECT_ROOT / p).resolve()
    return p if p.is_dir() else None


def _handle_run_vector_db_commit(handler: BaseHTTPRequestHandler, path: str) -> bool:
    prefix = "/runs/"
    suffix = "/vector-db/commit"
    if not path.startswith(prefix) or not path.endswith(suffix):
        return False
    middle = path[len(prefix): -len(suffix)].strip("/")
    if not middle or "/" in middle:
        return False
    run_id = middle
    run = RUNS.get(run_id)
    if not run:
        _json_response(handler, HTTPStatus.NOT_FOUND, {"error": "run_not_found"})
        return True
    if run.status not in ("done", "error"):
        _json_response(handler, HTTPStatus.CONFLICT, {"error": "run_not_finished", "status": run.status})
        return True
    report_dir = _resolve_report_dir(run.report_dir)
    if report_dir is None:
        _json_response(handler, HTTPStatus.BAD_REQUEST, {"error": "report_dir_missing"})
        return True
    try:
        from daemon.web_preferences import load_web_preferences
        from rag.case_writer import commit_from_report_dir, write_commit_audit
        from rag.vector_store_config import VectorStoreNotImplementedError

        prefs = load_web_preferences()
        result = commit_from_report_dir(
            report_dir,
            vector_db_config=prefs.get("vector_db"),
        )
        audit_status = "committed" if result.get("ok") else ("skipped" if result.get("skipped") else "failed")
        write_commit_audit(report_dir, {"status": audit_status, "result": result, "source": "web"})
        if result.get("ok"):
            _json_response(handler, HTTPStatus.OK, result)
        elif result.get("skipped"):
            _json_response(handler, HTTPStatus.BAD_REQUEST, result)
        else:
            code = HTTPStatus.NOT_IMPLEMENTED if "not implemented" in str(result.get("error", "")).lower() else HTTPStatus.BAD_REQUEST
            _json_response(handler, code, result)
    except VectorStoreNotImplementedError as exc:
        _json_response(handler, HTTPStatus.NOT_IMPLEMENTED, {"ok": False, "error": str(exc)})
    except Exception as exc:
        _json_response(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
    return True


class Handler(BaseHTTPRequestHandler):
    server_version = "AIStabilityDaemon/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]

        if path == "/health":
            payload = {
                "ok": True,
                "service": "stability-analysis-agent",
                "protocol_version": PROTOCOL_VERSION,
                "pid": os.getpid(),
                "web_ui": True,
            }
            payload.update(_scheduler_snapshot())
            _json_response(self, HTTPStatus.OK, payload)
            return

        if _handle_skills_get(self, path):
            return

        if _handle_web_get(self, path):
            return

        if path in ("/tool-system/tools", "/tool-system/workflows"):
            try:
                executor = _get_ts_executor()
                active = executor.list_active()
                _json_response(self, HTTPStatus.OK, active)
            except Exception as e:
                _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})
            return

        if path == "/runs":
            items = [_run_public_dict(run) for run in RUNS.list().values()]
            items.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)
            payload = {"runs": items}
            payload.update(_scheduler_snapshot())
            _json_response(self, HTTPStatus.OK, payload)
            return

        if path.startswith("/runs/") and path.endswith("/events"):
            run_id = path.split("/")[2]
            run = RUNS.get(run_id)
            if not run:
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "run_not_found"})
                return

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            _send_cors_headers(self)
            self.end_headers()

            hello = RunEvent(run_id, "events_opened", {}).to_dict()
            self.wfile.write(f"data: {json.dumps(hello, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

            while True:
                try:
                    ev = run.events.get(timeout=1.0)
                    self.wfile.write(
                        f"data: {json.dumps(ev.to_dict(), ensure_ascii=False)}\n\n".encode("utf-8")
                    )
                    self.wfile.flush()
                except queue.Empty:
                    if run.status in ("done", "error", "canceled"):
                        break
                    keep = RunEvent(run_id, "keepalive", {}).to_dict()
                    self.wfile.write(f"data: {json.dumps(keep, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
            return

        result_id = _single_id_path(path, "/result/")
        if result_id:
            _respond_result(self, result_id)
            return

        status_id = _single_id_path(path, "/status/")
        if status_id:
            _respond_status(self, status_id)
            return

        cancel_id = _single_id_path(path, "/cancel/")
        if cancel_id:
            _json_response(
                self,
                HTTPStatus.METHOD_NOT_ALLOWED,
                {"error": "method_not_allowed", "hint": f"POST /cancel/{cancel_id}"},
            )
            return

        if path.startswith("/runs/") and path.endswith("/result"):
            run_id = path.split("/")[2]
            _respond_result(self, run_id)
            return

        if path.startswith("/runs/"):
            run_id = path.split("/")[2]
            _respond_status(self, run_id)
            return

        if _serve_web_static(self, path):
            return

        _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        _send_cors_headers(self)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]

        if path == "/runs":
            try:
                body = _read_json_body(self)
                run = _start_run(body)
                links = _run_links(run.run_id)
                _json_response(
                    self,
                    HTTPStatus.OK,
                    {
                        "run_id": run.run_id,
                        "status": run.status,
                        "message": _status_message(run),
                        "links": links,
                    },
                )
            except SchedulerBusy as exc:
                _json_response(
                    self,
                    HTTPStatus.TOO_MANY_REQUESTS,
                    {
                        "error": "queue_full",
                        "queued": exc.queued,
                        "max_queue": exc.max_queue,
                    },
                    headers={"Retry-After": "5"},
                )
            except Exception as e:
                _json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(e)})
            return

        cancel_id = _single_id_path(path, "/cancel/")
        if cancel_id:
            _respond_cancel(self, cancel_id)
            return

        if path.startswith("/runs/") and path.endswith("/cancel"):
            run_id = path.split("/")[2]
            _respond_cancel(self, run_id)
            return

        if _handle_run_vector_db_commit(self, path):
            return

        if _handle_skills_post(self, path):
            return

        if _handle_web_post(self, path):
            return

        if path == "/tool-system/analyze":
            try:
                body = _read_json_body(self)
                executor = _get_ts_executor()
                crash_log = body.get("crash_log", "")
                force_anr = bool(body.get("force_anr_analysis") or body.get("force_anr"))
                try:
                    from tools.crash_parser.log_kind_classifier import (
                        classify_log_kind,
                        workflow_name_for_log_kind,
                    )
                    workflow_name = workflow_name_for_log_kind(
                        classify_log_kind(crash_log).log_kind,
                        force_anr=force_anr,
                    )
                except Exception:
                    workflow_name = "anr_freeze_analysis" if force_anr else "crash_analysis"
                result = executor.execute_workflow(workflow_name, {
                    "crash_log": crash_log,
                    "library_dir": body.get("library_dir"),
                    "code_roots": body.get("code_roots", []),
                    "force_anr_analysis": force_anr,
                    "scope": body.get("scope", "full"),
                })
                _json_response(self, HTTPStatus.OK, result)
            except Exception as e:
                _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})
            return

        if path == "/tool-system/native-leak":
            try:
                body = _read_json_body(self)
                executor = _get_ts_executor()
                result = executor.execute_workflow("native_leak_analysis", {
                    "native_leak_path": body.get("path") or body.get("native_leak_dir"),
                    "native_leak_trace_db": body.get("trace_db") or body.get("native_leak_trace_db"),
                    "code_roots": body.get("code_roots", []),
                    "scope": body.get("scope", "gen_prompt_only"),
                    "max_callchains": body.get("max_callchains", 5),
                    "min_callchain_percentage": body.get("min_callchain_percentage", 0.0),
                })
                _json_response(self, HTTPStatus.OK, result)
            except Exception as e:
                _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})
            return

        _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not_found"})


def main() -> int:
    parser = argparse.ArgumentParser(description="Stability Analysis Agent Local Daemon (HTTP)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--max-workers",
        type=int,
        default=_env_int("STABILITY_AGENT_DAEMON_MAX_WORKERS", 2),
        help="Max concurrent CLI subprocesses (env STABILITY_AGENT_DAEMON_MAX_WORKERS)",
    )
    parser.add_argument(
        "--max-queue",
        type=int,
        default=_env_int("STABILITY_AGENT_DAEMON_MAX_QUEUE", 32),
        help="Max queued runs waiting for a worker (env STABILITY_AGENT_DAEMON_MAX_QUEUE)",
    )
    parser.add_argument(
        "--run-timeout",
        type=int,
        default=_env_int("STABILITY_AGENT_DAEMON_RUN_TIMEOUT", 0),
        help="Seconds before a running CLI is killed; 0 disables (env STABILITY_AGENT_DAEMON_RUN_TIMEOUT)",
    )
    args = parser.parse_args()
    ensure_run_scheduler(
        max_workers=args.max_workers,
        max_queue=args.max_queue,
        run_timeout_sec=args.run_timeout,
    )

    try:
        from cli.report_paths import ensure_reports_migrated

        ensure_reports_migrated(PROJECT_ROOT)
    except Exception:
        pass

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)

    def _shutdown(*_):
        httpd.shutdown()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    snap = ensure_run_scheduler().snapshot()
    print(f"daemon listening on http://{args.host}:{args.port} (protocol={PROTOCOL_VERSION})")
    print(
        f"  Workers:         max_workers={snap['max_workers']} max_queue={snap['max_queue']} "
        f"run_timeout_sec={snap['run_timeout_sec']}"
    )
    print("  Web UI:         http://{}/".format(f"{args.host}:{args.port}"))
    print("  Run API:         POST /runs")
    print("                   GET /health  GET /status/<id>  GET /result/<id>  POST /cancel/<id>")
    print("                   GET /runs  GET /runs/<id>  GET /runs/<id>/events  POST /runs/<id>/cancel")
    print("                   POST /runs/<id>/vector-db/commit")
    print("  Web preferences: GET/POST /web/preferences")
    print("  Skills API:      GET /skills  GET /skills/<name>")
    print("                   POST /skills/install  POST /skills/lint  POST /skills/uninstall")
    print("  Tool System API: POST /tool-system/analyze  POST /tool-system/native-leak")
    print("                   GET /tool-system/tools  GET /tool-system/workflows")
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
