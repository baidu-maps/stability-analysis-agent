from __future__ import annotations

import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch

from services.git_worktree_manager import WorktreeIsolationError, cleanup_isolated_workspace
from services.repair_pipeline import run_repair_pipeline, unisolated_workspace_fingerprint
from services.verification import make_approval
from services.simple_verification import IssueVerificationScript, VerificationResult, VerificationStatus


class PhaseReproduction(IssueVerificationScript):
    def __init__(self):
        self.phases = []

    def run(self, context):
        phase = context.metadata.get("verification_phase")
        self.phases.append(phase)
        return VerificationResult(
            VerificationStatus.PASSED, phase, reproduced=phase == "before_fix",
        )


class MustNotRunReproduction(IssueVerificationScript):
    def run(self, context):
        raise AssertionError("runtime verification must remain disabled")


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

    def test_real_worktree_fix_syncs_after_safety_verification_without_scripts(self):
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
            with patch(
                "services.repair_actions.run_post_fix_diagnosis",
                side_effect=AssertionError("old log diagnosis must not run"),
            ):
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
                    post_fix_diagnosis=True,
                )
            try:
                self.assertEqual(result.verification_result["status"], "repair_applied", result.verification_result)
                self.assertEqual(result.result_updates["completion_reason"], "repair_applied")
                self.assertEqual(result.verification_result["safety_verification"]["status"], "passed")
                self.assertEqual(source.read_text(encoding="utf-8"), "int target() { return 1; }\n")
                self.assertIsNotNone(result.isolated_workspace)
                isolated_file = Path(result.isolated_workspace.isolated_code_roots[0]) / "src.cpp"
                self.assertEqual(isolated_file.read_text(encoding="utf-8"), "int target() { return 1; }\n")
                self.assertTrue((report_dir / "09_ai_fix_workspace.json").is_file())
            finally:
                if result.isolated_workspace is not None:
                    cleanup_isolated_workspace(result.isolated_workspace, force=True)

    def test_configured_runtime_script_is_not_loaded_or_run_without_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src.cpp"
            source.write_text("int target() { return 0; }\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
            subprocess.run(["git", "add", "src.cpp"], cwd=str(root), check=True)
            subprocess.run(["git", "-c", "user.email=test@example.com", "-c", "user.name=test",
                            "commit", "-qm", "initial"], cwd=str(root), check=True)

            def apply_fix(**kwargs):
                isolated_file = Path(kwargs["code_roots"][0]) / "src.cpp"
                isolated_file.write_text("int target() { return 1; }\n", encoding="utf-8")
                return {"success": True, "applied": [{"file": str(isolated_file)}]}

            result = run_repair_pipeline(
                result={"status": "success", "analysis": "done", "code_context": {}},
                code_roots=[str(root)], report_dir=root / "report", run_id="runtime-disabled",
                verification_config={
                    "verification": {
                        "effectiveness": {"issue": MustNotRunReproduction()},
                    }
                },
                llm_adapter=object(), apply_fix_fn=apply_fix, post_fix_diagnosis=False,
            )
            try:
                self.assertIsNone(result.baseline_result)
                self.assertEqual(result.verification_result["status"], "repair_applied")
                self.assertEqual(
                    result.verification_result["effectiveness_verification"]["status"], "skipped"
                )
            finally:
                if result.isolated_workspace is not None:
                    cleanup_isolated_workspace(result.isolated_workspace, force=True)

    def test_skip_verify_skips_runtime_script_but_syncs_safe_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src.cpp"
            source.write_text("int target() { return 0; }\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
            subprocess.run(["git", "add", "src.cpp"], cwd=str(root), check=True)
            subprocess.run(["git", "-c", "user.email=test@example.com", "-c", "user.name=test",
                            "commit", "-qm", "initial"], cwd=str(root), check=True)

            def apply_fix(**kwargs):
                isolated_file = Path(kwargs["code_roots"][0]) / "src.cpp"
                isolated_file.write_text("int target() { return 1; }\n", encoding="utf-8")
                return {"success": True, "applied": [{"file": str(isolated_file)}]}

            result = run_repair_pipeline(
                result={"status": "success", "analysis": "done", "code_context": {}},
                code_roots=[str(root)], report_dir=root / "report", run_id="runtime-skipped",
                verification_config={
                    "enable_runtime_verification": True,
                    "skip_runtime_verification": True,
                    "verification": {
                        "effectiveness": {"issue": MustNotRunReproduction()},
                    },
                },
                llm_adapter=object(), apply_fix_fn=apply_fix, post_fix_diagnosis=False,
            )
            try:
                self.assertIsNone(result.baseline_result)
                self.assertEqual(result.verification_result["status"], "repair_applied")
                self.assertEqual(source.read_text(encoding="utf-8"), "int target() { return 1; }\n")
                self.assertEqual(
                    result.verification_result["worktree_sync"]["integrity"]["status"], "passed"
                )
            finally:
                if result.isolated_workspace is not None:
                    cleanup_isolated_workspace(result.isolated_workspace, force=True)

    def test_undeclared_worktree_change_fails_safety_and_is_not_synced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.cpp"
            second = root / "second.cpp"
            first.write_text("int first() { return 0; }\n", encoding="utf-8")
            second.write_text("int second() { return 0; }\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
            subprocess.run(["git", "add", "first.cpp", "second.cpp"], cwd=str(root), check=True)
            subprocess.run(["git", "-c", "user.email=test@example.com", "-c", "user.name=test",
                            "commit", "-qm", "initial"], cwd=str(root), check=True)

            def apply_fix(**kwargs):
                isolated_root = Path(kwargs["code_roots"][0])
                isolated_first = isolated_root / "first.cpp"
                isolated_first.write_text("int first() { return 1; }\n", encoding="utf-8")
                (isolated_root / "second.cpp").write_text(
                    "int second() { return 1; }\n", encoding="utf-8"
                )
                return {"success": True, "applied": [{"file": str(isolated_first)}]}

            result = run_repair_pipeline(
                result={"status": "success", "analysis": "done", "code_context": {}},
                code_roots=[str(root)], report_dir=root / "report", run_id="unsafe-manifest",
                verification_config=None, llm_adapter=object(), apply_fix_fn=apply_fix,
            )
            try:
                self.assertEqual(result.verification_result["status"], "safety_check_failed")
                self.assertEqual(first.read_text(encoding="utf-8"), "int first() { return 0; }\n")
                self.assertEqual(second.read_text(encoding="utf-8"), "int second() { return 0; }\n")
            finally:
                if result.isolated_workspace is not None:
                    cleanup_isolated_workspace(result.isolated_workspace, force=True)

    def test_issue_reproduction_runs_before_and_after_patch_inside_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src.cpp"
            source.write_text("int target() { return 0; }\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
            subprocess.run(["git", "add", "src.cpp"], cwd=str(root), check=True)
            subprocess.run(["git", "-c", "user.email=test@example.com", "-c", "user.name=test",
                            "commit", "-qm", "initial"], cwd=str(root), check=True)

            def apply_fix(**kwargs):
                isolated_file = Path(kwargs["code_roots"][0]) / "src.cpp"
                isolated_file.write_text("int target() { return 1; }\n", encoding="utf-8")
                return {"success": True, "applied": [{"file": str(isolated_file)}]}

            script = PhaseReproduction()
            result = run_repair_pipeline(
                result={"status": "success", "analysis": "done", "code_context": {}},
                code_roots=[str(root)], report_dir=root / "report", run_id=f"simple-{root.name}",
                verification_config={
                    "enable_runtime_verification": True,
                    "verification": {
                        "safety": {},
                        "effectiveness": {"issue": script},
                    },
                },
                llm_adapter=object(), apply_fix_fn=apply_fix, post_fix_diagnosis=False,
            )
            try:
                self.assertIsNotNone(result.baseline_result, result.result_updates)
                self.assertEqual(result.baseline_result["summary"], "before_fix")
                self.assertEqual(
                    result.verification_result["effectiveness_verification"]["issue_verification"]["summary"],
                    "after_fix",
                )
                self.assertEqual(result.result_updates["completion_reason"], "issue_verified")
                self.assertEqual(source.read_text(encoding="utf-8"), "int target() { return 1; }\n")
            finally:
                if result.isolated_workspace is not None:
                    cleanup_isolated_workspace(result.isolated_workspace, force=True)


if __name__ == "__main__":
    unittest.main()
