#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Daemon report failure-field enrichment for /status and /result."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from daemon import server


class ReportFailureFieldTests(unittest.TestCase):
    """Pure helpers that surface completion_reason / fix_notes to HTTP callers."""

    def test_compose_safety_check_failed_detail(self) -> None:
        detail = server._compose_failure_detail(
            completion_reason="safety_check_failed",
            stage_errors=[{"stage": "apply_ai_fixes", "message": "safety checks failed: change_manifest"}],
            fix_notes=[
                {
                    "file": "walk_guidance_control.cpp",
                    "function": "DemoGuidanceControl::GetDrawSteps()",
                    "status": "skipped",
                    "error": "replacement_code 的类名/函数名与目标签名不一致，已拒绝写入",
                }
            ],
            applied={"success": False, "error": "safety checks failed: change_manifest"},
        )
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertIn("已回滚全部改动", detail)
        self.assertIn("apply_ai_fixes", detail)
        self.assertIn("walk_guidance_control.cpp", detail)
        self.assertIn("skipped", detail)

    def test_fix_notes_only_non_applied(self) -> None:
        notes = server._fix_notes_from_applied(
            {
                "applied": [
                    {"file": "/tmp/a.cpp", "status": "applied", "function_signature": "A::ok()"},
                    {
                        "file": "/tmp/b.cpp",
                        "status": "skipped",
                        "function_signature": "B::skip()",
                        "error": "equivalent",
                    },
                    {"file": "/tmp/c.cpp", "status": "rolled_back", "error": "safety"},
                ]
            }
        )
        self.assertEqual(len(notes), 2)
        self.assertEqual(notes[0]["file"], "b.cpp")
        self.assertEqual(notes[0]["status"], "skipped")
        self.assertEqual(notes[1]["status"], "rolled_back")

    def test_attach_fields_backfills_completion_reason_and_rewrites_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp)
            (report / "00_run_summary.json").write_text(
                json.dumps(
                    {
                        "completion_reason": "safety_check_failed",
                        "stages": [
                            {"name": "llm_analysis", "status": "success"},
                            {"name": "apply_ai_fixes", "status": "failed"},
                        ],
                        "errors": [
                            {
                                "stage": "apply_ai_fixes",
                                "message": "safety checks failed: change_manifest",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (report / "08_apply_ai_fixes.json").write_text(
                json.dumps(
                    {
                        "success": False,
                        "error": "safety checks failed: change_manifest",
                        "applied": [
                            {
                                "file": str(report / "src" / "BmObj.cpp"),
                                "status": "skipped",
                                "function_signature": "BmObj::foo()",
                                "error": "replacement_code 与原函数等价",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            run = server.RunState(
                run_id="run-diag-1",
                transport_status="error",
                created_at=0.0,
                report_dir=str(report),
                last_progress="[阶段 5/5] 第一轮：AI推理分析 ✓ 2m15s",
                error="分析未正常完成；中止于 [阶段 5/5] 第一轮：AI推理分析 ✓ 2m15s",
            )
            payload = server._attach_report_failure_fields(
                run,
                {
                    "run_id": run.run_id,
                    "status": run.status,
                    "error": run.error,
                    "completion_reason": None,
                },
            )
            self.assertEqual(payload["completion_reason"], "safety_check_failed")
            self.assertEqual(run.completion_reason, "safety_check_failed")
            self.assertEqual(payload["stage_status"]["apply_ai_fixes"], "failed")
            self.assertEqual(payload["stage_errors"][0]["stage"], "apply_ai_fixes")
            self.assertEqual(payload["fix_notes"][0]["file"], "BmObj.cpp")
            detail = str(payload["failure_detail"] or "")
            self.assertIn("已回滚全部改动", detail)
            self.assertIn("BmObj.cpp", detail)
            error = str(payload["error"] or "")
            self.assertIn("已回滚全部改动", error)
            self.assertIn("AI推理分析", error)
            self.assertEqual(run.error, error)
            self.assertIn("已回滚全部改动", str(run.error or ""))


if __name__ == "__main__":
    unittest.main()
