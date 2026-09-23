from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from services.repair_actions import (
    RepairActionDeps,
    _apply_patch_handler,
    changed_files_from_applied_result,
)
from services.safety_verification import SafetyContext, SafetyVerificationRunner


class ChangedFilesFromAppliedTests(unittest.TestCase):
    def test_helper_keeps_only_applied_status(self) -> None:
        files = changed_files_from_applied_result(
            {
                "success": True,
                "applied": [
                    {"file": "/tmp/a.cpp", "status": "applied"},
                    {"file": "/tmp/b.cpp", "status": "skipped"},
                    {"file": "/tmp/c.cpp", "status": "rolled_back"},
                    {"file": "", "status": "applied"},
                    "bad",
                ],
            }
        )
        self.assertEqual(files, ["/tmp/a.cpp"])

    def test_helper_handles_empty_or_invalid_payload(self) -> None:
        self.assertEqual(changed_files_from_applied_result(None), [])
        self.assertEqual(changed_files_from_applied_result({}), [])
        self.assertEqual(changed_files_from_applied_result({"applied": None}), [])

    def test_apply_patch_declared_list_ignores_skipped_edits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            applied_file = str(Path(tmp) / "demo_engine_control.cpp")
            skipped_file = str(Path(tmp) / "walk_guidance_control.cpp")

            def _apply_fix_fn(**_kwargs):
                return {
                    "success": True,
                    "applied": [
                        {
                            "file": applied_file,
                            "status": "applied",
                            "function_signature": "DemoArray<RenderStep> DemoEngineControl::GetDrawSteps()",
                        },
                        {
                            "file": skipped_file,
                            "status": "skipped",
                            "error": "replacement_code 的类名/函数名与目标签名不一致，已拒绝写入",
                            "function_signature": "DemoArray<RenderStep> DemoGuidanceControl::GetDrawSteps()",
                        },
                    ],
                }

            deps = RepairActionDeps(
                result={},
                code_roots=[tmp],
                report_dir=Path(tmp),
                run_id="run-change-manifest",
                apply_fix_fn=_apply_fix_fn,
            )
            result = _apply_patch_handler(deps, {"code_roots": [tmp], "report_dir": tmp})

        self.assertTrue(result["success"])
        self.assertEqual(deps.changed_files, [applied_file])

        report = SafetyVerificationRunner().run(
            SafetyContext(
                applied_result=result,
                declared_changed_files=deps.changed_files,
                actual_changed_files=[applied_file],
                workspace_roots=[tmp],
            )
        )
        self.assertEqual(report["status"], "passed")
        failed = {item["name"] for item in report["checks"] if item["status"] == "failed"}
        self.assertEqual(failed, set())


if __name__ == "__main__":
    unittest.main()
