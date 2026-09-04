import json
import tempfile
import unittest
from pathlib import Path

from cli.main import _write_cli_report
from services.external_agent_evaluation import build_external_agent_comparison, build_external_agent_evaluation


class ExternalAgentEvaluationTests(unittest.TestCase):
    def test_cli_report_generation_is_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp)
            _write_cli_report(
                report,
                {"resolved_stack": {"frames": []}, "analysis": "done"},
                "done",
                scope="parse_stack_only",
                request_record={"effective_parameters": {}},
            )
            self.assertFalse((report / "external_agent_evaluation").exists())

    def test_cli_report_generation_can_be_enabled_explicitly(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp)
            _write_cli_report(
                report,
                {"resolved_stack": {"frames": [{"function": "crash"}]}, "analysis": "done"},
                "done",
                scope="parse_stack_only",
                request_record={
                    "effective_parameters": {"external_agent_evaluation": True}
                },
            )
            self.assertTrue(
                (report / "external_agent_evaluation" / "benchmark_task.md").is_file()
            )

    def test_writes_namespaced_task_manifest_and_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp)
            (report / "01_crash_log_parser.json").write_text('{"signal":"SIGSEGV"}', encoding="utf-8")
            (report / "03_add2line_resolver.json").write_text('{"frames":[{"function":"crash"}]}', encoding="utf-8")
            (report / "04a_crash_diagnosis.json").write_text('{"root_cause":"secret conclusion"}', encoding="utf-8")
            request = {
                "run_id": "run-1",
                "paths": {"code_roots": [tmp]},
                "effective_parameters": {"verification": {"checks": [{
                    "id": "replay", "kind": "replay", "provider": "test_runner",
                    "description": "fixed fixture", "command": ["secret-runner"],
                    "verification_level": "L3", "allowed_changed_files": ["src/**"],
                }]}},
            }
            out = build_external_agent_evaluation(report, request=request, result={})
            evaluation = report / "external_agent_evaluation"
            self.assertEqual(Path(out["directory"]), evaluation.resolve())
            self.assertTrue((evaluation / "benchmark_task.md").is_file())
            self.assertTrue((evaluation / "submission_schema.json").is_file())
            self.assertTrue((evaluation / "submissions").is_dir())
            manifest = json.loads((evaluation / "input_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["verification_capabilities"][0]["check_id"], "replay")
            self.assertNotIn("command", manifest["verification_capabilities"][0])
            self.assertEqual(manifest["authorized_changed_files"], ["src/**"])
            task = (evaluation / "benchmark_task.md").read_text(encoding="utf-8")
            self.assertIn("03_add2line_resolver.json", task)
            self.assertIn("../04a_crash_diagnosis.json", manifest["excluded_agent_artifacts"])
            self.assertNotIn("secret conclusion", task)
            self.assertNotIn("secret-runner", task)

    def test_does_not_generate_without_resolved_stack(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(build_external_agent_evaluation(Path(tmp), request={}, result={}))
            self.assertFalse((Path(tmp) / "external_agent_evaluation").exists())

    def test_builds_descriptive_report_from_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp)
            submission = report / "external_agent_evaluation" / "submissions" / "codex"
            submission.mkdir(parents=True)
            payload = {
                "schema_version": 1, "tool": "codex", "investigation_journal": [{"step": 1}],
                "root_cause": {"category": "uaf", "statement": "x", "location": {}},
                "supporting_evidence": [{"source": "a.cpp:1"}], "changed_files": ["a.cpp"],
                "confidence": 0.8, "limitations": [],
            }
            (submission / "result.json").write_text(json.dumps(payload), encoding="utf-8")
            result = build_external_agent_comparison(report)
            self.assertTrue(result["external_submissions"][0]["valid"])
            self.assertTrue((report / "external_agent_evaluation" / "comparison_report.md").is_file())


if __name__ == "__main__":
    unittest.main()
