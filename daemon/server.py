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
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLI_ENTRY = PROJECT_ROOT / "cli" / "main.py"
DEFAULT_REPORT_DIR = PROJECT_ROOT / "cli_reports"

# 允许从任意 cwd 直接运行：python3 daemon/server.py
sys.path.insert(0, str(PROJECT_ROOT.parent))

from stability_analyzer_agent.protocol.models import RunEvent, RunRequest, RunResult, normalize_run_code_roots
from stability_analyzer_agent.protocol.version import PROTOCOL_VERSION


# ---------------------------------------------------------------------------
# Tool System executor（用于 /tool-system/* 端点）
# ---------------------------------------------------------------------------
_ts_executor = None
_ts_lock = threading.Lock()


def _get_ts_executor():
    """延迟初始化 tool_system ConfigDrivenExecutor（仅在首次调用 /tool-system/* 时触发）。"""
    global _ts_executor
    if _ts_executor is not None:
        return _ts_executor
    with _ts_lock:
        if _ts_executor is not None:
            return _ts_executor
        try:
            from stability_analyzer_agent.tool_system import (
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

    process: Optional[subprocess.Popen] = None
    events: "queue.Queue[RunEvent]" = queue.Queue()
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


RUNS = RunManager()


def _json_response(handler: BaseHTTPRequestHandler, code: int, payload: Dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _read_json_body(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(length) if length > 0 else b"{}"
    return json.loads(raw.decode("utf-8") or "{}")


def _build_cli_cmd(req: RunRequest) -> Tuple[list, Optional[str]]:
    """
    将 RunRequest 转成 CLI 命令行参数。
    返回 (cmd, stdin_text)
    """
    cmd = [
        "python3",
        "-u",
        str(CLI_ENTRY),
    ]

    stdin_text: Optional[str] = None

    # consultation 模式
    if req.consultation:
        cmd += ["--consultation"]
        if req.prompt:
            cmd += ["--prompt", req.prompt]
        if req.streaming:
            cmd += ["--streaming"]
        if req.config:
            cmd += ["--config", req.config]
        if req.output_format:
            cmd += ["--output-format", req.output_format]
        return cmd, None

    # analysis 模式
    if req.crash_log_content is not None:
        cmd += ["--crash-log", "-"]
        stdin_text = req.crash_log_content
    elif req.crash_log:
        cmd += ["--crash-log", req.crash_log]
    else:
        # 交给 CLI 自己报错
        cmd += ["--crash-log", ""]

    if req.library_dir:
        cmd += ["--library-dir", req.library_dir]
    for cr in normalize_run_code_roots(req):
        cmd += ["--code-root", cr]
    if req.config:
        cmd += ["--config", req.config]

    if req.output_format:
        cmd += ["--output-format", req.output_format]

    # 执行引擎（默认 sequential；当选择 langgraph 时让 CLI 走 FullStabilityAnalyzer）
    if getattr(req, "engine", None):
        cmd += ["--engine", str(req.engine)]

    scope = str(getattr(req, "scope", "full") or "full")
    if scope and scope != "full":
        cmd += ["--scope", scope]
    prompt_mode = str(getattr(req, "prompt_mode", "analysis") or "analysis")
    if prompt_mode and prompt_mode != "analysis":
        cmd += ["--prompt-mode", prompt_mode]
    agent_loop = getattr(req, "agent_loop", None)
    if agent_loop in {"single", "context_loop"}:
        cmd += ["--agent-loop", str(agent_loop)]
    max_rounds = int(getattr(req, "max_agent_rounds", 1) or 1)
    if max_rounds != 1:
        cmd += ["--max-agent-rounds", str(max_rounds)]
    max_context_requests = int(getattr(req, "max_context_requests_per_round", 5) or 5)
    if max_context_requests != 5:
        cmd += ["--max-context-requests-per-round", str(max_context_requests)]
    if req.optimized:
        cmd += ["--optimized"]
    if req.streaming:
        cmd += ["--streaming"]

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


def _emit_stdout_line(run: RunState, stdout_acc: list[str], line: str) -> None:
    text = line.rstrip("\n")
    stdout_acc.append(text + "\n")
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


def _run_worker(run: RunState, req: RunRequest) -> None:
    run.status = "running"
    run.started_at = time.time()
    run.events.put(RunEvent(run.run_id, "run_started", {"request": req.to_dict()}))

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
                    run.events.put(RunEvent(run.run_id, "stderr", {"line": line.rstrip("\n")}))
            except Exception as e:
                run.events.put(RunEvent(run.run_id, "stream_error", {"which": "stderr", "error": str(e)}))

        t_out = threading.Thread(target=_pump_stdout, args=(p.stdout,), daemon=True)
        t_err = threading.Thread(target=_pump_stderr, args=(p.stderr,), daemon=True)
        t_out.start()
        t_err.start()

        exit_code = p.wait()
        run.exit_code = exit_code
        run.finished_at = time.time()

        # 读取剩余输出（保险）
        try:
            remaining_out = p.stdout.read() if p.stdout else ""
            if remaining_out:
                for line in remaining_out.splitlines():
                    _emit_stdout_line(run, stdout_acc, line + "\n")
        except Exception:
            pass

        status = "done" if exit_code == 0 else "error"
        run.status = status

        output_text = "".join(stdout_acc)
        run.result = RunResult(
            run_id=run.run_id,
            status=run.status,
            output_format=req.output_format,
            output=output_text,
            error=None if exit_code == 0 else f"process exit {exit_code}",
        )
    except Exception as e:
        run.status = "error"
        run.error = str(e)
        run.finished_at = time.time()
        run.events.put(RunEvent(run.run_id, "run_error", {"error": str(e)}))
    finally:
        run.events.put(RunEvent(run.run_id, "run_finished", {"status": run.status, "exit_code": run.exit_code}))


def _start_run(req_dict: Dict[str, Any]) -> RunState:
    # 旧字段（skip_ai / parse_stack_only）兼容：静默归并为 scope 后再丢弃。
    if "scope" not in req_dict:
        legacy_skip_ai = bool(req_dict.get("skip_ai", False))
        legacy_parse_stack_only = bool(req_dict.get("parse_stack_only", False))
        if legacy_parse_stack_only:
            req_dict["scope"] = "parse_stack_only"
        elif legacy_skip_ai:
            req_dict["scope"] = "gen_prompt_only"
    req_dict.pop("skip_ai", None)
    req_dict.pop("parse_stack_only", None)
    req = RunRequest(**req_dict)
    run = RUNS.create_run(req)

    # 在 worker 中累计 stdout（用于 /result 与落盘）
    stdout_acc: list[str] = []  # chunk list

    def worker():
        run.status = "running"
        run.started_at = time.time()
        run.events.put(RunEvent(run.run_id, "run_started", {"request": req.to_dict()}))

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
            )
            run.process = p

            if stdin_text is not None and p.stdin is not None:
                p.stdin.write(stdin_text)
                p.stdin.close()

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
                        line = line.rstrip("\n")
                        run.events.put(RunEvent(run.run_id, "stderr", {"line": line}))
                except Exception as e:
                    run.events.put(RunEvent(run.run_id, "stream_error", {"which": "stderr", "error": str(e)}))

            t_out = threading.Thread(target=_pump_stdout, args=(p.stdout,), daemon=True)
            t_err = threading.Thread(target=_pump_stderr, args=(p.stderr,), daemon=True)
            t_out.start()
            t_err.start()

            exit_code = p.wait()
            run.exit_code = exit_code
            run.finished_at = time.time()
            run.status = "done" if exit_code == 0 else "error"

            output_text = "".join(stdout_acc)
            run.result = RunResult(
                run_id=run.run_id,
                status=run.status,
                output_format=req.output_format,
                output=output_text,
                error=None if exit_code == 0 else f"process exit {exit_code}",
            )
            _persist_result(run)
        except Exception as e:
            run.status = "error"
            run.error = str(e)
            run.finished_at = time.time()
            run.result = RunResult(
                run_id=run.run_id,
                status="error",
                output_format=req.output_format,
                output="\n".join(stdout_acc),
                error=str(e),
            )
        finally:
            run.events.put(RunEvent(run.run_id, "run_finished", {"status": run.status, "exit_code": run.exit_code}))

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return run


class Handler(BaseHTTPRequestHandler):
    server_version = "AIStabilityDaemon/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        # 降噪：默认不输出到 stderr
        return

    def do_GET(self) -> None:
        if self.path == "/health":
            _json_response(
                self,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "protocol_version": PROTOCOL_VERSION,
                    "pid": os.getpid(),
                },
            )
            return

        # --- tool-system 直连端点（ConfigDrivenExecutor）---
        if self.path in ("/tool-system/tools", "/tool-system/workflows"):
            try:
                executor = _get_ts_executor()
                active = executor.list_active()
                _json_response(self, HTTPStatus.OK, active)
            except Exception as e:
                _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})
            return

        if self.path.startswith("/runs/") and self.path.endswith("/events"):
            run_id = self.path.split("/")[2]
            run = RUNS.get(run_id)
            if not run:
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "run_not_found"})
                return

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            # 先发一个 hello
            hello = RunEvent(run_id, "events_opened", {}).to_dict()
            self.wfile.write(f"data: {json.dumps(hello, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

            # 持续输出事件直到 run 结束且队列清空
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
                    # keepalive
                    keep = RunEvent(run_id, "keepalive", {}).to_dict()
                    self.wfile.write(f"data: {json.dumps(keep, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
            return

        if self.path.startswith("/runs/") and self.path.endswith("/result"):
            run_id = self.path.split("/")[2]
            run = RUNS.get(run_id)
            if not run:
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "run_not_found"})
                return
            if not run.result:
                _json_response(self, HTTPStatus.ACCEPTED, {"status": run.status})
                return
            _json_response(self, HTTPStatus.OK, run.result.to_dict())
            return

        if self.path.startswith("/runs/"):
            run_id = self.path.split("/")[2]
            run = RUNS.get(run_id)
            if not run:
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "run_not_found"})
                return
            _json_response(
                self,
                HTTPStatus.OK,
                {
                    "run_id": run.run_id,
                    "status": run.status,
                    "created_at": run.created_at,
                    "started_at": run.started_at,
                    "finished_at": run.finished_at,
                    "exit_code": run.exit_code,
                    "error": run.error,
                    "output_format": run.output_format,
                },
            )
            return

        _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path == "/runs":
            try:
                body = _read_json_body(self)
                run = _start_run(body)
                _json_response(self, HTTPStatus.OK, {"run_id": run.run_id})
            except Exception as e:
                _json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(e)})
            return

        if self.path.startswith("/runs/") and self.path.endswith("/cancel"):
            run_id = self.path.split("/")[2]
            run = RUNS.get(run_id)
            if not run:
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "run_not_found"})
                return
            if run.process and run.status == "running":
                try:
                    run.process.terminate()
                    time.sleep(0.2)
                    if run.process.poll() is None:
                        run.process.kill()
                    run.status = "canceled"
                    run.finished_at = time.time()
                    run.events.put(RunEvent(run_id, "run_canceled", {}))
                except Exception as e:
                    _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})
                    return
            _json_response(self, HTTPStatus.OK, {"run_id": run_id, "status": run.status})
            return

        # --- tool-system 直连端点（ConfigDrivenExecutor）---
        if self.path == "/tool-system/analyze":
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

        _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not_found"})


def main() -> int:
    parser = argparse.ArgumentParser(description="Stability Analysis Agent Local Daemon (HTTP)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)

    def _shutdown(*_):
        httpd.shutdown()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    print(f"daemon listening on http://{args.host}:{args.port} (protocol={PROTOCOL_VERSION})")
    print(f"  Run API:         POST /runs  GET /runs/<id>  GET /runs/<id>/events  POST /runs/<id>/cancel")
    print(f"  Tool System API: POST /tool-system/analyze  GET /tool-system/tools  GET /tool-system/workflows")
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

