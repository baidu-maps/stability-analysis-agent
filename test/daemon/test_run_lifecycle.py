#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Daemon run lifecycle contract tests without invoking the real AI."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from daemon import server
from protocol.models import RunRequest


class _FakeProcess:
    def __init__(self, output: str = '{"status":"success"}\n', returncode: int = 0) -> None:
        import io

        self.stdout = io.StringIO(output)
        self.stderr = io.StringIO("")
        self.stdin = None
        self._returncode = returncode
        self.terminated = False

    def wait(self, timeout=None):
        return self._returncode

    def poll(self):
        return self._returncode if self.terminated else None

    def terminate(self):
        self.terminated = True
        self._returncode = -15

    def kill(self):
        self.terminated = True
        self._returncode = -9


class _BlockingProcess(_FakeProcess):
    def __init__(self, gate: threading.Event) -> None:
        super().__init__()
        self.gate = gate

    def wait(self, timeout=None):
        self.gate.wait(timeout=10)
        return self._returncode

    def poll(self):
        if self.gate.is_set():
            return self._returncode
        return None


class DaemonRunLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        server.RUNS = server.RunManager()
        server.reset_run_scheduler_for_tests(max_workers=2, max_queue=32)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def _request(self, method: str, path: str, body=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read().decode("utf-8") or "{}")
        conn.close()
        return response.status, data

    def test_run_reaches_done_and_exposes_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(server, "DEFAULT_REPORT_DIR", Path(tmp)), mock.patch.object(
            server.subprocess, "Popen", return_value=_FakeProcess()
        ):
            status, created = self._request("POST", "/runs", {"crash_log": "/tmp/demo.crash", "output_format": "json"})
            self.assertEqual(status, 200)
            run_id = created["run_id"]
            deadline = time.monotonic() + 3
            state = {}
            while time.monotonic() < deadline:
                _, state = self._request("GET", f"/runs/{run_id}")
                if state.get("status") == "done":
                    break
                time.sleep(0.01)
            self.assertEqual(state.get("status"), "done")
            status, result = self._request("GET", f"/runs/{run_id}/result")
            self.assertEqual(status, 200)
            self.assertEqual(result["status"], "done")
            self.assertIn('"status":"success"', result["output"])

    def test_each_run_has_an_isolated_event_queue(self) -> None:
        first = server.RUNS.create_run(RunRequest(crash_log="a"))
        second = server.RUNS.create_run(RunRequest(crash_log="b"))
        self.assertIsNot(first.events, second.events)
        first.events.put("first-only")
        self.assertTrue(second.events.empty())

    def test_canceled_process_is_not_reclassified_as_error(self) -> None:
        run = server.RUNS.create_run(RunRequest(crash_log="a"))
        def fake_popen(*args, **kwargs):
            run.status = "canceled"
            return _FakeProcess(returncode=-15)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(server, "DEFAULT_REPORT_DIR", Path(tmp)), mock.patch.object(
            server.subprocess, "Popen", side_effect=fake_popen
        ):
            server._run_worker(run, RunRequest(crash_log="a"))
        self.assertEqual(run.status, "canceled")
        self.assertIsNotNone(run.result)
        self.assertEqual(run.result.status, "canceled")

    def test_start_run_copies_contextvars_into_worker(self) -> None:
        """HTTP 线程里 set 的 ContextVar 必须能被 worker 读到。"""
        import contextvars

        marker = contextvars.ContextVar("daemon_context_probe", default=None)
        seen = []
        done = threading.Event()

        def spy_worker(run, req) -> None:
            seen.append(marker.get())
            done.set()

        token = marker.set("from-http-thread")
        try:
            with mock.patch.object(server, "_run_worker", spy_worker):
                server._start_run({"crash_log": "/tmp/demo.crash"})
                self.assertTrue(done.wait(2.0))
        finally:
            marker.reset(token)
        self.assertEqual(seen, ["from-http-thread"])

    def test_health_and_list_expose_scheduler(self) -> None:
        status, health = self._request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(health.get("ok"))
        self.assertEqual(health.get("max_workers"), 2)
        self.assertEqual(health.get("max_queue"), 32)
        self.assertEqual(health.get("service"), "stability-analysis-agent")
        self.assertIn("queued", health)
        self.assertIn("running", health)
        status, listed = self._request("GET", "/runs")
        self.assertEqual(status, 200)
        self.assertEqual(listed.get("runs"), [])
        self.assertEqual(listed.get("max_workers"), 2)

    def test_queue_full_returns_429(self) -> None:
        gate = threading.Event()
        server.reset_run_scheduler_for_tests(max_workers=1, max_queue=1)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            server, "DEFAULT_REPORT_DIR", Path(tmp)
        ), mock.patch.object(
            server.subprocess, "Popen", return_value=_BlockingProcess(gate)
        ):
            try:
                status, first = self._request("POST", "/runs", {"crash_log": "/tmp/a.crash"})
                self.assertEqual(status, 200)
                deadline = time.monotonic() + 2
                state = {}
                while time.monotonic() < deadline:
                    _, state = self._request("GET", f"/runs/{first['run_id']}")
                    if state.get("status") == "running":
                        break
                    time.sleep(0.01)
                self.assertEqual(state.get("status"), "running")
                status, second = self._request("POST", "/runs", {"crash_log": "/tmp/b.crash"})
                self.assertEqual(status, 200)
                _, queued = self._request("GET", f"/runs/{second['run_id']}")
                self.assertEqual(queued.get("status"), "queued")
                status, busy = self._request("POST", "/runs", {"crash_log": "/tmp/c.crash"})
                self.assertEqual(status, 429)
                self.assertEqual(busy.get("error"), "queue_full")
                status, cancel = self._request("POST", f"/runs/{second['run_id']}/cancel")
                self.assertEqual(status, 200)
                self.assertEqual(cancel.get("status"), "canceled")
                _, listed = self._request("GET", "/runs")
                ids = {item["run_id"]: item["status"] for item in listed["runs"]}
                self.assertEqual(ids.get(second["run_id"]), "canceled")
            finally:
                gate.set()

    def test_short_aliases_and_report_field(self) -> None:
        output = (
            "[阶段 1/5] 解析崩溃日志 ✓\n"
            "__SDK_RELEASE_STATUS__ {}\n"
            "# 崩溃分析结果\n"
            "根因说明\n"
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            server, "DEFAULT_REPORT_DIR", Path(tmp)
        ), mock.patch.object(server.subprocess, "Popen", return_value=_FakeProcess(output)):
            status, created = self._request("POST", "/runs", {"crash_log": "/tmp/demo.crash"})
            self.assertEqual(status, 200)
            run_id = created["run_id"]
            self.assertEqual(created["links"]["status"], f"/status/{run_id}")
            self.assertEqual(created["links"]["result"], f"/result/{run_id}")
            self.assertEqual(created["links"]["cancel"], f"/cancel/{run_id}")
            deadline = time.monotonic() + 3
            state = {}
            while time.monotonic() < deadline:
                _, state = self._request("GET", f"/status/{run_id}")
                if state.get("status") == "done":
                    break
                time.sleep(0.01)
            self.assertEqual(state.get("status"), "done")
            self.assertEqual(state.get("message"), "分析完成")
            self.assertIn("解析崩溃日志", state.get("progress") or "")
            status, result = self._request("GET", f"/result/{run_id}")
            self.assertEqual(status, 200)
            self.assertTrue(result["report"].startswith("# 崩溃分析结果"))
            self.assertIn("__SDK_RELEASE_STATUS__", result["output"])
            self.assertNotIn("__SDK_RELEASE_STATUS__", result["report"])

    def test_cancel_alias_stops_queued_job(self) -> None:
        gate = threading.Event()
        server.reset_run_scheduler_for_tests(max_workers=1, max_queue=8)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            server, "DEFAULT_REPORT_DIR", Path(tmp)
        ), mock.patch.object(
            server.subprocess, "Popen", return_value=_BlockingProcess(gate)
        ):
            try:
                status, first = self._request("POST", "/runs", {"crash_log": "/tmp/a.crash"})
                self.assertEqual(status, 200)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    _, state = self._request("GET", f"/status/{first['run_id']}")
                    if state.get("status") == "running":
                        break
                    time.sleep(0.01)
                status, second = self._request("POST", "/runs", {"crash_log": "/tmp/b.crash"})
                self.assertEqual(status, 200)
                status, canceled = self._request("POST", f"/cancel/{second['run_id']}")
                self.assertEqual(status, 200)
                self.assertEqual(canceled.get("status"), "canceled")
            finally:
                gate.set()


if __name__ == "__main__":
    unittest.main()
