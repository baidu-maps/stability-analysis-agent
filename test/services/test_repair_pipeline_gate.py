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


if __name__ == "__main__":
    unittest.main()
