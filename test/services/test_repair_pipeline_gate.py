#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for repair_gate bypass in should_run_repair_pipeline."""

from __future__ import annotations

import unittest

from services.repair_pipeline import should_run_repair_pipeline


class RepairPipelineGateTests(unittest.TestCase):
    """Probable diagnoses should enter apply when bypass is on (default)."""

    def _result(self) -> dict:
        return {
            "status": "success",
            "analysis": "fix suggestion",
            "crash_diagnosis": {
                "diagnosis_status": "probable",
                "confidence": 0.65,
            },
        }

    def test_bypass_default_allows_probable(self) -> None:
        result = self._result()
        allowed = should_run_repair_pipeline(
            apply_ai_fixes=True,
            result=result,
            scope="full",
        )
        self.assertTrue(allowed)
        gate = (result.get("metadata") or {}).get("repair_gate") or {}
        self.assertFalse(gate.get("allowed"))
        self.assertTrue(gate.get("bypassed"))

    def test_no_bypass_blocks_probable(self) -> None:
        result = self._result()
        allowed = should_run_repair_pipeline(
            apply_ai_fixes=True,
            result=result,
            scope="full",
            bypass_repair_gate=False,
        )
        self.assertFalse(allowed)
        gate = (result.get("metadata") or {}).get("repair_gate") or {}
        self.assertFalse(gate.get("allowed"))
        self.assertFalse(gate.get("bypassed"))

    def test_bypass_does_not_override_unpinned_source_identity(self) -> None:
        """branch_latest 身份风险不能被 repair_gate bypass 覆盖。"""
        result = self._result()
        result["metadata"] = {
            "source_identity": {
                "source_origin": "icafe_branch_fallback",
                "commit_policy": "branch_latest",
                "identity_confidence": "unsafe",
            },
            "repair_safety": {
                "hard_block_auto_repair": True,
                "signal": "forbid_auto_code_write",
            },
        }
        allowed = should_run_repair_pipeline(
            apply_ai_fixes=True,
            result=result,
            scope="full",
            bypass_repair_gate=True,
        )
        self.assertFalse(allowed)
        gate = (result.get("metadata") or {}).get("source_identity_gate") or {}
        self.assertTrue(gate.get("hard_block"))

    def test_empty_source_identity_does_not_block_legacy_runs(self) -> None:
        result = self._result()
        result["metadata"] = {
            "source_identity": {
                "source_origin": "",
                "commit_policy": "",
                "base_commit": "",
                "identity_confidence": "",
                "block_reason": "",
            }
        }
        self.assertTrue(
            should_run_repair_pipeline(
                apply_ai_fixes=True,
                result=result,
                scope="full",
            )
        )


if __name__ == "__main__":
    unittest.main()
