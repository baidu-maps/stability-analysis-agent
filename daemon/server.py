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
import hashlib
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

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

IDEMPOTENCY_TTL_SEC = 7200
_IDEMPOTENCY_LOCK = threading.Lock()
_IDEMPOTENCY: Dict[str, Tuple[float, str, str]] = {}
_HEALTH_EXTRAS: List[Callable[[], Dict[str, Any]]] = []

_DENY_LOCAL_PATH_FIELDS = False
_ACCESS_LOG = False
_LOCAL_PATH_FIELDS = frozenset(
    {
        "crash_log",
        "crash_log_dir",
        "code_roots",
        "library_dir",
        "config",
        "native_leak_dir",
        "native_leak_trace_db",
        "workspace_root",
        "repo_cache_root",
        "vector_db_path",
    }
)
_OUTPUT_FORMATS = frozenset({"markdown", "json", "text"})


def set_deny_local_path_fields(enabled: bool) -> None:
    """Enterprise/remote daemons should turn this on for unauthenticated ports."""
    global _DENY_LOCAL_PATH_FIELDS
    _DENY_LOCAL_PATH_FIELDS = bool(enabled)


def _prepare_http_run_body(body: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize and guard a POST /runs JSON object."""
    payload = dict(body or {})
    if "apply_ai_fixes" not in payload:
        payload["apply_ai_fixes"] = False
    if "external_agent_evaluation" in payload and not isinstance(
        payload["external_agent_evaluation"], bool
    ):
        raise DaemonHttpError(
            HTTPStatus.BAD_REQUEST,
            {
                "error": "invalid_external_agent_evaluation",
                "message": "external_agent_evaluation 必须是 boolean",
            },
        )
    fmt = str(payload.get("output_format") or "markdown").strip().lower() or "markdown"
    if fmt not in _OUTPUT_FORMATS:
        raise DaemonHttpError(
            HTTPStatus.BAD_REQUEST,
            {
                "error": "invalid_output_format",
                "message": "output_format 只能是 markdown / json / text",
                "output_format": payload.get("output_format"),
            },
        )
    payload["output_format"] = fmt
    if _DENY_LOCAL_PATH_FIELDS:
        for field_name in sorted(_LOCAL_PATH_FIELDS):
            if field_name not in payload:
                continue
            value = payload.get(field_name)
            if value in (None, "", [], {}):
                continue
            raise DaemonHttpError(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": "forbidden_field",
                    "field": field_name,
                    "message": f"远程模式不允许传 {field_name}，日志请放 crash_log_content",
                },
            )
    content = payload.get("crash_log_content")
    has_content = isinstance(content, str) and bool(content.strip())
    has_path = bool(str(payload.get("crash_log") or "").strip())
    has_dir = bool(str(payload.get("crash_log_dir") or "").strip())
    if _DENY_LOCAL_PATH_FIELDS or not (has_path or has_dir):
        if not has_content:
            raise DaemonHttpError(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": "crash_log_content_required",
                    "message": "crash_log_content 不能为空",
                },
            )
    return payload


class DaemonHttpError(Exception):
    """Mapped to an HTTP JSON error by Handler.do_POST / do_GET."""

    def __init__(self, status: int, payload: Dict[str, Any]) -> None:
        """Store status code and JSON body."""
        super().__init__(str((payload or {}).get("error") or status))
        self.status = int(status)
        self.payload = dict(payload or {})


def register_health_extra(factory: Callable[[], Dict[str, Any]]) -> None:
    """Let the enterprise shell add fields to GET /health (replaces prior extra)."""
    _HEALTH_EXTRAS.clear()
    _HEALTH_EXTRAS.append(factory)


def reset_idempotency_for_tests() -> None:
    """Drop in-memory idempotency keys. Test-only."""
    with _IDEMPOTENCY_LOCK:
        _IDEMPOTENCY.clear()


# ---------------------------------------------------------------------------
# Tool System executor（用于 /tool-system/* 端点）
# ---------------------------------------------------------------------------
_ts_runtimes: Dict[str, Any] = {}
_ts_lock = threading.Lock()
_SHUTTING_DOWN = False
_EVICTION_STARTED = False
_TERMINAL_RUN_STATUSES = frozenset({"done", "error", "canceled"})


class DropOldestQueue(queue.Queue):
    """Bounded queue; a full put drops the oldest item instead of blocking."""

    def put(self, item, block: bool = True, timeout: Optional[float] = None) -> None:
        """Enqueue ``item``, discarding the oldest entry when ``maxsize`` is reached."""
        with self.not_full:
            if 0 < self.maxsize <= self._qsize():
                self._get()
                if self.unfinished_tasks > 0:
                    self.unfinished_tasks -= 1
            self._put(item)
            self.unfinished_tasks += 1
            self.not_empty.notify()


class RecordingEventQueue(DropOldestQueue):
    """Live queue plus a bounded in-memory replay log for reconnecting SSE clients."""
    def __init__(self, owner: Any, maxsize: int):
        super().__init__(maxsize=maxsize)
        self.owner = owner

    def put(self, item: Any, block: bool = True, timeout: Optional[float] = None) -> None:
        if isinstance(item, RunEvent):
            item = RunEvent(item.run_id, item.type, item.data, seq=len(self.owner.event_log) + 1)
            self.owner.event_log.append(item)
            del self.owner.event_log[:-512]
            _persist_run_event(self.owner)
        super().put(item, block=block, timeout=timeout)


def _new_event_queue() -> queue.Queue:
    """Create the per-run SSE queue with a drop-oldest bound."""
    maxsize = _env_int("STABILITY_AGENT_DAEMON_EVENT_QUEUE_MAX", 256)
    return DropOldestQueue(maxsize=max(1, maxsize))


_ENGINE_TYPES = frozenset({"direct", "langchain", "langgraph"})


def _resolve_tool_system_engine(engine: Optional[str]) -> str:
    """Resolve tool-system engine; explicit invalid values raise ValueError."""
    if engine is None or not str(engine).strip():
        value = str(os.environ.get("STABILITY_AGENT_DAEMON_ENGINE") or "direct").strip() or "direct"
    else:
        value = str(engine).strip()
    if value not in _ENGINE_TYPES:
        raise ValueError("engine must be one of: direct, langchain, langgraph")
    return value


def _build_ts_agent_runtime(engine: str):
    from tool_system import (
        ToolAndWorkflowRegistry, SystemConfig, LLMConfig,
        ToolConfig, WorkflowConfig, ConfigDrivenExecutor,
        LLMAdapterFactory, register_all_tools_and_workflows, AgentRuntime,
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
            WorkflowConfig(name="native_leak_analysis", enabled=True),
        ],
    )
    llm_adapter = None
    api_key = os.environ.get("WENXIN_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if api_key:
        try:
            llm_cfg = {
                "provider": "openai",
                "model": os.environ.get("OPENAI_MODEL", "glm-4"),
                "api_key": api_key,
                "base_url": os.environ.get("OPENAI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"),
            }
            llm_adapter = LLMAdapterFactory.create(llm_cfg)
            config.llm = LLMConfig(**llm_cfg)
        except Exception:
            pass
    return AgentRuntime(
        ConfigDrivenExecutor(registry, config, llm_adapter),
        engine=engine,
    )


def _get_ts_agent_runtime(engine: Optional[str] = None):
    """延迟初始化 AgentRuntime（按 engine 缓存，tool-system 与 daemon 内联执行共用）。"""
    resolved = _resolve_tool_system_engine(engine)
    cached = _ts_runtimes.get(resolved)
    if cached is not None:
        return cached
    with _ts_lock:
        cached = _ts_runtimes.get(resolved)
        if cached is not None:
            return cached
        try:
            _ts_runtimes[resolved] = _build_ts_agent_runtime(resolved)
        except Exception as e:
            raise RuntimeError(f"tool_system 初始化失败: {e}") from e
    return _ts_runtimes[resolved]


def _run_tool_system_workflow(
    workflow_name: str,
    problem: Dict[str, Any],
    *,
    engine: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute a workflow through the unified AgentRuntime lifecycle."""
    resolved_engine = engine or (problem or {}).get("engine")
    runtime = _get_ts_agent_runtime(resolved_engine)
    result = runtime.run(workflow_name, dict(problem or {}), defer_decision=False)
    if isinstance(result, dict):
        metadata = result.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
            result["metadata"] = metadata
        metadata["runtime_state"] = runtime.state.to_dict()
        trace = runtime.trace
        if trace is not None and hasattr(trace, "snapshot"):
            metadata["runtime_trace"] = trace.snapshot()
        metadata["runtime_decision"] = runtime.state.decision
    return result if isinstance(result, dict) else {"status": "error", "error": "invalid workflow result"}


@dataclass
class RunState:
    run_id: str
    transport_status: str  # queued/running/verification_pending/done/error/canceled
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
    last_progress_percent: Optional[int] = None
    completion_reason: Optional[str] = None
    runtime_state: Optional[Dict[str, Any]] = None
    approval: Optional[Dict[str, Any]] = None
    cancel_requested: bool = False

    process: Optional[subprocess.Popen] = None
    events: "queue.Queue[RunEvent]" = field(default_factory=_new_event_queue)
    result: Optional[RunResult] = None
    request: Optional[RunRequest] = None
    pending_workspace: Any = None
    pending_changed_files: List[str] = field(default_factory=list)
    pending_verification: Optional[Dict[str, Any]] = None
    pending_tool_approval: Optional[Dict[str, Any]] = None
    runtime_trace: Optional[Dict[str, Any]] = None
    event_log: List[RunEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        maxsize = _env_int("STABILITY_AGENT_DAEMON_EVENT_QUEUE_MAX", 256)
        self.events = RecordingEventQueue(self, max(1, maxsize))

    def runtime_core(self) -> Any:
        from tool_system.runtime import RuntimeState

        payload = self.runtime_state if isinstance(self.runtime_state, dict) else {}
        if not payload:
            payload = {"stage": "observe", "status": "running"}
        return RuntimeState.from_dict(payload)

    @property
    def stage(self) -> str:
        return str(self.runtime_core().stage or "observe")

    @property
    def runtime_status(self) -> str:
        return str(self.runtime_core().status or "running")

    @property
    def runtime_decision(self) -> Optional[str]:
        return self.runtime_core().decision

    @property
    def runtime_checkpoints(self) -> List[Any]:
        return list(self.runtime_core().checkpoints)

    def apply_runtime_core(self, state: Any) -> None:
        if hasattr(state, "to_dict"):
            payload = state.to_dict()
        elif isinstance(state, dict):
            payload = state
        else:
            return
        _adopt_runtime_payload(self, payload)

    @property
    def status(self) -> str:
        """HTTP/transport alias for ``transport_status`` (not harness ``RuntimeState.status``)."""
        return self.transport_status

    @status.setter
    def status(self, value: str) -> None:
        self.transport_status = value


_TRANSPORT_RUNTIME_SYNC = {
    "verification_pending": ("verify", "pending", "verification_pending"),
    "approval_required": ("verify", "pending", "approval_required"),
    "running": (None, "running", None),
    "done": ("decide", "completed", None),
    "error": ("decide", "error", None),
    "canceled": (None, "error", "canceled"),
}


def _set_transport_status(run: RunState, value: str, *, sync_runtime: bool = True) -> None:
    """Set daemon transport status and optionally sync harness RuntimeState."""
    run.transport_status = str(value or "").strip() or "queued"
    if not sync_runtime:
        return
    mapping = _TRANSPORT_RUNTIME_SYNC.get(run.transport_status)
    if mapping is None:
        return
    stage, runtime_status, reason = mapping
    payload = dict(run.runtime_state or {})
    if stage is not None:
        run.runtime_state = _runtime_payload_transition(payload, stage, status=runtime_status, reason=reason)
    elif payload:
        state = run.runtime_core()
        state.transition(state.stage, status=runtime_status, reason=reason)
        run.runtime_state = state.to_dict()
    else:
        run.runtime_state = _runtime_payload_transition(
            None, "observe", status=runtime_status, reason=reason,
        )


def _map_logical_transport_status(logical_status: str) -> str:
    return {
        "success": "done",
        "error": "error",
        "approval_required": "approval_required",
        "verification_pending": "verification_pending",
    }.get(str(logical_status or "").strip(), str(logical_status or "").strip() or "error")


def _runtime_payload_transition(payload: Optional[Dict[str, Any]], stage: str, *,
                                status: str, reason: Optional[str] = None) -> Dict[str, Any]:
    """Use core RuntimeState as the only lifecycle state transition owner."""
    from tool_system.runtime import RuntimeState

    original = dict(payload or {})
    state = RuntimeState.from_dict(original)
    state.transition(stage, status=status, reason=reason)
    normalized = state.to_dict()
    for key, value in original.items():
        if key not in normalized:
            normalized[key] = value
    return normalized


def _adopt_runtime_payload(run: RunState, payload: Optional[Dict[str, Any]]) -> None:
    if not isinstance(payload, dict):
        return
    from tool_system.runtime import RuntimeState

    normalized = RuntimeState.from_dict(payload).to_dict()
    for key, value in payload.items():
        if key not in normalized:
            normalized[key] = value
    run.runtime_state = normalized


class RunManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: Dict[str, RunState] = {}
        self._restore_persisted_runs()

    def _restore_persisted_runs(self) -> None:
        try:
            from services.run_store import load_snapshots
            for item in load_snapshots():
                # Only resumable runs are restored. Finished runs remain useful
                # on disk for audit but must not repopulate daemon memory.
                if item.get("transport_status") not in {"verification_pending", "approval_required"}:
                    continue
                request = run_request_from_dict(item.get("request") or {}) if item.get("request") else None
                transport = str(item.get("transport_status") or "queued")
                run = RunState(run_id=str(item.get("run_id")), transport_status=transport,
                               created_at=float(item.get("created_at") or time.time()),
                               output_format=str(item.get("output_format") or "markdown"), request=request)
                for name in ("started_at", "finished_at", "exit_code", "error", "report_dir", "workspace_dir",
                             "workspace_manifest", "patch_path", "last_progress", "last_progress_percent", "completion_reason",
                             "runtime_state", "approval", "runtime_trace", "pending_tool_approval"):
                    if name in item:
                        setattr(run, name, item[name])
                run.original_code_roots = list(item.get("original_code_roots") or [])
                run.isolated_code_roots = list(item.get("isolated_code_roots") or [])
                run.pending_changed_files = list(item.get("pending_changed_files") or [])
                run.pending_verification = item.get("pending_verification") if isinstance(item.get("pending_verification"), dict) else None
                saved_result = item.get("result")
                if isinstance(saved_result, dict):
                    run.result = RunResult(run.run_id, run.status, run.output_format,
                                           str(saved_result.get("output") or ""), saved_result.get("error"))
                saved_events = item.get("events")
                if isinstance(saved_events, list):
                    run.event_log = [RunEvent(str(x.get("run_id") or run.run_id), str(x.get("type") or "replayed"),
                                              x.get("data") if isinstance(x.get("data"), dict) else {}, int(x.get("seq") or i + 1))
                                      for i, x in enumerate(saved_events) if isinstance(x, dict)]
                if run.status == "verification_pending":
                    run.pending_workspace = _restore_workspace_from_manifest(run)
                elif run.status == "approval_required":
                    if not run.pending_tool_approval:
                        run.pending_tool_approval = _load_report_pending_tool_approval(run)
                self._runs[run.run_id] = run
        except Exception:
            return

    def create_run(self, req: RunRequest) -> RunState:
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        st = RunState(
            run_id=run_id,
            transport_status="queued",
            created_at=time.time(),
            output_format=req.output_format,
            request=req,
        )
        with self._lock:
            self._runs[run_id] = st
        _persist_run_state(st)
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

def _persist_run_state(run: RunState) -> None:
    try:
        from services.run_store import save_snapshot
        save_snapshot(run)
    except Exception:
        # Persistence must not take down the analysis process.
        return


def _persist_run_event(run: RunState) -> None:
    _persist_run_state(run)


def _env_int(name: str, default: int) -> int:
    """Read a non-negative int from the environment."""
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def _drop_run_events(run: RunState) -> None:
    """Discard queued SSE events after a run is evicted or finished."""
    while True:
        try:
            run.events.get_nowait()
        except queue.Empty:
            break


def evict_finished_runs(*, now: Optional[float] = None, ttl_sec: Optional[int] = None) -> int:
    """Drop finished runs from memory after ``ttl_sec`` (default 6 hours). ``ttl_sec==0`` disables."""
    ttl = _env_int("STABILITY_AGENT_DAEMON_RUN_TTL_SEC", 6 * 60 * 60) if ttl_sec is None else int(ttl_sec)
    if ttl <= 0:
        return 0
    stamp = now if now is not None else time.time()
    removed = 0
    for run_id, run in list(RUNS.list().items()):
        if run.status not in _TERMINAL_RUN_STATUSES:
            continue
        finished = run.finished_at or run.created_at
        if stamp - finished < ttl:
            continue
        RUNS.discard(run_id)
        _drop_run_events(run)
        removed += 1
        with _IDEMPOTENCY_LOCK:
            stale = [key for key, item in _IDEMPOTENCY.items() if item[2] == run_id]
            for key in stale:
                _IDEMPOTENCY.pop(key, None)
    return removed


def _eviction_loop() -> None:
    """Background loop that evicts finished in-memory runs."""
    while True:
        time.sleep(60)
        try:
            evict_finished_runs()
        except Exception:
            continue


def _ensure_eviction_thread() -> None:
    """Start the finished-run eviction thread once per process."""
    global _EVICTION_STARTED
    if _EVICTION_STARTED:
        return
    _EVICTION_STARTED = True
    threading.Thread(target=_eviction_loop, name="run-eviction", daemon=True).start()


def _drain_active_runs(wait_sec: int) -> None:
    """Cancel queued/running work and wait up to ``wait_sec`` for workers to leave those states."""
    for run in RUNS.list().values():
        if run.status in ("queued", "running"):
            try:
                _cancel_run(run)
            except Exception:
                continue
    deadline = time.time() + max(0, int(wait_sec))
    while time.time() < deadline:
        if not any(r.status in ("queued", "running") for r in RUNS.list().values()):
            return
        time.sleep(0.2)


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
        _ensure_eviction_thread()
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
    global SCHEDULER, _SHUTTING_DOWN
    _SHUTTING_DOWN = False
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
        reset_idempotency_for_tests()
        return SCHEDULER


def _send_cors_headers(handler: BaseHTTPRequestHandler) -> None:
    """Allow intranet digital-employee / browser clients to call the daemon."""
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Idempotency-Key")
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


DEFAULT_MAX_BODY_BYTES = 16 * 1024 * 1024


def _max_json_body_bytes() -> int:
    """JSON request body limit in bytes (default 16 MiB)."""
    return max(1, _env_int("STABILITY_AGENT_DAEMON_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES))


def _read_json_body(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    """Read a JSON object from the request. Oversize ``Content-Length`` is 413 before ``read``."""
    raw_len = str(handler.headers.get("Content-Length", "0") or "0").strip()
    try:
        length = int(raw_len)
    except ValueError:
        raise DaemonHttpError(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_content_length", "message": "Content-Length 必须是整数"},
        )
    if length < 0:
        raise DaemonHttpError(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_content_length", "message": "Content-Length 不能为负数"},
        )
    limit = _max_json_body_bytes()
    if length > limit:
        raise DaemonHttpError(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            {
                "error": "payload_too_large",
                "max_body_bytes": limit,
                "content_length": length,
                "message": f"请求体超过上限 {limit} 字节",
            },
        )
    raw = handler.rfile.read(length) if length > 0 else b"{}"
    try:
        parsed = json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise DaemonHttpError(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_json", "message": str(exc)},
        ) from exc
    if not isinstance(parsed, dict):
        raise DaemonHttpError(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_json", "message": "请求体必须是 JSON object"},
        )
    return parsed


def _installed_dist_version(dist_name: str) -> Optional[str]:
    """Return installed distribution version, or None if missing."""
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:
        from importlib_metadata import PackageNotFoundError, version  # type: ignore
    try:
        return str(version(dist_name))
    except PackageNotFoundError:
        return None
    except Exception:
        return None


def _health_package_info() -> Dict[str, Any]:
    """Core package identity for GET /health."""
    payload: Dict[str, Any] = {
        "package": "stability-analysis-agent",
        "package_version": _installed_dist_version("stability-analysis-agent"),
    }
    for factory in list(_HEALTH_EXTRAS):
        try:
            extra = factory() or {}
        except Exception:
            extra = {}
        if isinstance(extra, dict):
            payload.update(extra)
    return payload


def _run_fingerprint(body: Dict[str, Any]) -> str:
    """Hash the fields that distinguish one analysis request."""
    relevant = {
        "crash_log_content": body.get("crash_log_content"),
        "crash_log": body.get("crash_log"),
        "crash_log_dir": body.get("crash_log_dir"),
        "platform": body.get("platform"),
        "sdk_version": body.get("sdk_version"),
        "apply_ai_fixes": body.get("apply_ai_fixes"),
        "output_format": body.get("output_format"),
        "external_agent_evaluation": body.get("external_agent_evaluation", False),
    }
    blob = json.dumps(relevant, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _extract_idempotency_key(handler: BaseHTTPRequestHandler, body: Dict[str, Any]) -> str:
    """Read Idempotency-Key header or body.idempotency_key."""
    header = str(
        handler.headers.get("Idempotency-Key")
        or handler.headers.get("idempotency-key")
        or ""
    ).strip()
    if header:
        return header
    return str((body or {}).get("idempotency_key") or "").strip()


def _idempotency_lookup(key: str, fingerprint: str) -> Optional[str]:
    """Return existing run_id, or raise DaemonHttpError on fingerprint mismatch."""
    now = time.time()
    with _IDEMPOTENCY_LOCK:
        item = _IDEMPOTENCY.get(key)
        if not item:
            return None
        expires, stored_fp, run_id = item
        if expires < now:
            _IDEMPOTENCY.pop(key, None)
            return None
        if stored_fp != fingerprint:
            raise DaemonHttpError(
                HTTPStatus.CONFLICT,
                {
                    "error": "idempotency_key_conflict",
                    "message": "同一 idempotency_key 不能搭配不同的分析请求",
                    "run_id": run_id,
                },
            )
        return run_id


def _idempotency_store(key: str, fingerprint: str, run_id: str) -> None:
    """Remember a successful POST /runs under an idempotency key."""
    with _IDEMPOTENCY_LOCK:
        _IDEMPOTENCY[key] = (time.time() + IDEMPOTENCY_TTL_SEC, fingerprint, run_id)


def _created_run_payload(run: "RunState") -> Dict[str, Any]:
    """JSON body returned by POST /runs."""
    return {
        "run_id": run.run_id,
        "status": run.status,
        "message": _status_message(run),
        "links": _run_links(run.run_id),
    }


def _load_run_summary(run: "RunState") -> Optional[Dict[str, Any]]:
    """Load 00_run_summary.json when present."""
    report_dir = _resolve_report_dir(run.report_dir)
    if report_dir is None:
        return None
    path = report_dir / "00_run_summary.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


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
        cmd += ["--code-roots", cr]
    if req.config:
        cmd += ["--config", req.config]

    if req.engine != "direct":
        cmd += ["--engine", req.engine]

    output_format = req.output_format or getattr(defaults, "output_format", "markdown")
    if output_format != getattr(defaults, "output_format", "markdown"):
        cmd += ["--output-format", output_format]

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
    if bool(getattr(req, "external_agent_evaluation", False)) != bool(
        getattr(defaults, "external_agent_evaluation", False)
    ):
        cmd += [
            "--external-agent-evaluation"
            if req.external_agent_evaluation
            else "--no-external-agent-evaluation"
        ]

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
    _append_nondefault_value(
        cmd,
        "--max-symbol-only-rescues",
        req.max_symbol_only_rescues,
        getattr(defaults, "max_symbol_only_rescues", None),
    )
    _append_nondefault_value(
        cmd,
        "--max-crash-caller-search-files",
        req.max_crash_caller_search_files,
        getattr(defaults, "max_crash_caller_search_files", None),
    )
    for module in req.plugin_modules or []:
        module_text = str(module or "").strip()
        if module_text:
            cmd += ["--plugin-module", module_text]

    if isinstance(getattr(req, "verification", None), dict):
        cmd += ["--verification-config-json", json.dumps(req.verification, ensure_ascii=False)]
    cmd += ["--no-interactive", "--no-save-to-vector-db"]

    return cmd, stdin_text


def _persist_result(run: RunState) -> None:
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


def _run_worker(run: RunState, req: RunRequest) -> None:
    if run.status == "canceled":
        return
    _set_transport_status(run, "running")
    run.started_at = time.time()
    run.events.put(RunEvent(run.run_id, "run_started", {"request": req.to_dict()}))

    workspace = None
    cmd, stdin_text = _build_cli_cmd(req)
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
        if run.cancel_requested:
            try:
                p.terminate()
            except Exception:
                pass
            _set_transport_status(run, "canceled")

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

        output_text = "".join(stdout_acc)
        reason = _extract_completion_reason(output_text)
        if reason:
            run.completion_reason = reason
        report_summary = _load_run_summary(run)
        if isinstance(report_summary, dict):
            saved_runtime = report_summary.get("runtime_state")
            if isinstance(saved_runtime, dict):
                _adopt_runtime_payload(run, saved_runtime)
            run.runtime_trace = _load_report_runtime_trace(run)
            _emit_trace_handoff(run, run.runtime_trace)
        workspace = _restore_workspace_from_report(run)
        if run.cancel_requested or run.status == "canceled":
            status = "canceled"
        elif reason == "verification_pending":
            status = "verification_pending"
        elif reason == "approval_required":
            status = "approval_required"
        elif reason and reason.startswith("skipped_"):
            status = "error"
        elif exit_code == 0:
            status = "done"
        else:
            status = "error"
        _set_transport_status(run, status)
        result_error = _summarize_cli_outcome(run, output_text, exit_code)
        run.error = result_error
        run.result = RunResult(
            run_id=run.run_id,
            status=run.status,
            output_format=req.output_format,
            output=output_text,
            error=result_error,
        )
        if status == "verification_pending":
            if not run.runtime_state:
                _adopt_runtime_payload(run, _runtime_payload_transition(
                    None, "verify", status="pending",
                    reason="verification_provider_not_configured",
                ))
            run.pending_workspace = workspace
            run.pending_verification = _load_report_verification(run)
            run.pending_changed_files = list(
                (run.pending_verification or {}).get("changed_files") or []
            )
            run.events.put(RunEvent(
                run.run_id,
                "verification_pending",
                {
                    "message": "等待用户提交明确验证配置；候选命令不会自动执行",
                    "verification": run.pending_verification or {},
                    "resume": f"/runs/{run.run_id}/verification",
                },
            ))
        elif status == "approval_required":
            if not run.runtime_state:
                _adopt_runtime_payload(run, _runtime_payload_transition(
                    None, "verify", status="pending", reason="approval_required",
                ))
            run.pending_tool_approval = _load_report_pending_tool_approval(run)
            run.events.put(RunEvent(
                run.run_id,
                "tool_approval_required",
                {
                    "message": "等待用户批准工具调用",
                    "pending_tool_approval": run.pending_tool_approval or {},
                    "resume": f"/runs/{run.run_id}/tool-approval",
                },
            ))
        _persist_run_state(run)
        _persist_result(run)
    except Exception as e:
        _set_transport_status(run, "error")
        run.error = str(e)
        run.finished_at = time.time()
        run.events.put(RunEvent(run.run_id, "run_error", {"error": str(e)}))
        _persist_run_state(run)
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
        _set_transport_status(run, "canceled")
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
_STAGE_RE = re.compile(r"\[阶段\s*(\d+)\s*/\s*(\d+)\]")
_COMPLETION_REASON_RE = re.compile(
    r'completion_reason["\s:=]+([A-Za-z0-9_]+)',
    re.IGNORECASE,
)
_SKIP_REASON_TOKENS = (
    "skipped_no_usable_resolve",
    "skipped_no_usable_parse",
    "skipped_no_usable_code",
)


def _progress_percent_from_text(text: str) -> Optional[int]:
    """Map ``[阶段 n/m]`` to 0-100. Unknown text returns None."""
    match = _STAGE_RE.search(str(text or ""))
    if not match:
        return None
    current = int(match.group(1))
    total = int(match.group(2))
    if total <= 0:
        return None
    percent = int(round(100.0 * current / total))
    return max(0, min(100, percent))


def _extract_completion_reason(text: str) -> Optional[str]:
    """Parse CLI completion_reason from stdout/stderr blobs."""
    blob = str(text or "")
    match = _COMPLETION_REASON_RE.search(blob)
    if match:
        return str(match.group(1) or "").strip() or None
    for token in _SKIP_REASON_TOKENS:
        if token in blob:
            return token
    if "verification_pending" in blob:
        return "verification_pending"
    return None


def _load_report_verification(run: RunState) -> Optional[Dict[str, Any]]:
    if not run.report_dir:
        return None
    path = Path(run.report_dir).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    try:
        payload = json.loads((path / "09_verification.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _load_report_applied_fixes(run: RunState) -> Optional[Dict[str, Any]]:
    if not run.report_dir:
        return None
    path = Path(run.report_dir).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    try:
        payload = json.loads((path / "08_apply_ai_fixes.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _emit_trace_handoff(run: RunState, trace: Optional[Dict[str, Any]]) -> None:
    if not isinstance(trace, dict):
        return
    events = trace.get("events") if isinstance(trace.get("events"), list) else []
    run.events.put(
        RunEvent(
            type="trace_loaded",
            data={
                "event_count": len(events),
                "report_dir": run.report_dir,
                "run_id": trace.get("run_id"),
            },
        )
    )


def _load_report_runtime_trace(run: RunState) -> Optional[Dict[str, Any]]:
    if not run.report_dir:
        return None
    path = Path(run.report_dir).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    sidecar = path / "00_runtime_trace.json"
    if sidecar.is_file():
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else None
        except (OSError, ValueError):
            pass
    summary = _load_run_summary(run)
    if isinstance(summary, dict) and isinstance(summary.get("trace"), dict):
        return summary.get("trace")
    return None


def _load_report_pending_tool_approval(run: RunState) -> Optional[Dict[str, Any]]:
    if not run.report_dir:
        return None
    path = Path(run.report_dir).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    sidecar = path / "09_pending_tool_approval.json"
    if not sidecar.is_file():
        return None
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError):
        return None


def _restore_workspace_from_manifest(run: RunState) -> Any:
    """Rehydrate only the metadata needed to sync a pending worktree."""
    path = Path(run.workspace_manifest or "").expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        from services.git_worktree_manager import IsolatedCodeWorkspace, RepositoryWorktree
        repositories = [RepositoryWorktree(Path(x["repository"]), Path(x["worktree"]), str(x["base_commit"]))
                        for x in payload.get("repositories", [])]
        return IsolatedCodeWorkspace(str(payload["run_id"]), Path(payload["workspace_root"]),
                                     list(payload.get("original_code_roots") or []),
                                     list(payload.get("isolated_code_roots") or []), repositories)
    except (OSError, KeyError, TypeError, ValueError):
        return None


def _restore_workspace_from_report(run: RunState) -> Any:
    """Load the AgentRuntime-owned worktree manifest from a CLI report."""
    if not run.report_dir:
        return None
    report_dir = Path(run.report_dir).expanduser()
    if not report_dir.is_absolute():
        report_dir = PROJECT_ROOT / report_dir
    manifest = (report_dir.resolve() / "09_ai_fix_workspace.json")
    if not manifest.is_file():
        return None
    run.workspace_manifest = str(manifest)
    patch = report_dir / "09_ai_fix.patch"
    run.patch_path = str(patch) if patch.is_file() else None
    workspace = _restore_workspace_from_manifest(run)
    if workspace is not None:
        run.workspace_dir = str(workspace.root)
        run.original_code_roots = list(workspace.original_code_roots)
        run.isolated_code_roots = list(workspace.isolated_code_roots)
        run.events.put(RunEvent(run.run_id, "workspace_restored", workspace.to_dict()))
    return workspace


def _post_fix_diagnosis(run: RunState) -> Dict[str, Any]:
    """Re-run deterministic diagnosis after a verified sync, without applying fixes."""
    from services.post_fix_diagnosis import run_post_fix_diagnosis_from_request

    return run_post_fix_diagnosis_from_request(
        run.request,
        project_root=PROJECT_ROOT,
        env=_subprocess_env_for_run(run.run_id),
    )


def _summarize_cli_outcome(
    run: RunState,
    output_text: str,
    exit_code: Optional[int],
) -> Optional[str]:
    """Build a user-facing Chinese error; None when the run succeeded."""
    reason = run.completion_reason or _extract_completion_reason(output_text)
    stage = (run.last_progress or "").strip() or "未知阶段"
    if run.status == "canceled":
        code = exit_code if exit_code is not None else -15
        return f"任务已取消，未产出分析结果（exit_code={code}）"
    if reason and str(reason).startswith("skipped_"):
        return f"分析未正常完成；中止于 {stage}；completion_reason={reason}"
    if run.status == "error" or (exit_code not in (None, 0)):
        extra = f"；completion_reason={reason}" if reason else ""
        return f"分析未正常完成；中止于 {stage}{extra}"
    return None


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
        percent = _progress_percent_from_text(stripped)
        if percent is not None:
            run.last_progress_percent = percent
        reason = _extract_completion_reason(stripped)
        if reason:
            run.completion_reason = reason


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
    if run.status == "verification_pending":
        return "等待用户配置验证命令"
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
        "verification": f"/runs/{run_id}/verification",
        "cleanup": f"/runs/{run_id}/cleanup",
        "checkpoints": f"/runs/{run_id}/checkpoints",
        "resume": f"/runs/{run_id}/resume",
        "retry_stage": f"/runs/{run_id}/retry-stage",
        "tool_approval": f"/runs/{run_id}/tool-approval",
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
    payload = {
        "run_id": run.run_id,
        "status": run.status,
        "transport_status": run.transport_status,
        "message": _status_message(run),
        "progress": run.last_progress,
        "progress_percent": run.last_progress_percent,
        "queue_position": _queue_position(run),
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "exit_code": run.exit_code,
        "error": run.error,
        "completion_reason": run.completion_reason,
        "stage": run.stage,
        "runtime_status": run.runtime_status,
        "runtime_decision": run.runtime_decision,
        "runtime_state": run.runtime_state,
        "approval": run.approval,
        "output_format": run.output_format,
        "report_dir": run.report_dir,
        "workspace_dir": run.workspace_dir,
        "original_code_roots": run.original_code_roots,
        "isolated_code_roots": run.isolated_code_roots,
        "workspace_manifest": run.workspace_manifest,
        "patch_path": run.patch_path,
        "verification": run.pending_verification,
        "discovered_candidates": (
            (run.pending_verification or {}).get("discovered_candidates")
            if isinstance(run.pending_verification, dict) else None
        ),
        "pending_tool_approval": run.pending_tool_approval,
        "runtime_trace": run.runtime_trace,
        "timeline_event_count": len((run.runtime_trace or {}).get("events") or [])
        if isinstance(run.runtime_trace, dict) else 0,
        "links": _run_links(run.run_id),
    }
    try:
        from services.run_snapshot import HarnessRunSnapshot

        snapshot = HarnessRunSnapshot.from_daemon_run(run)
        payload["timeline_summary"] = snapshot.timeline_summary()
    except Exception:
        pass
    report_dir = str(run.report_dir or "").strip()
    if report_dir:
        eval_path = Path(report_dir).expanduser().resolve() / "00_evaluation.json"
        if eval_path.is_file():
            try:
                payload["evaluation"] = json.loads(eval_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        elif run.status in {"done", "success", "error", "failed"} and run.runtime_trace:
            try:
                from services.evaluation import evaluate_report_dir

                payload["evaluation"] = evaluate_report_dir(report_dir).to_dict()
            except Exception:
                pass
    return payload


def _result_public_dict(run: RunState) -> Dict[str, Any]:
    """JSON view of a finished run, including a cleaned ``report`` field."""
    payload = run.result.to_dict() if run.result is not None else {}
    output = str(payload.get("output") or "")
    payload["report"] = _extract_report(output)
    payload["error"] = run.error if payload.get("error") is None else payload.get("error")
    payload["completion_reason"] = run.completion_reason
    payload["verification"] = run.pending_verification
    payload["links"] = _run_links(run.run_id)
    return payload


def _respond_status(handler: BaseHTTPRequestHandler, run_id: str) -> None:
    """GET /status/{id} and GET /runs/{id}."""
    run = RUNS.get(run_id)
    if not run:
        _json_response(handler, HTTPStatus.NOT_FOUND, {"error": "run_not_found"})
        return
    _json_response(handler, HTTPStatus.OK, _run_public_dict(run))


def _respond_checkpoints(handler: BaseHTTPRequestHandler, run_id: str) -> None:
    run = RUNS.get(run_id)
    if not run:
        _json_response(handler, HTTPStatus.NOT_FOUND, {"error": "run_not_found"})
        return
    state = run.runtime_state if isinstance(run.runtime_state, dict) else {}
    checkpoints = state.get("checkpoints") if isinstance(state.get("checkpoints"), list) else []
    _json_response(handler, HTTPStatus.OK, {"run_id": run_id, "stage": run.stage,
                                            "status": run.status, "checkpoints": checkpoints})


def _respond_retry_stage(handler: BaseHTTPRequestHandler, run_id: str,
                         body: Optional[Dict[str, Any]] = None) -> None:
    run = RUNS.get(run_id)
    if not run:
        _json_response(handler, HTTPStatus.NOT_FOUND, {"error": "run_not_found"})
        return
    if run.status in {"queued", "running"}:
        _json_response(handler, HTTPStatus.CONFLICT, {"error": "run_still_active", "status": run.status})
        return
    body = body if isinstance(body, dict) else _read_json_body(handler)
    stage = str(body.get("stage") or "").strip()
    if stage not in {"observe", "analyze", "plan", "act", "verify", "decide"}:
        _json_response(handler, HTTPStatus.BAD_REQUEST, {"error": "invalid_stage"})
        return
    state = dict(run.runtime_state or {})
    checkpoints = list(state.get("checkpoints") or [])
    source_revision = str(body.get("source_revision") or "")
    checkpoint_id = str(body.get("checkpoint_id") or "").strip()
    selected = next((x for x in reversed(checkpoints) if isinstance(x, dict) and
                     ((checkpoint_id and x.get("checkpoint_id") == checkpoint_id) or
                      (not checkpoint_id and x.get("stage") == stage))), None)
    if selected is None:
        _json_response(handler, HTTPStatus.NOT_FOUND, {"error": "checkpoint_not_found", "stage": stage, "checkpoint_id": checkpoint_id})
        return
    if selected.get("stage") != stage:
        _json_response(handler, HTTPStatus.CONFLICT, {"error": "checkpoint_stage_mismatch"})
        return
    if not selected.get("source_revision") or not selected.get("worktree_revision"):
        _json_response(handler, HTTPStatus.CONFLICT, {"error": "checkpoint_revision_missing"})
        return
    workspace = run.pending_workspace or _restore_workspace_from_report(run)
    actual_source = actual_worktree = None
    if workspace is not None:
        from services.git_worktree_manager import workspace_revision, workspace_source_revision
        actual_source = workspace_source_revision(workspace)
        actual_worktree = workspace_revision(workspace)
    elif run.request is not None:
        from services.git_worktree_manager import revision_for_code_roots
        roots = normalize_run_code_roots(run.request)
        actual_source = revision_for_code_roots(roots, include_diff=False)
        actual_worktree = revision_for_code_roots(roots, include_diff=True)
    if selected.get("source_revision") != actual_source:
        _json_response(handler, HTTPStatus.CONFLICT, {"error": "source_revision_changed"})
        return
    if selected.get("worktree_revision") != actual_worktree:
        _json_response(handler, HTTPStatus.CONFLICT, {"error": "worktree_revision_changed"})
        return
    if selected.get("source_revision") != source_revision:
        _json_response(handler, HTTPStatus.CONFLICT, {"error": "source_revision_mismatch"})
        return
    if selected.get("worktree_revision") and str(body.get("worktree_revision") or "") != str(selected.get("worktree_revision")):
        _json_response(handler, HTTPStatus.CONFLICT, {"error": "worktree_revision_mismatch"})
        return
    idempotency_key = str(body.get("idempotency_key") or "").strip()
    if not idempotency_key:
        _json_response(handler, HTTPStatus.BAD_REQUEST, {"error": "idempotency_key_required"})
        return
    previous_keys = {str(x.get("idempotency_key")) for x in checkpoints if isinstance(x, dict)}
    previous_keys.update(
        str(x.get("idempotency_key"))
        for x in state.get("retry_requests", []) if isinstance(x, dict)
    )
    if idempotency_key in previous_keys:
        _json_response(handler, HTTPStatus.CONFLICT, {"error": "duplicate_idempotency_key"})
        return
    if stage == "act":
        _json_response(handler, HTTPStatus.CONFLICT, {"error": "act_replay_forbidden", "message": "act 禁止从 checkpoint 重放，必须创建新的显式修复任务"})
        return
    state = _runtime_payload_transition(
        state, stage,
        status="completed" if stage == "decide" else "retry_requested",
        reason="state_restored" if stage == "decide" else "explicit_stage_retry",
    )
    state.setdefault("retry_requests", []).append({
        "stage": stage, "requested_at": time.time(),
        "idempotency_key": idempotency_key,
        "checkpoint_id": selected.get("checkpoint_id"),
    })
    _adopt_runtime_payload(run, state)
    run.error = None
    if bool(body.get("execute")) and stage != "decide" and run.report_dir:
        if stage == "verify":
            verification_config = body.get("verification") if isinstance(body.get("verification"), dict) else None
            if not isinstance(verification_config, dict) or not verification_config.get("command"):
                _json_response(handler, HTTPStatus.BAD_REQUEST, {
                    "error": "verification_config_required",
                    "message": "verify retry 必须显式提交 verification.command，候选命令不会自动执行",
                })
                return
        cmd = [
            sys.executable,
            str(PROJECT_ROOT / "cli" / "main.py"),
            "replay",
            str(run.report_dir),
            "--from-stage",
            stage,
            "--checkpoint-id",
            str(selected.get("checkpoint_id") or ""),
        ]
        if stage == "verify":
            cmd += ["--verification-config-json", json.dumps(verification_config, ensure_ascii=False)]
        proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, check=False,
                              env=_subprocess_env_for_run(run.run_id))
        state.setdefault("retry_executions", []).append({
            "stage": stage,
            "exit_code": proc.returncode,
            "stdout_tail": (proc.stdout or "")[-4000:],
            "stderr_tail": (proc.stderr or "")[-2000:],
        })
        run.runtime_state = state
        run.runtime_trace = _load_report_runtime_trace(run)
        run.pending_tool_approval = _load_report_pending_tool_approval(run)
        reason = _extract_completion_reason((proc.stdout or "") + "\n" + (proc.stderr or ""))
        if reason == "verification_pending":
            _set_transport_status(run, "verification_pending")
        elif reason == "approval_required":
            _set_transport_status(run, "approval_required")
        else:
            _set_transport_status(run, "done" if proc.returncode == 0 else "error")
    elif stage == "decide":
        if run.transport_status not in {"error", "canceled"}:
            _set_transport_status(run, "done")
    _persist_run_state(run)
    _json_response(handler, HTTPStatus.OK, {"run_id": run_id, "status": run.status,
                                            "runtime_state": run.runtime_state,
                                            "message": "重试状态已记录；execute=true 会按指定阶段执行 replay，act 始终禁止重放"})


def _respond_result(
    handler: BaseHTTPRequestHandler,
    run_id: str,
    *,
    result_format: str = "",
) -> None:
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
                "progress_percent": run.last_progress_percent,
            },
        )
        return
    fmt = str(result_format or "").strip().lower()
    if fmt and fmt not in {"markdown", "json", "summary"}:
        _json_response(
            handler,
            HTTPStatus.BAD_REQUEST,
            {
                "error": "format_not_supported",
                "message": "format 仅支持 markdown（默认）或 summary",
            },
        )
        return
    payload = _result_public_dict(run)
    if fmt == "summary":
        payload["summary"] = _load_run_summary(run)
    _json_response(handler, HTTPStatus.OK, payload)


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
        {
            "run_id": run_id,
            "status": run.status,
            "message": _status_message(run),
            "cancel_requested": bool(run.cancel_requested),
        },
    )


def _respond_verification(handler: BaseHTTPRequestHandler, run_id: str,
                          body: Optional[Dict[str, Any]] = None) -> None:
    """Run explicitly configured verification for a pending fix."""
    run = RUNS.get(run_id)
    if not run:
        _json_response(handler, HTTPStatus.NOT_FOUND, {"error": "run_not_found"})
        return
    if run.status not in {"verification_pending", "approval_required"}:
        _json_response(handler, HTTPStatus.CONFLICT, {
            "error": "verification_not_pending",
            "status": run.status,
        })
        return
    try:
        body = body if isinstance(body, dict) else _read_json_body(handler)
        config = body.get("verification") if isinstance(body.get("verification"), dict) else body
        command = config.get("command") if isinstance(config, dict) else None
        if not command or (not isinstance(command, (list, str))):
            raise DaemonHttpError(HTTPStatus.BAD_REQUEST, {
                "error": "verification_command_required",
                "message": "必须显式提交 verification.command，候选命令不会自动执行",
            })
        if not run.report_dir:
            raise DaemonHttpError(HTTPStatus.CONFLICT, {"error": "verification_report_missing"})
        from services.repair_pipeline import resume_verification_from_report
        from tool_system.runtime import RunTrace

        trace = RunTrace.from_dict(
            run.runtime_trace, run_id=run.run_id,
            engine=(run.runtime_trace or {}).get("engine"),
        )
        pipeline = resume_verification_from_report(
            report_dir=Path(run.report_dir).expanduser().resolve(),
            verification_config=dict(config),
            trace=trace,
            run_id=run.run_id,
        )
        payload = pipeline.verification_result or {}
        updates = pipeline.result_updates
        run.pending_verification = payload
        run.approval = payload.get("approval") if isinstance(payload.get("approval"), dict) else run.approval
        run.pending_tool_approval = pipeline.pending_tool_approval
        _adopt_runtime_payload(run, pipeline.runtime_state or run.runtime_state)
        run.runtime_trace = trace.snapshot()
        logical_status = str(updates.get("status") or "verification_pending")
        _set_transport_status(run, _map_logical_transport_status(logical_status))
        run.error = str(updates.get("error") or "") or None
        run.completion_reason = str(updates.get("completion_reason") or "") or None
        run.finished_at = time.time() if run.status != "verification_pending" else None
        if run.result is not None:
            run.result = RunResult(run.run_id, run.status, run.output_format, run.result.output, run.error)
        run.events.put(RunEvent(run.run_id, "verification_finished", {"status": run.status, "verification": payload}))
        if run.status != "verification_pending":
            _persist_result(run)
        _persist_run_state(run)
        _json_response(handler, HTTPStatus.OK, _run_public_dict(run))
    except DaemonHttpError as exc:
        _json_response(handler, int(exc.status), exc.payload)
    except Exception as exc:
        _json_response(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "verification_error", "message": str(exc)})


def _respond_resume(handler: BaseHTTPRequestHandler, run_id: str) -> None:
    """Resume state or explicit verification without implicit side effects."""
    body = _read_json_body(handler)
    if str(body.get("stage") or "").strip():
        _respond_retry_stage(handler, run_id, body)
        return
    run = RUNS.get(run_id)
    if run is not None and run.status in {"verification_pending", "approval_required"}:
        _respond_verification(handler, run_id, body)
        return
    _json_response(handler, HTTPStatus.BAD_REQUEST, {
        "error": "resume_stage_required",
        "message": "resume 需要显式 stage；等待验证的 run 也可提交 verification.command",
    })


def _respond_tool_approval(handler: BaseHTTPRequestHandler, run_id: str) -> None:
    run = RUNS.get(run_id)
    if not run:
        _json_response(handler, HTTPStatus.NOT_FOUND, {"error": "run_not_found"})
        return
    if run.status not in {"approval_required", "verification_pending"}:
        _json_response(handler, HTTPStatus.CONFLICT, {"error": "tool_approval_not_applicable", "status": run.status})
        return
    try:
        body = _read_json_body(handler)
        pending = run.pending_tool_approval
        if not isinstance(pending, dict) or not isinstance(pending.get("approval"), dict):
            raise DaemonHttpError(HTTPStatus.CONFLICT, {
                "error": "tool_approval_not_pending",
                "message": "只能确认 runtime 已生成的 pending tool approval",
            })
        pending_approval = dict(pending["approval"])
        pending_status = str(pending_approval.get("status") or "required")
        if pending_status != "required":
            raise DaemonHttpError(HTTPStatus.CONFLICT, {
                "error": "approval_already_submitted",
                "status": pending_status,
            })
        tool_name = str(body.get("tool") or "").strip()
        if not tool_name:
            raise DaemonHttpError(HTTPStatus.BAD_REQUEST, {"error": "tool_name_required"})
        from services.verification import consume_approval

        fingerprint = str(body.get("fingerprint") or body.get("input_hash") or "").strip()
        if not fingerprint:
            raise DaemonHttpError(HTTPStatus.BAD_REQUEST, {"error": "command_fingerprint_required"})
        expected_tool = str(pending.get("tool") or "").strip()
        expected_call_id = str(pending_approval.get("tool_call_id") or pending.get("tool_call_id") or "").strip()
        expected_scope = str(pending_approval.get("scope") or "single_tool").strip()
        tool_call_id = str(body.get("tool_call_id") or expected_call_id).strip()
        scope = str(body.get("scope") or expected_scope).strip()
        submitted_approval_id = str(body.get("approval_id") or "").strip()
        if (tool_name != expected_tool or tool_call_id != expected_call_id or
                fingerprint != str(pending_approval.get("command_fingerprint") or "") or
                scope != expected_scope or
                (submitted_approval_id and submitted_approval_id != str(pending_approval.get("approval_id") or ""))):
            raise DaemonHttpError(HTTPStatus.CONFLICT, {"error": "approval_binding_mismatch"})
        requested_status = str(body.get("status") or "granted").strip().lower()
        if requested_status not in {"granted", "rejected"}:
            raise DaemonHttpError(HTTPStatus.BAD_REQUEST, {"error": "invalid_approval_status"})
        approval = dict(pending_approval)
        if requested_status == "rejected":
            approval.update(status="rejected", granted_by="user", source="explicit_user_request")
        else:
            approval.update(status="granted", granted_by="user", source="explicit_user_request")
            # verify is consumed inside resume_verification_from_report; other tools
            # are consumed after RuntimeActionExecutor succeeds.
        run.approval = approval
        run.pending_tool_approval = dict(pending)
        run.pending_tool_approval["approval"] = approval
        if requested_status == "rejected":
            _set_transport_status(run, "error")
            run.error = "tool approval rejected"
            run.events.put(RunEvent(run.run_id, "tool_approval_finished", {"status": "rejected", "approval": approval}))
            _persist_run_state(run)
            _json_response(handler, HTTPStatus.OK, _run_public_dict(run) | {"tool_approval": {"status": "rejected", "approval": approval}})
            return
        if tool_name == "verify":
            from tool_system.runtime import RunTrace
            from services.repair_pipeline import resume_verification_from_report

            pending_input = pending.get("input") if isinstance(pending.get("input"), dict) else {}
            verification_config = pending_input.get("verification")
            if not isinstance(verification_config, dict):
                raise DaemonHttpError(HTTPStatus.CONFLICT, {"error": "verification_config_missing"})
            verification_config = dict(verification_config)
            verification_config["approval"] = approval
            trace = RunTrace.from_dict(
                run.runtime_trace or {}, run_id=run.run_id,
                engine=(run.runtime_trace or {}).get("engine"),
            )
            pipeline = resume_verification_from_report(
                report_dir=Path(run.report_dir).expanduser().resolve(),
                verification_config=verification_config,
                trace=trace,
                run_id=run.run_id,
            )
            payload = pipeline.verification_result or {}
            run.pending_verification = payload
            run.pending_tool_approval = pipeline.pending_tool_approval
            run.approval = payload.get("approval") if isinstance(payload.get("approval"), dict) else approval
            _adopt_runtime_payload(run, pipeline.runtime_state or run.runtime_state)
            run.runtime_trace = trace.snapshot()
            logical_status = str(pipeline.result_updates.get("status") or "error")
            _set_transport_status(run, _map_logical_transport_status(logical_status))
            run.error = str(pipeline.result_updates.get("error") or "") or None
            run.completion_reason = str(pipeline.result_updates.get("completion_reason") or "") or None
            run.finished_at = time.time() if run.status != "verification_pending" else None
            result_payload = {"status": run.status, "approval": run.approval,
                              "verification": payload}
            run.events.put(RunEvent(run.run_id, "tool_approval_finished", result_payload))
            if run.status != "verification_pending":
                _persist_result(run)
            _persist_run_state(run)
            _json_response(handler, HTTPStatus.OK, _run_public_dict(run) | {"tool_approval": result_payload})
            return
        from tool_system.runtime import RunTrace
        from services.repair_pipeline import resume_tool_approval_from_report

        if not run.report_dir:
            raise DaemonHttpError(HTTPStatus.CONFLICT, {"error": "report_dir_missing"})
        trace = RunTrace.from_dict(
            run.runtime_trace or {}, run_id=run.run_id,
            engine=(run.runtime_trace or {}).get("engine"),
        )
        runtime = _get_ts_agent_runtime((run.runtime_trace or {}).get("engine"))
        tool_executor = None
        if hasattr(runtime, "execute_tool"):
            tool_executor = lambda name, data: runtime.execute_tool(name, data)
        llm_adapter = getattr(getattr(runtime, "executor", None), "llm_adapter", None)
        try:
            pipeline = resume_tool_approval_from_report(
                report_dir=Path(run.report_dir).expanduser().resolve(),
                tool_name=tool_name,
                pending=pending,
                approval=approval,
                trace=trace,
                run_id=run.run_id,
                tool_executor=tool_executor,
                runtime_state=run.runtime_state,
                llm_adapter=llm_adapter,
            )
            run.approval = consume_approval(
                approval,
                fingerprint=fingerprint,
                run_id=run_id,
                tool_call_id=tool_call_id,
                scope=scope,
            )
            _adopt_runtime_payload(run, pipeline.runtime_state or run.runtime_state)
            run.runtime_trace = trace.snapshot()
            logical_status = str(pipeline.result_updates.get("status") or "done")
            _set_transport_status(run, _map_logical_transport_status(logical_status))
            run.error = str(pipeline.result_updates.get("error") or "") or None
            run.completion_reason = str(pipeline.result_updates.get("completion_reason") or "") or None
            run.pending_verification = pipeline.verification_result
            run.pending_tool_approval = pipeline.pending_tool_approval
            run.finished_at = time.time() if run.status not in {"verification_pending", "approval_required"} else None
            result_payload = {
                "status": run.status,
                "approval": run.approval,
                "output": pipeline.applied_fix_result or pipeline.verification_result,
            }
            if run.status == "approval_required":
                result_payload["pending_tool_approval"] = pipeline.pending_tool_approval
            if run.status not in {"verification_pending", "approval_required"}:
                run.pending_tool_approval = None
            run.events.put(RunEvent(run.run_id, "tool_approval_finished", result_payload))
            if run.status not in {"verification_pending", "approval_required"}:
                _persist_result(run)
            _persist_run_state(run)
            _json_response(handler, HTTPStatus.OK, _run_public_dict(run) | {"tool_approval": result_payload})
        except Exception as exc:
            result_payload = {"status": "error", "approval": approval, "error": str(exc)}
            _set_transport_status(run, "error")
            run.error = str(exc)
            run.events.put(RunEvent(run.run_id, "tool_approval_finished", result_payload))
            _persist_run_state(run)
            _json_response(handler, HTTPStatus.OK, _run_public_dict(run) | {"tool_approval": result_payload})
    except DaemonHttpError as exc:
        _json_response(handler, int(exc.status), exc.payload)
    except Exception as exc:
        _json_response(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "tool_approval_error", "message": str(exc)})


def _respond_cleanup(handler: BaseHTTPRequestHandler, run_id: str) -> None:
    run = RUNS.get(run_id)
    if not run:
        _json_response(handler, HTTPStatus.NOT_FOUND, {"error": "run_not_found"})
        return
    if run.status == "running":
        _json_response(handler, HTTPStatus.CONFLICT, {"error": "run_still_running"})
        return
    try:
        body = _read_json_body(handler)
        workspace = run.pending_workspace or _restore_workspace_from_manifest(run)
        removed = []
        if workspace is not None:
            from services.git_worktree_manager import cleanup_isolated_workspace
            removed = cleanup_isolated_workspace(workspace, force=bool(body.get("force", False)))
        run.pending_workspace = None
        run.workspace_dir = None
        run.events.put(RunEvent(run.run_id, "workspace_cleaned", {"removed": removed}))
        _persist_run_state(run)
        _json_response(handler, HTTPStatus.OK, {"run_id": run_id, "status": run.status, "removed": removed})
    except Exception as exc:
        _json_response(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "cleanup_error", "message": str(exc)})


def _scheduler_snapshot() -> Dict[str, int]:
    """Queue counters; start the pool lazily so /health works before the first POST."""
    return ensure_run_scheduler().snapshot()


def _cancel_run(run: RunState) -> None:
    """Cancel a queued job in-place, or terminate a running CLI subprocess."""
    run.cancel_requested = True
    if run.status == "queued":
        _set_transport_status(run, "canceled")
        run.finished_at = time.time()
        run.error = "任务已取消，未产出分析结果（尚未启动）"
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
    if run.status != "running":
        return
    if run.process:
        try:
            run.process.terminate()
        except Exception:
            pass
        time.sleep(0.2)
        if run.process.poll() is None:
            try:
                run.process.kill()
            except Exception:
                pass
        _set_transport_status(run, "canceled")
        run.finished_at = time.time()
        run.error = "任务已取消，未产出分析结果（exit_code=-15）"
        run.events.put(RunEvent(run.run_id, "run_canceled", {"phase": "running"}))
        return


def _start_run(req_dict: Dict[str, Any]) -> RunState:
    """Enqueue a run on the worker pool. Copies HTTP-thread ContextVars into the worker."""
    if _SHUTTING_DOWN:
        raise DaemonHttpError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {
                "error": "shutting_down",
                "message": "服务正在停机，请稍后重新提交分析任务",
            },
        )
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
        except DaemonHttpError as exc:
            _json_response(handler, int(exc.status), exc.payload)
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
        except DaemonHttpError as exc:
            _json_response(handler, int(exc.status), exc.payload)
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
        except DaemonHttpError as exc:
            _json_response(handler, int(exc.status), exc.payload)
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
        except DaemonHttpError as exc:
            _json_response(handler, int(exc.status), exc.payload)
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
        if not _ACCESS_LOG:
            return
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format % args))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = {key: (vals[-1] if vals else "") for key, vals in parse_qs(parsed.query).items()}

        if path == "/health":
            payload = {
                "ok": True,
                "service": "stability-analysis-agent",
                "protocol_version": PROTOCOL_VERSION,
                "pid": os.getpid(),
                "web_ui": True,
            }
            payload.update(_scheduler_snapshot())
            payload.update(_health_package_info())
            payload["shutting_down"] = bool(_SHUTTING_DOWN)
            payload["runs_retained"] = len(RUNS.list())
            payload["max_body_bytes"] = _max_json_body_bytes()
            _json_response(self, HTTPStatus.OK, payload)
            return

        if _handle_skills_get(self, path):
            return

        if _handle_web_get(self, path):
            return

        if path in ("/tool-system/tools", "/tool-system/workflows"):
            try:
                engine = _resolve_tool_system_engine(query.get("engine"))
                executor = _get_ts_agent_runtime(engine)
                active = executor.executor.list_active() if hasattr(executor, "executor") else executor.list_active()
                _json_response(self, HTTPStatus.OK, active)
            except ValueError as exc:
                if "engine must be one of" in str(exc):
                    _json_response(self, HTTPStatus.BAD_REQUEST, {"error": "invalid_engine", "message": str(exc)})
                else:
                    raise
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

        if path == "/workspaces":
            try:
                from services.git_worktree_manager import scan_worktree_runs
                active = {run.workspace_dir for run in RUNS.list().values() if run.workspace_dir}
                items = [item for item in scan_worktree_runs() if item.get("path") not in active]
                _json_response(self, HTTPStatus.OK, {"workspaces": items})
            except Exception as exc:
                _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
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

            try:
                after_id = int(self.headers.get("Last-Event-ID") or query.get("after") or 0)
            except ValueError:
                after_id = 0
            if after_id > 0:
                for replay in list(run.event_log):
                    if replay.seq > after_id:
                        self.wfile.write(f"data: {json.dumps(replay.to_dict(), ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
                # Drop already replayed live events without calling ``put``;
                # calling put would assign a new sequence number.
                with run.events.mutex:
                    retained = [item for item in list(run.events.queue)
                                if not isinstance(item, RunEvent) or item.seq > after_id]
                    run.events.queue.clear()
                    run.events.queue.extend(retained)

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
                    if run.status in ("done", "error", "canceled", "verification_pending"):
                        break
                    keep = RunEvent(run_id, "keepalive", {}).to_dict()
                    self.wfile.write(f"data: {json.dumps(keep, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
            return

        if path.startswith("/runs/") and path.endswith("/checkpoints"):
            _respond_checkpoints(self, path.split("/")[2])
            return

        result_id = _single_id_path(path, "/result/")
        if result_id:
            _respond_result(self, result_id, result_format=str(query.get("format") or ""))
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
            _respond_result(self, run_id, result_format=str(query.get("format") or ""))
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
            if _SHUTTING_DOWN:
                _json_response(
                    self,
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "error": "shutting_down",
                        "message": "服务正在停机，请稍后重新提交分析任务",
                    },
                    headers={"Retry-After": "30"},
                )
                return
            try:
                body = _read_json_body(self)
                if not isinstance(body, dict):
                    raise DaemonHttpError(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_json", "message": "请求体必须是 JSON object"},
                    )
                body = _prepare_http_run_body(body)
                key = _extract_idempotency_key(self, body)
                fingerprint = _run_fingerprint(body)
                if key:
                    existing_id = _idempotency_lookup(key, fingerprint)
                    if existing_id:
                        existing = RUNS.get(existing_id)
                        if existing is not None:
                            _json_response(self, HTTPStatus.OK, _created_run_payload(existing))
                            return
                run = _start_run(body)
                if key:
                    _idempotency_store(key, fingerprint, run.run_id)
                _json_response(self, HTTPStatus.OK, _created_run_payload(run))
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
            except ValueError as exc:
                if "engine must be one of" in str(exc):
                    _json_response(self, HTTPStatus.BAD_REQUEST, {"error": "invalid_engine", "message": str(exc)})
                else:
                    raise
            except DaemonHttpError as exc:
                _json_response(self, int(exc.status), exc.payload)
            except Exception as e:
                _json_response(
                    self,
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "internal_error", "message": str(e)},
                )
            return

        cancel_id = _single_id_path(path, "/cancel/")
        if cancel_id:
            _respond_cancel(self, cancel_id)
            return

        if path.startswith("/runs/") and path.endswith("/cancel"):
            run_id = path.split("/")[2]
            _respond_cancel(self, run_id)
            return

        if path.startswith("/runs/") and path.endswith("/verification"):
            run_id = path.split("/")[2]
            _respond_verification(self, run_id)
            return

        if path.startswith("/runs/") and path.endswith("/resume"):
            _respond_resume(self, path.split("/")[2])
            return

        if path.startswith("/runs/") and path.endswith("/tool-approval"):
            _respond_tool_approval(self, path.split("/")[2])
            return

        if path.startswith("/runs/") and path.endswith("/retry-stage"):
            _respond_retry_stage(self, path.split("/")[2])
            return

        if path.startswith("/runs/") and path.endswith("/cleanup"):
            run_id = path.split("/")[2]
            _respond_cleanup(self, run_id)
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
                result = _run_tool_system_workflow(workflow_name, {
                    "crash_log": crash_log,
                    "library_dir": body.get("library_dir"),
                    "code_roots": body.get("code_roots", []),
                    "force_anr_analysis": force_anr,
                    "scope": body.get("scope", "full"),
                    "engine": body.get("engine"),
                }, engine=body.get("engine"))
                _json_response(self, HTTPStatus.OK, result)
            except ValueError as exc:
                if "engine must be one of" in str(exc):
                    _json_response(self, HTTPStatus.BAD_REQUEST, {"error": "invalid_engine", "message": str(exc)})
                else:
                    raise
            except DaemonHttpError as exc:
                _json_response(self, int(exc.status), exc.payload)
            except Exception as e:
                _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})
            return

        if path == "/tool-system/native-leak":
            try:
                body = _read_json_body(self)
                result = _run_tool_system_workflow("native_leak_analysis", {
                    "native_leak_path": body.get("path") or body.get("native_leak_dir"),
                    "native_leak_trace_db": body.get("trace_db") or body.get("native_leak_trace_db"),
                    "code_roots": body.get("code_roots", []),
                    "scope": body.get("scope", "gen_prompt_only"),
                    "max_callchains": body.get("max_callchains", 5),
                    "min_callchain_percentage": body.get("min_callchain_percentage", 0.0),
                    "engine": body.get("engine"),
                }, engine=body.get("engine"))
                _json_response(self, HTTPStatus.OK, result)
            except ValueError as exc:
                if "engine must be one of" in str(exc):
                    _json_response(self, HTTPStatus.BAD_REQUEST, {"error": "invalid_engine", "message": str(exc)})
                else:
                    raise
            except DaemonHttpError as exc:
                _json_response(self, int(exc.status), exc.payload)
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
    parser.add_argument(
        "--shutdown-wait",
        type=int,
        default=_env_int("STABILITY_AGENT_DAEMON_SHUTDOWN_WAIT_SEC", 90),
        help="Seconds to wait for CLI subprocesses after SIGTERM (env STABILITY_AGENT_DAEMON_SHUTDOWN_WAIT_SEC)",
    )
    parser.add_argument(
        "--run-ttl",
        type=int,
        default=_env_int("STABILITY_AGENT_DAEMON_RUN_TTL_SEC", 6 * 60 * 60),
        help="Seconds to keep finished runs in memory; 0 disables (env STABILITY_AGENT_DAEMON_RUN_TTL_SEC)",
    )
    parser.add_argument(
        "--event-queue-max",
        type=int,
        default=_env_int("STABILITY_AGENT_DAEMON_EVENT_QUEUE_MAX", 256),
        help="Per-run SSE queue size; oldest events drop (env STABILITY_AGENT_DAEMON_EVENT_QUEUE_MAX)",
    )
    parser.add_argument(
        "--max-body-bytes",
        type=int,
        default=_env_int("STABILITY_AGENT_DAEMON_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES),
        help="Max JSON request body bytes; oversize is HTTP 413 (env STABILITY_AGENT_DAEMON_MAX_BODY_BYTES)",
    )
    parser.add_argument(
        "--deny-local-path-fields",
        action="store_true",
        help="Reject POST /runs fields that point at server-local paths",
    )
    parser.add_argument(
        "--allow-local-path-fields",
        action="store_true",
        help="Allow crash_log/code_roots/library_dir in POST /runs (local Web UI)",
    )
    parser.add_argument(
        "--access-log",
        action="store_true",
        default=_env_int("STABILITY_AGENT_DAEMON_ACCESS_LOG", 0) == 1,
        help="Log method/path/status to stderr (env STABILITY_AGENT_DAEMON_ACCESS_LOG=1)",
    )
    args = parser.parse_args()
    os.environ["STABILITY_AGENT_DAEMON_SHUTDOWN_WAIT_SEC"] = str(max(0, int(args.shutdown_wait)))
    os.environ["STABILITY_AGENT_DAEMON_RUN_TTL_SEC"] = str(max(0, int(args.run_ttl)))
    os.environ["STABILITY_AGENT_DAEMON_EVENT_QUEUE_MAX"] = str(max(1, int(args.event_queue_max)))
    os.environ["STABILITY_AGENT_DAEMON_MAX_BODY_BYTES"] = str(max(1, int(args.max_body_bytes)))
    global _ACCESS_LOG
    _ACCESS_LOG = bool(args.access_log) or _env_int("STABILITY_AGENT_DAEMON_ACCESS_LOG", 0) == 1
    if args.allow_local_path_fields:
        set_deny_local_path_fields(False)
    elif args.deny_local_path_fields or _env_int("STABILITY_AGENT_DAEMON_DENY_LOCAL_PATH_FIELDS", 0) == 1:
        set_deny_local_path_fields(True)
    ensure_run_scheduler(
        max_workers=args.max_workers,
        max_queue=args.max_queue,
        run_timeout_sec=args.run_timeout,
    )

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)

    def _shutdown(*_):
        global _SHUTTING_DOWN
        _SHUTTING_DOWN = True
        threading.Thread(target=httpd.shutdown, name="httpd-shutdown", daemon=True).start()

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
    print(f"  deny_local_path_fields={_DENY_LOCAL_PATH_FIELDS} access_log={_ACCESS_LOG}")
    print("  Web preferences: GET/POST /web/preferences")
    print("  Skills API:      GET /skills  GET /skills/<name>")
    print("                   POST /skills/install  POST /skills/lint  POST /skills/uninstall")
    print("  Tool System API: POST /tool-system/analyze  POST /tool-system/native-leak")
    print("                   GET /tool-system/tools  GET /tool-system/workflows")
    httpd.serve_forever()
    wait_sec = _env_int("STABILITY_AGENT_DAEMON_SHUTDOWN_WAIT_SEC", 90)
    print(
        f"daemon shutting down; cancelling active runs (wait up to {wait_sec}s)",
        file=sys.stderr,
    )
    _drain_active_runs(wait_sec)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
