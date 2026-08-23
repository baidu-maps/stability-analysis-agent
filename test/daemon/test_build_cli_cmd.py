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

from cli.main import build_parser
from protocol.models import RunRequest, run_request_from_dict
from daemon.server import _build_cli_cmd, _cli_analysis_defaults


def _argv_from_cmd(cmd: list) -> list:
    """Strip `python3 -u cli/main.py` so leftover tokens are CLI flags."""
    try:
        idx = next(i for i, part in enumerate(cmd) if str(part).endswith("cli/main.py"))
    except StopIteration:
        return cmd
    return cmd[idx + 1 :]


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
        self.assertIn("--crash-log-file", cmd)
        self.assertIn("-", cmd)
        self.assertNotIn("--crash-log", cmd)

    def test_omitted_agent_flags_match_cli_argparse_defaults(self) -> None:
        """HTTP 省略字段时，除 daemon 固定的非交互/禁写库外，不应覆盖 CLI 默认值。"""
        defaults = _cli_analysis_defaults()
        req = run_request_from_dict({"crash_log": "/tmp/a.crash"})
        cmd, _ = _build_cli_cmd(req)
        argv = _argv_from_cmd(cmd)

        omitted_when_default = [
            "--engine",
            "--scope",
            "--prompt-mode",
            "--max-agent-rounds",
            "--max-context-requests-per-round",
            "--streaming",
            "--no-streaming",
            "--output-format",
            "--vector-db-path",
            "--vector-db-max-results",
            "--vector-db-record-usage",
            "--rule-confidence-threshold",
            "--use-ctags-index",
            "--plugin-module",
            "--max-sibling-member-functions",
            "--max-stack-frames-symbol-enrich",
            "--max-stack-frames-in-prompt",
            "--max-shared-var-related-functions",
            "--min-key-read-related-functions",
            "--code-context-timeout-sec",
            "--find-source-timeout-sec",
            "--consultation",
        ]
        for flag in omitted_when_default:
            self.assertNotIn(flag, argv, msg=f"default request should omit {flag}")

        parsed = build_parser().parse_args(argv)
        self.assertEqual(parsed.engine, defaults.engine)
        self.assertEqual(parsed.scope, defaults.scope)
        self.assertEqual(parsed.prompt_mode, defaults.prompt_mode)
        self.assertEqual(parsed.max_agent_rounds, defaults.max_agent_rounds)
        self.assertEqual(parsed.max_context_requests_per_round, defaults.max_context_requests_per_round)
        self.assertEqual(parsed.streaming, defaults.streaming)
        self.assertEqual(parsed.vector_db_path, defaults.vector_db_path)
        self.assertEqual(parsed.vector_db_max_results, defaults.vector_db_max_results)
        self.assertEqual(parsed.include_memory_in_05, defaults.include_memory_in_05)
        self.assertEqual(parsed.max_sibling_member_functions, defaults.max_sibling_member_functions)
        self.assertEqual(defaults.max_sibling_member_functions, 0)
        self.assertEqual(parsed.max_stack_frames_symbol_enrich, defaults.max_stack_frames_symbol_enrich)
        self.assertEqual(defaults.max_stack_frames_symbol_enrich, 8)
        self.assertEqual(parsed.max_stack_frames_in_prompt, defaults.max_stack_frames_in_prompt)
        self.assertEqual(defaults.max_stack_frames_in_prompt, 4)
        self.assertTrue(parsed.interactive is False)
        self.assertTrue(parsed.no_save_to_vector_db)

    def test_explicit_max_agent_rounds_one_is_forwarded(self) -> None:
        req = run_request_from_dict({"crash_log": "/tmp/a.crash", "max_agent_rounds": 1})
        cmd, _ = _build_cli_cmd(req)
        argv = _argv_from_cmd(cmd)
        self.assertIn("--max-agent-rounds", argv)
        self.assertEqual(argv[argv.index("--max-agent-rounds") + 1], "1")
        parsed = build_parser().parse_args(argv)
        self.assertEqual(parsed.max_agent_rounds, 1)

    def test_streaming_false_forwards_no_streaming(self) -> None:
        req = run_request_from_dict({"crash_log": "/tmp/a.crash", "streaming": False})
        cmd, _ = _build_cli_cmd(req)
        argv = _argv_from_cmd(cmd)
        self.assertIn("--no-streaming", argv)
        parsed = build_parser().parse_args(argv)
        self.assertIs(parsed.streaming, False)

    def test_consultation_is_ignored(self) -> None:
        req = run_request_from_dict(
            {
                "crash_log": "/tmp/a.crash",
                "consultation": True,
                "prompt": "hello",
            }
        )
        cmd, _ = _build_cli_cmd(req)
        self.assertNotIn("--consultation", cmd)
        self.assertNotIn("--prompt", cmd)
        build_parser().parse_args(_argv_from_cmd(cmd))

    def test_timeouts_plugins_and_vector_flags_are_forwarded(self) -> None:
        req = run_request_from_dict(
            {
                "crash_log": "/tmp/a.crash",
                "vector_db_path": "/tmp/vdb",
                "vector_db_max_results": 7,
                "vector_db_record_usage": True,
                "rule_confidence_threshold": 0.5,
                "use_ctags_index": True,
                "plugin_modules": ["pkg.ext_a", "pkg.ext_b"],
                "max_sibling_member_functions": 12,
                "max_stack_frames_symbol_enrich": 10,
                "max_stack_frames_in_prompt": 6,
                "max_shared_var_related_functions": 8,
                "min_key_read_related_functions": 1,
                "code_context_timeout_sec": 600,
                "find_source_timeout_sec": 90,
            }
        )
        cmd, _ = _build_cli_cmd(req)
        argv = _argv_from_cmd(cmd)
        parsed = build_parser().parse_args(argv)
        self.assertEqual(parsed.vector_db_path, "/tmp/vdb")
        self.assertEqual(parsed.vector_db_max_results, 7)
        self.assertTrue(parsed.vector_db_record_usage)
        self.assertEqual(parsed.rule_confidence_threshold, 0.5)
        self.assertTrue(parsed.use_ctags_index)
        self.assertEqual(parsed.plugin_modules, ["pkg.ext_a", "pkg.ext_b"])
        self.assertIn("--max-sibling-member-functions", argv)
        self.assertEqual(parsed.max_sibling_member_functions, 12)
        self.assertEqual(parsed.max_stack_frames_symbol_enrich, 10)
        self.assertEqual(parsed.max_stack_frames_in_prompt, 6)
        self.assertEqual(parsed.max_shared_var_related_functions, 8)
        self.assertEqual(parsed.min_key_read_related_functions, 1)
        self.assertEqual(parsed.code_context_timeout_sec, 600.0)
        self.assertEqual(parsed.find_source_timeout_sec, 90.0)

    def test_run_request_defaults_do_not_override_cli_sentinels(self) -> None:
        req = RunRequest(crash_log="/tmp/a.crash")
        self.assertIsNone(req.max_agent_rounds)
        self.assertIsNone(req.max_context_requests_per_round)
        self.assertIsNone(req.streaming)


if __name__ == "__main__":
    unittest.main()
