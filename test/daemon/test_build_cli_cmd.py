#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for daemon RunRequest → CLI argv mapping."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from protocol.models import run_request_from_dict
from daemon.server import _build_cli_cmd


class BuildCliCmdTests(unittest.TestCase):
    def test_crash_log_dir_preferred(self) -> None:
        req = run_request_from_dict(
            {
                "crash_log_dir": "/tmp/logs",
                "crash_log": "/tmp/ignored.crash",
                "library_dir": "/tmp/lib",
                "code_roots": ["/tmp/code"],
            }
        )
        cmd, stdin = _build_cli_cmd(req)
        self.assertIsNone(stdin)
        self.assertIn("--crash-log-dir", cmd)
        self.assertIn("/tmp/logs", cmd)
        self.assertNotIn("--crash-log", cmd)
        self.assertIn("--library-dir", cmd)
        self.assertIn("--code-root", cmd)

    def test_no_apply_ai_fixes_and_no_backup(self) -> None:
        req = run_request_from_dict(
            {
                "crash_log": "/tmp/a.crash",
                "apply_ai_fixes": False,
                "backup_original_sources": False,
                "scope": "full",
            }
        )
        cmd, _ = _build_cli_cmd(req)
        self.assertIn("--no-apply-ai-fixes", cmd)
        self.assertIn("--no-backup-original-sources", cmd)

    def test_force_and_llm_flags(self) -> None:
        req = run_request_from_dict(
            {
                "crash_log": "/tmp/a.crash",
                "force_disassembly": True,
                "force_anr_analysis": True,
                "force_memory_analysis": True,
                "force_timeline_analysis": True,
                "llm_mode": "auto",
                "llm_profile": "strong",
                "include_memory_in_05": True,
                "native_leak_dir": "/tmp/leak",
                "engine": "langgraph",
                "scope": "parse_stack_only",
                "prompt_mode": "fix",
                "agent_loop": "single",
                "streaming": True,
            }
        )
        cmd, _ = _build_cli_cmd(req)
        for flag in (
            "--force-disassembly",
            "--force-anr-analysis",
            "--force-memory-analysis",
            "--force-timeline-analysis",
            "--include-memory-in-05",
            "--streaming",
        ):
            self.assertIn(flag, cmd)
        self.assertIn("--llm-mode", cmd)
        self.assertIn("auto", cmd)
        self.assertIn("--llm-profile", cmd)
        self.assertIn("strong", cmd)
        self.assertIn("--native-leak-dir", cmd)
        self.assertIn("--engine", cmd)
        self.assertIn("langgraph", cmd)
        self.assertIn("--scope", cmd)
        self.assertIn("parse_stack_only", cmd)
        self.assertNotIn("--prompt-mode", cmd)
        self.assertIn("--agent-loop", cmd)
        self.assertIn("single", cmd)

    def test_legacy_sequential_engine_and_unknown_keys(self) -> None:
        req = run_request_from_dict(
            {
                "crash_log": "/tmp/a.crash",
                "engine": "sequential",
                "unknown_web_field": "x",
                "skip_ai": True,
            }
        )
        self.assertEqual(req.engine, "direct")
        self.assertEqual(req.scope, "gen_prompt_only")
        cmd, _ = _build_cli_cmd(req)
        # default direct → omit --engine
        self.assertNotIn("--engine", cmd)
        self.assertIn("--scope", cmd)
        self.assertIn("gen_prompt_only", cmd)

    def test_web_daemon_non_interactive_vector_db_flags(self) -> None:
        req = run_request_from_dict({"crash_log": "/tmp/a.crash", "scope": "full"})
        cmd, _ = _build_cli_cmd(req)
        self.assertIn("--no-interactive", cmd)
        self.assertIn("--no-save-to-vector-db", cmd)

    def test_content_uses_stdin(self) -> None:
        req = run_request_from_dict({"crash_log_content": "SIGSEGV\n"})
        cmd, stdin = _build_cli_cmd(req)
        self.assertEqual(stdin, "SIGSEGV\n")
        self.assertIn("--crash-log", cmd)
        self.assertIn("-", cmd)


if __name__ == "__main__":
    unittest.main()
