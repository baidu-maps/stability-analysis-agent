#!/usr/bin/env python3
# -*- coding: utf-8 -*-

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


def _write_eligible_report(report_dir: Path) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "01_crash_log_parser.json").write_text(
        json.dumps(
            {
                "crash_info": {"signal": "SIGSEGV", "crash_reason": "segv"},
                "meta_info": {"os_type": "macos"},
            }
        ),
        encoding="utf-8",
    )
    (report_dir / "02_add2line_resolver.json").write_text(
        json.dumps(
            {
                "resolved_threads": [
                    {
                        "frames": [
                            {
                                "resolved_function": "foo",
                                "module": "libx.so",
                                "resolved_file": "a.cpp",
                                "resolved_line": 1,
                            }
                        ]
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (report_dir / "08_apply_ai_fixes.json").write_text(
        json.dumps({"success": True, "applied": [{"status": "applied", "file": "a.cpp"}]}),
        encoding="utf-8",
    )


class VectorDbCommitApiTests(unittest.TestCase):
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

    def test_commit_requires_finished_run_with_report_dir(self) -> None:
        run = server.RUNS.create_run(RunRequest(crash_log="/tmp/a.crash"))
        status, data = self._request("POST", f"/runs/{run.run_id}/vector-db/commit", {})
        self.assertEqual(status, 409)
        self.assertEqual(data["error"], "run_not_finished")

        run.status = "done"
        status, data = self._request("POST", f"/runs/{run.run_id}/vector-db/commit", {})
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], "report_dir_missing")

    def test_commit_success_writes_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = Path(tmp) / "report"
            _write_eligible_report(report_dir)
            run = server.RUNS.create_run(RunRequest(crash_log="/tmp/a.crash"))
            run.status = "done"
            run.report_dir = str(report_dir)
            with mock.patch(
                "rag.case_writer.commit_from_report_dir",
                return_value={"ok": True, "pattern_id": "pattern_demo", "vector_db_path": str(tmp)},
            ):
                status, data = self._request("POST", f"/runs/{run.run_id}/vector-db/commit", {})
            self.assertEqual(status, 200)
            self.assertTrue(data["ok"])
            self.assertEqual(data["pattern_id"], "pattern_demo")
            audit = json.loads((report_dir / "09_vector_db_commit.json").read_text(encoding="utf-8"))
            self.assertEqual(audit["source"], "web")

    def test_remote_mode_returns_501(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = Path(tmp) / "report"
            _write_eligible_report(report_dir)
            run = server.RUNS.create_run(RunRequest(crash_log="/tmp/a.crash"))
            run.status = "done"
            run.report_dir = str(report_dir)
            with mock.patch(
                "rag.case_writer.commit_from_report_dir",
                return_value={"ok": False, "error": "remote vector store is not implemented yet"},
            ):
                status, data = self._request("POST", f"/runs/{run.run_id}/vector-db/commit", {})
            self.assertEqual(status, 501)
            self.assertIn("not implemented", data.get("error", "").lower())

    def test_capture_report_dir_from_stderr_line(self) -> None:
        run = server.RUNS.create_run(RunRequest(crash_log="/tmp/a.crash"))
        server._capture_report_dir_from_line(run, "report 已保存到: /tmp/reports/demo_run")
        self.assertEqual(run.report_dir, "/tmp/reports/demo_run")


if __name__ == "__main__":
    unittest.main()
