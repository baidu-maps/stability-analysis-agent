from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.code_fixer import FixResult
from services.repair_actions import (
    RepairActionDeps,
    _apply_patch_handler,
    changed_files_from_applied_result,
)
from tool_system.runtime import RunTrace, RuntimeBudget
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

    def test_text_extract_apply_does_not_spend_exhausted_llm_budget(self) -> None:
        """分析已用满模型次数时，从已有文本提取补丁仍应继续。"""
        budget = RuntimeBudget(max_llm_calls=8, llm_calls=8)
        trace = RunTrace(run_id="budget-full", budget=budget)
        called = {}

        def _fake_generate(self, **_kwargs):
            called["yes"] = True
            return FixResult(
                success=True,
                applied=[{"file": "/tmp/a.cpp", "status": "applied"}],
                summary="fast_path_from_analysis",
            )

        with tempfile.TemporaryDirectory() as tmp:
            deps = RepairActionDeps(
                result={},
                code_roots=[tmp],
                report_dir=Path(tmp),
                run_id="budget-full",
                llm_adapter=object(),
                trace=trace,
            )
            with patch(
                "services.repair_actions.CodeFixer.generate_and_apply",
                _fake_generate,
            ):
                result = _apply_patch_handler(deps, {"code_roots": [tmp], "report_dir": tmp})

        self.assertTrue(called.get("yes"))
        self.assertTrue(result["success"])
        self.assertNotIn("runtime budget exceeded", str(result.get("error") or ""))
        self.assertEqual(budget.llm_calls, 8)


if __name__ == "__main__":
    unittest.main()
