#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the final-source AI regression runner."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Mapping, Sequence

from test.ai_regression.runner import AIRegressionRunner, PROJECT_ROOT, load_case
from services.code_fixer import CodeFixer


CASE_PATH = PROJECT_ROOT / "test" / "ai_regression" / "cases" / "demo_basic_nullptr.json"


def _hash_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if ".crash_agent" in path.parts:
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


class TestAIRegressionRunner(unittest.TestCase):
    def setUp(self) -> None:
        self.case = dataclasses.replace(load_case(CASE_PATH), platforms=())

    @staticmethod
    def _apply_expected(
        command: Sequence[str],
        cwd: Path,
        output_json: Path,
        timeout_seconds: int,
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess:
        del cwd, timeout_seconds, env
        workspace = Path(command[command.index("--code-roots") + 1])
        patch = PROJECT_ROOT / "test" / "ai_regression" / "expected" / "demo_basic_nullptr.patch"
        applied = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", str(patch)],
            cwd=str(workspace),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        output_json.write_text(
            json.dumps({"status": "success", "applied_ai_fixes": {"success": applied.returncode == 0}}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, applied.returncode, applied.stdout, applied.stderr)

    def test_expected_patch_and_case_references_are_valid(self) -> None:
        self.assertTrue(self.case.crash_log.is_file())
        self.assertTrue(self.case.library_dir.is_dir())
        self.assertTrue(self.case.code_root.is_dir())
        self.assertEqual(self.case.allowed_changed_files, ("common/src/my_lib.cpp",))

    def test_daemon_request_reuses_case_and_maps_agent_options(self) -> None:
        workspace = Path("/tmp/regression-code")
        request = AIRegressionRunner(entrypoint="daemon")._daemon_request(self.case, workspace)
        self.assertEqual(request["crash_log"], str(self.case.crash_log))
        self.assertEqual(request["library_dir"], str(self.case.library_dir))
        self.assertEqual(request["code_roots"], [str(workspace)])
        self.assertEqual(request["scope"], "full")
        self.assertEqual(request["prompt_mode"], "analysis")
        self.assertEqual(request["agent_loop"], "context_loop")
        self.assertTrue(request["apply_ai_fixes"])

    def test_daemon_request_rejects_unmapped_cli_only_option(self) -> None:
        case = dataclasses.replace(self.case, agent_args=("--unknown-option",))
        with self.assertRaisesRegex(ValueError, "not supported by daemon"):
            AIRegressionRunner(entrypoint="daemon")._daemon_request(case, Path("/tmp/code"))

    def test_fix_extractor_prefers_repair_block_and_graph_signature(self) -> None:
        analysis = """
原始代码：
```cpp
void crash_nullptr() {
    int* p = nullptr;
    *p = 42;
}
```
修复代码：
```cpp
void crash_nullptr() {
    int* p = nullptr;
    if (p == nullptr) {
        return;
    }
    *p = 42;
}
```
"""
        plan = CodeFixer()._try_extract_fix_plan_from_analysis(
            analysis,
            [{
                "file": str(self.case.code_root / "common" / "src" / "my_lib.cpp"),
                "signature": "void crash_nullptr()",
            }],
            [],
        )
        self.assertIsNotNone(plan)
        edit = plan["edits"][0]
        self.assertEqual(edit["function_signature"], "void crash_nullptr()")
        self.assertIn("if (p == nullptr)", edit["replacement_code"])

    def test_passes_when_final_source_matches_expected_patch(self) -> None:
        original_hash = _hash_tree(self.case.code_root)
        with tempfile.TemporaryDirectory() as tmp:
            result = AIRegressionRunner(agent_executor=self._apply_expected).run(
                self.case,
                Path(tmp) / "result",
            )
        self.assertEqual(result.verdict, "passed")
        self.assertEqual(result.actual_changed_files, ["common/src/my_lib.cpp"])
        self.assertEqual(_hash_tree(self.case.code_root), original_hash)

    def test_fails_when_ai_code_differs(self) -> None:
        def wrong_fix(command, cwd, output_json, timeout_seconds, env):
            completed = self._apply_expected(command, cwd, output_json, timeout_seconds, env)
            workspace = Path(command[command.index("--code-roots") + 1])
            source = workspace / "common" / "src" / "my_lib.cpp"
            source.write_text(
                source.read_text(encoding="utf-8").replace("错误: 尝试解引用空指针", "忽略错误"),
                encoding="utf-8",
            )
            return completed

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "result"
            result = AIRegressionRunner(agent_executor=wrong_fix).run(self.case, output)
            diff = (output / f"{self.case.case_id}.diff").read_text(encoding="utf-8")
        self.assertEqual(result.verdict, "failed")
        self.assertEqual(result.mismatched_files, ["common/src/my_lib.cpp"])
        self.assertIn("忽略错误", diff)

    def test_fails_when_ai_changes_unapproved_file(self) -> None:
        def extra_fix(command, cwd, output_json, timeout_seconds, env):
            completed = self._apply_expected(command, cwd, output_json, timeout_seconds, env)
            workspace = Path(command[command.index("--code-roots") + 1])
            (workspace / "unexpected.cpp").write_text("int unexpected = 1;\n", encoding="utf-8")
            return completed

        with tempfile.TemporaryDirectory() as tmp:
            result = AIRegressionRunner(agent_executor=extra_fix).run(self.case, Path(tmp) / "result")
        self.assertEqual(result.verdict, "failed")
        self.assertEqual(result.unauthorized_changed_files, ["unexpected.cpp"])

    def test_batch_cli_writes_compact_machine_results(self) -> None:
        from test.ai_regression import runner as module
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "batch"
            old = module.AIRegressionRunner

            class FakeRunner(AIRegressionRunner):
                def run(self, case, output_dir, **kwargs):
                    result = super().run(case, output_dir, **kwargs)
                    result.report_dir = "/repo/reports/fake_standard_report"
                    return result

            module.AIRegressionRunner = FakeRunner
            try:
                rc = module.main(["--case", str(CASE_PATH), "--result-dir", str(output), "--entrypoint", "cli"])
            finally:
                module.AIRegressionRunner = old
            self.assertEqual(rc, 1)
            self.assertTrue((output / "batch_summary.json").is_file())
            self.assertTrue((output / "demo_basic_nullptr_1" / "result.json").is_file())
            names = {path.name for path in (output / "demo_basic_nullptr_1").iterdir()}
            self.assertEqual(names, {"result.json", "demo_basic_nullptr.diff"})
            payload = json.loads((output / "batch_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["results"][0]["entrypoint"], "cli")


if __name__ == "__main__":
    unittest.main()
