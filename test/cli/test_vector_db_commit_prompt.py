#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cli import main as cli_main


def _args(**overrides):
    base = {
        "no_save_to_vector_db": False,
        "save_to_vector_db": False,
        "interactive": None,
        "vector_db_path": "",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _eligible_report(tmp: str) -> Path:
    report_dir = Path(tmp)
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
    return report_dir


class VectorDbCommitPromptTests(unittest.TestCase):
    def test_non_interactive_skips_without_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = _eligible_report(tmp)
            applied = json.loads((report_dir / "08_apply_ai_fixes.json").read_text(encoding="utf-8"))
            with mock.patch.object(cli_main, "_is_tty_interactive", return_value=False):
                cli_main._maybe_commit_vector_db_after_fix(
                    _args(),
                    report_dir,
                    applied,
                    scope="full",
                )
            audit = report_dir / "09_vector_db_commit.json"
            self.assertTrue(audit.is_file())
            self.assertEqual(json.loads(audit.read_text(encoding="utf-8"))["status"], "skipped")

    def test_save_flag_commits_when_rag_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = _eligible_report(tmp)
            applied = json.loads((report_dir / "08_apply_ai_fixes.json").read_text(encoding="utf-8"))
            with mock.patch("rag.runtime.rag_stack_available", return_value=True), mock.patch(
                "rag.case_writer.commit_from_report_dir",
                return_value={"ok": True, "pattern_id": "pattern_x", "vector_db_path": "/tmp/vdb"},
            ):
                cli_main._maybe_commit_vector_db_after_fix(
                    _args(save_to_vector_db=True),
                    report_dir,
                    applied,
                    scope="full",
                )
            audit = json.loads((report_dir / "09_vector_db_commit.json").read_text(encoding="utf-8"))
            self.assertEqual(audit["status"], "committed")

    def test_interactive_prompt_decline_writes_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = _eligible_report(tmp)
            applied = json.loads((report_dir / "08_apply_ai_fixes.json").read_text(encoding="utf-8"))
            with mock.patch.object(cli_main, "_is_tty_interactive", return_value=True), mock.patch.object(
                cli_main, "_prompt_yes_no", return_value=False
            ):
                cli_main._maybe_commit_vector_db_after_fix(
                    _args(),
                    report_dir,
                    applied,
                    scope="full",
                )
            audit = json.loads((report_dir / "09_vector_db_commit.json").read_text(encoding="utf-8"))
            self.assertEqual(audit["status"], "skipped")
            self.assertEqual(audit["reason"], "user_declined")

    def test_no_save_flag_skips_even_interactive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = _eligible_report(tmp)
            applied = json.loads((report_dir / "08_apply_ai_fixes.json").read_text(encoding="utf-8"))
            with mock.patch.object(cli_main, "_prompt_yes_no") as prompt:
                cli_main._maybe_commit_vector_db_after_fix(
                    _args(no_save_to_vector_db=True),
                    report_dir,
                    applied,
                    scope="full",
                )
            prompt.assert_not_called()
            self.assertFalse((report_dir / "09_vector_db_commit.json").exists())


if __name__ == "__main__":
    unittest.main()
