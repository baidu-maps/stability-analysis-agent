import json
import queue
import sys
import tempfile
import unittest
import types
from unittest import mock
from pathlib import Path

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root.parent))
pkg = types.ModuleType("stability_analyzer_agent")
pkg.__path__ = [str(project_root)]
sys.modules.setdefault("stability_analyzer_agent", pkg)

from cli.main import (
    _build_run_request_record,
    _build_replay_argv_from_record,
    _interactive_state_to_argv,
    _read_crash_input_source_from_interactive_prompt,
    _write_cli_report,
)
from daemon.server import RunState, _emit_stdout_line


class TestCliReplayAndStreaming(unittest.TestCase):
    def test_write_cli_report_persists_run_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = Path(tmp)
            result = {
                "status": "success",
                "parse_result": {"ok": True},
                "analysis": "done",
            }
            request_record = {
                "crash_log": "/tmp/demo.crash",
                "crash_log_source": "file",
                "scope": "full",
                "engine": "direct",
                "prompt_mode": "analysis",
                "output_format": "markdown",
            }
            written = _write_cli_report(
                report_dir,
                result,
                "output",
                scope="full",
                request_record=request_record,
            )
            self.assertEqual(written, report_dir)
            request_file = report_dir / "00_run_request.json"
            summary_file = report_dir / "00_run_summary.json"
            self.assertTrue(request_file.exists())
            self.assertTrue(summary_file.exists())
            payload = json.loads(request_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["crash_log"], "/tmp/demo.crash")
            self.assertEqual(payload["scope"], "full")
            summary = json.loads(summary_file.read_text(encoding="utf-8"))
            self.assertEqual(summary["schema_version"], 2)
            self.assertEqual(summary["status"], "success")
            self.assertNotIn("crash_log", summary)
            self.assertIn("01_crash_log_parser.json", summary["artifacts"])
            self.assertEqual(
                summary["artifacts"]["02_add2line_resolver.json"]["status"],
                "not_written",
            )

    @mock.patch("cli.main._git_runtime_snapshot", return_value={"commit": "abc", "dirty": False})
    @mock.patch("cli.main._runtime_version", return_value="1.2.8")
    @mock.patch("cli.main._llm_request_snapshot")
    def test_build_run_request_records_effective_defaults(
        self,
        mock_llm_snapshot,
        _mock_version,
        _mock_git,
    ) -> None:
        mock_llm_snapshot.return_value = {
            "enabled": True,
            "provider": "zhipu_bigmodel",
            "model": "glm-4",
            "streaming": True,
        }
        with tempfile.NamedTemporaryFile("w", suffix=".crash", delete=False) as tmp:
            tmp.write("demo crash")
            crash_path = tmp.name
        try:
            args = mock.Mock(
                engine="direct",
                prompt_mode="analysis",
                agent_loop=None,
                max_agent_rounds=0,
                max_context_requests_per_round=5,
                library_dir="/tmp/lib",
                config=None,
                output_format="markdown",
                apply_ai_fixes=False,
                backup_original_sources=True,
                optimized=False,
                streaming=None,
                vector_db_path="./vector_db",
                vector_db_max_results=3,
                vector_db_record_usage=False,
                rule_confidence_threshold=0.85,
                max_sibling_member_functions=0,
                max_shared_var_related_functions=20,
                min_key_read_related_functions=2,
                use_ctags_index=False,
                include_memory_in_05=False,
                code_context_timeout_sec=360,
                find_source_timeout_sec=600,
                plugin_modules=[],
                output_file=None,
                print_full_report=False,
            )
            record = _build_run_request_record(
                args,
                crash_log_content="demo crash",
                scope="full",
                code_roots=["/tmp/code"],
                crash_log_source="file",
                crash_log_value=crash_path,
            )
            self.assertEqual(record["schema_version"], 2)
            self.assertEqual(record["crash_input"]["source"], "file")
            self.assertEqual(record["effective_parameters"]["agent_loop"], "context_loop")
            self.assertEqual(record["effective_parameters"]["max_agent_rounds"], 3)
            self.assertTrue(record["effective_parameters"]["streaming"])
            self.assertNotIn("api_key", json.dumps(record))
        finally:
            Path(crash_path).unlink(missing_ok=True)

    def test_build_replay_argv_from_v2_record(self) -> None:
        record = {
            "schema_version": 2,
            "crash_input": {
                "source": "file",
                "resolved_path": "/tmp/demo.crash",
            },
            "paths": {
                "library_dir": "/tmp/lib",
                "code_roots": ["/tmp/code"],
                "vector_db_path": "/tmp/vector_db",
                "config": None,
            },
            "effective_parameters": {
                "scope": "full",
                "engine": "direct",
                "output_format": "markdown",
                "prompt_mode": "analysis",
                "agent_loop": "context_loop",
                "max_agent_rounds": 3,
                "max_context_requests_per_round": 5,
                "apply_ai_fixes": False,
                "backup_original_sources": True,
                "streaming": True,
                "vector_db_max_results": 3,
                "rule_confidence_threshold": 0.85,
                "plugin_modules": [],
            },
        }
        argv, cleanup_paths = _build_replay_argv_from_record(record)
        self.assertIn("--crash-log-file", argv)
        self.assertIn("/tmp/demo.crash", argv)
        self.assertIn("--agent-loop", argv)
        self.assertIn("context_loop", argv)
        self.assertIn("--max-agent-rounds", argv)
        self.assertIn("--no-apply-ai-fixes", argv)
        self.assertIn("--streaming", argv)
        self.assertFalse(cleanup_paths)

    def test_write_cli_report_summarizes_execution_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = Path(tmp)
            request_record = {
                "schema_version": 2,
                "run_id": "run-1",
                "created_at": "2026-08-04T12:00:00+08:00",
                "effective_parameters": {"apply_ai_fixes": False},
            }
            result = {
                "status": "success",
                "workflow": "crash_analysis",
                "parse_result": {"ok": True},
                "analysis": "done",
                "metadata": {
                    "execution_events": [
                        {
                            "kind": "tool",
                            "name": "crash_log_parser",
                            "status": "success",
                            "duration_ms": 12,
                        },
                        {
                            "kind": "llm",
                            "name": "llm_analysis",
                            "status": "success",
                            "duration_ms": 25,
                        },
                    ]
                },
            }
            _write_cli_report(
                report_dir,
                result,
                "output",
                scope="full",
                request_record=request_record,
                run_duration_ms=50,
            )
            summary = json.loads(
                (report_dir / "00_run_summary.json").read_text(encoding="utf-8")
            )
            stage_by_name = {stage["name"]: stage for stage in summary["stages"]}
            self.assertEqual(summary["run_id"], "run-1")
            self.assertEqual(summary["duration_ms"], 50)
            self.assertEqual(stage_by_name["crash_log_parser"]["duration_ms"], 12)
            self.assertEqual(stage_by_name["llm_analysis"]["duration_ms"], 25)
            self.assertEqual(stage_by_name["apply_ai_fixes"]["status"], "disabled")

    def test_build_replay_argv_from_stdin_record_uses_temp_file(self) -> None:
        record = {
            "crash_log_source": "stdin",
            "crash_log_content": "crash log content",
            "scope": "gen_prompt_only",
            "engine": "direct",
            "output_format": "markdown",
        }
        argv, cleanup_paths = _build_replay_argv_from_record(record)
        self.assertIn("--crash-log-file", argv)
        idx = argv.index("--crash-log-file")
        temp_path = Path(argv[idx + 1])
        self.assertTrue(temp_path.exists())
        self.assertEqual(temp_path.read_text(encoding="utf-8"), "crash log content")
        for item in cleanup_paths:
            Path(item).unlink(missing_ok=True)

    def test_build_replay_argv_from_content_record_uses_temp_file(self) -> None:
        record = {
            "crash_log_source": "content",
            "crash_log_content": "crash log content",
            "scope": "full",
            "engine": "direct",
            "output_format": "markdown",
        }
        argv, cleanup_paths = _build_replay_argv_from_record(record)
        self.assertIn("--crash-log-file", argv)
        idx = argv.index("--crash-log-file")
        temp_path = Path(argv[idx + 1])
        self.assertTrue(temp_path.exists())
        self.assertEqual(temp_path.read_text(encoding="utf-8"), "crash log content")
        for item in cleanup_paths:
            Path(item).unlink(missing_ok=True)

    def test_build_replay_argv_from_dir_record_uses_dir_flag(self) -> None:
        record = {
            "crash_log_source": "dir",
            "crash_log_dir": "/tmp/logs",
            "scope": "full",
            "engine": "direct",
            "output_format": "markdown",
        }
        argv, cleanup_paths = _build_replay_argv_from_record(record)
        self.assertIn("--crash-log-dir", argv)
        idx = argv.index("--crash-log-dir")
        self.assertEqual(argv[idx + 1], "/tmp/logs")
        self.assertFalse(cleanup_paths)

    def test_interactive_state_to_argv_for_file(self) -> None:
        argv = _interactive_state_to_argv(
            {
                "crash_log_source": "file",
                "crash_log_file": "/tmp/demo.crash",
                "engine": "direct",
                "scope": "parse_log_only",
                "code_roots": [],
            }
        )
        self.assertIn("--crash-log-file", argv)
        self.assertIn("/tmp/demo.crash", argv)
        self.assertIn("--scope", argv)
        self.assertIn("parse_log_only", argv)

    def test_interactive_state_to_argv_for_content(self) -> None:
        argv = _interactive_state_to_argv(
            {
                "crash_log_source": "content",
                "crash_log_content": "line1\nline2",
                "engine": "direct",
                "scope": "full",
                "code_roots": [],
            }
        )
        self.assertIn("--crash-log-content", argv)
        idx = argv.index("--crash-log-content")
        self.assertEqual(argv[idx + 1], "line1\nline2")

    @mock.patch("cli.main._safe_input_back")
    @mock.patch("cli.main._prompt_select")
    def test_read_crash_input_source_file_choice(self, mock_select, mock_input) -> None:
        mock_select.return_value = "file"
        with tempfile.NamedTemporaryFile("w", suffix=".crash", delete=False) as tmp:
            tmp.write("demo crash")
            tmp_path = tmp.name
        try:
            mock_input.return_value = tmp_path
            result = _read_crash_input_source_from_interactive_prompt()
            self.assertEqual(result["crash_log_source"], "file")
            self.assertEqual(result["crash_log_file"], str(Path(tmp_path).resolve()))
            self.assertIsNone(result["crash_log_content"])
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    @mock.patch("cli.main._is_tty_interactive")
    @mock.patch("cli.main._prompt_select")
    def test_read_crash_input_source_content_choice(self, mock_select, mock_is_tty) -> None:
        mock_select.return_value = "content"
        mock_is_tty.return_value = False
        with mock.patch("cli.main.sys.stdin.read", return_value="line1\nline2\n"):
            result = _read_crash_input_source_from_interactive_prompt()
        self.assertEqual(result["crash_log_source"], "content")
        self.assertIsNone(result["crash_log_file"])
        self.assertEqual(result["crash_log_content"], "line1\nline2")

    def test_emit_stdout_line_parses_ai_stream(self) -> None:
        run = RunState(run_id="run-1", status="running", created_at=0.0)
        run.events = queue.Queue()
        stdout_acc = []
        _emit_stdout_line(
            run,
            stdout_acc,
            'AI_STREAM_DATA:{"type":"chunk","content":"hi"}\n',
        )
        self.assertTrue(stdout_acc)
        event = run.events.get_nowait()
        self.assertEqual(event.type, "ai_stream")
        self.assertEqual(event.data["payload"]["type"], "chunk")
        self.assertEqual(event.data["payload"]["content"], "hi")


if __name__ == "__main__":
    unittest.main()
