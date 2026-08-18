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

    def wait(self):
        return self._returncode

    def poll(self):
        return self._returncode if self.terminated else None

    def terminate(self):
        self.terminated = True
        self._returncode = -15

    def kill(self):
        self.terminated = True
        self._returncode = -9


class DaemonRunLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        server.RUNS = server.RunManager()
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


if __name__ == "__main__":
    unittest.main()
