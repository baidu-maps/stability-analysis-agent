from __future__ import annotations

import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch

from services.diff_review import review_changed_files
from services.git_worktree_manager import WorktreeIsolationError, cleanup_isolated_workspace
from services.repair_pipeline import run_repair_pipeline, unisolated_workspace_fingerprint
from services.verification import make_approval


class RepairRuntimeContractTests(unittest.TestCase):
    @staticmethod
    def _no_fix(**kwargs):
        del kwargs
        return {"success": False, "applied": []}

    def _run(self, root: str, report_dir: Path, config):
        return run_repair_pipeline(
            result={"status": "success", "analysis": "done", "code_context": {}},
            code_roots=[root],
            report_dir=report_dir,
            run_id="run-1",
            verification_config=config,
            llm_adapter=object(),
            apply_fix_fn=self._no_fix,
            post_fix_diagnosis=False,
        )

    def test_disabling_isolation_without_bound_approval_still_isolates(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("services.repair_pipeline.prepare_isolated_workspace",
                       side_effect=WorktreeIsolationError("forced isolation")) as prepare:
                result = self._run(tmp, Path(tmp) / "reports", {
                    "isolate_worktree": False,
                    "allow_unisolated": True,
                })
            prepare.assert_called_once()
            self.assertEqual(result.verification_result["provider"], "worktree")

    def test_unisolated_approval_must_match_scope_and_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            fingerprint = unisolated_workspace_fingerprint("run-1", [tmp])
            approval = make_approval(
                run_id="run-1", tool_call_id="unisolated_workspace",
                command_fingerprint=fingerprint, scope="single_command",
            )
            approval.update(status="granted", granted_by="user")
            with patch("services.repair_pipeline.prepare_isolated_workspace",
                       side_effect=WorktreeIsolationError("forced isolation")) as prepare:
                self._run(tmp, Path(tmp) / "reports", {
                    "isolate_worktree": False,
                    "allow_unisolated": True,
                    "unisolated_approval": approval,
                })
            prepare.assert_called_once()

    def test_valid_high_risk_approval_is_consumed_before_unisolated_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            fingerprint = unisolated_workspace_fingerprint("run-1", [tmp])
            approval = make_approval(
                run_id="run-1", tool_call_id="unisolated_workspace",
                command_fingerprint=fingerprint, scope="unisolated_workspace",
            )
            approval.update(status="granted", granted_by="user")
            with patch("services.repair_pipeline.prepare_isolated_workspace") as prepare:
                result = self._run(tmp, Path(tmp) / "reports", {
                    "isolate_worktree": False,
                    "allow_unisolated": True,
                    "unisolated_approval": approval,
                })
            prepare.assert_not_called()
            self.assertEqual(result.result_updates["unisolated_approval"]["status"], "consumed")

    def test_diff_review_rejects_dangerous_dependency_api_and_guard_removal(self):
        path = "/tmp/api.h"
        old = "public:\n  void lock();\n  void checkError();\n"
        new = "#include <cstdlib>\npublic:\n  void changed();\n  system(\"x\");\n"
        review = review_changed_files(
            [path], [path],
            changed_contents={path: new}, original_contents={path: old},
            diff_text="-  void lock();\n-  void checkError();\n+  void changed();\n+  system(\"x\");",
        )
        joined = "\n".join(review.issues)
        self.assertEqual(review.status, "failed")
        self.assertIn("dangerous API", joined)
        self.assertIn("new dependency", joined)
        self.assertIn("lock/error-check removal", joined)
        self.assertIn("public API/ABI", joined)

    def test_diff_review_enforces_file_diff_and_function_limits(self):
        review = review_changed_files(
            ["/tmp/a.cpp", "/tmp/b.cpp"],
            ["/tmp/a.cpp", "/tmp/b.cpp"],
            max_files=1,
            max_diff_lines=1,
            diff_text="+one\n+two\n",
            changed_functions=["Other::f"],
            allowed_functions=["Target::f"],
        )
        joined = "\n".join(review.issues)
        self.assertIn("changed file count exceeds", joined)
        self.assertIn("diff line count exceeds", joined)
        self.assertIn("unrelated", joined)

    def test_real_worktree_fix_stays_isolated_until_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src.cpp"
            source.write_text("int target() { return 0; }\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
            subprocess.run(["git", "add", "src.cpp"], cwd=str(root), check=True)
            subprocess.run(["git", "-c", "user.email=test@example.com", "-c", "user.name=test",
                            "commit", "-qm", "initial"], cwd=str(root), check=True)

            def apply_fix(**kwargs):
                isolated_root = Path(kwargs["code_roots"][0])
                isolated_file = isolated_root / "src.cpp"
                isolated_file.write_text("int target() { return 1; }\n", encoding="utf-8")
                return {"success": True, "applied": [{"file": str(isolated_file)}]}

            report_dir = root / "report"
            result = run_repair_pipeline(
                result={
                    "status": "success",
                    "analysis": "done",
                    "code_context": {"graph": {"nodes": [{
                        "file": str(source), "signature": "target()", "snippet": ["int target()"]
                    }]}},
                },
                code_roots=[str(root)], report_dir=report_dir, run_id="real-worktree",
                verification_config=None, llm_adapter=object(), apply_fix_fn=apply_fix,
                post_fix_diagnosis=False,
            )
            try:
                self.assertEqual(result.verification_result["status"], "pending", result.verification_result)
                self.assertEqual(result.result_updates["completion_reason"], "verification_pending")
                self.assertEqual(source.read_text(encoding="utf-8"), "int target() { return 0; }\n")
                self.assertIsNotNone(result.isolated_workspace)
                isolated_file = Path(result.isolated_workspace.isolated_code_roots[0]) / "src.cpp"
                self.assertEqual(isolated_file.read_text(encoding="utf-8"), "int target() { return 1; }\n")
                self.assertTrue((report_dir / "09_ai_fix_workspace.json").is_file())
            finally:
                if result.isolated_workspace is not None:
                    cleanup_isolated_workspace(result.isolated_workspace, force=True)


if __name__ == "__main__":
    unittest.main()
