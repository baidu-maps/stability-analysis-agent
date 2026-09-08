from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from services.safety_verification import (
    SafetyContext,
    SafetyVerificationRunner,
    verify_sync_integrity,
)


class SafetyVerificationTests(unittest.TestCase):
    def test_exact_workspace_change_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "a.cpp")
            report = SafetyVerificationRunner().run(SafetyContext(
                applied_result={"success": True},
                declared_changed_files=[path],
                actual_changed_files=[path],
                workspace_roots=[tmp],
            ))
        self.assertEqual(report["status"], "passed")

    def test_undeclared_or_outside_change_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            declared = str(Path(tmp) / "a.cpp")
            report = SafetyVerificationRunner().run(SafetyContext(
                applied_result={"success": True},
                declared_changed_files=[declared],
                actual_changed_files=[declared, "/tmp/outside.cpp"],
                workspace_roots=[tmp],
            ))
        self.assertEqual(report["status"], "failed")
        failed = {item["name"] for item in report["checks"] if item["status"] == "failed"}
        self.assertEqual(failed, {"workspace_boundary", "change_manifest"})

    def test_core_checks_cannot_be_disabled(self) -> None:
        runner = SafetyVerificationRunner([])
        self.assertEqual(set(runner.checks), {"patch_applied", "workspace_boundary", "change_manifest"})

    def test_unknown_check_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SafetyVerificationRunner(["guess_code_quality"])

    def test_sync_integrity_requires_exact_file_set(self) -> None:
        self.assertEqual(
            verify_sync_integrity(expected_files=["/tmp/a"], copied_files=["/tmp/a"])["status"],
            "passed",
        )
        self.assertEqual(
            verify_sync_integrity(expected_files=["/tmp/a"], copied_files=[])["status"],
            "failed",
        )


if __name__ == "__main__":
    unittest.main()
