#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for source-identity risk flags recorded alongside automated repair."""

from __future__ import annotations

import unittest

from services.evidence_contract import evaluate_source_identity


class SourceIdentityContractTests(unittest.TestCase):
    """Empty identity stays compatible; unpinned identity stays flagged for review."""

    def test_empty_identity_is_safe(self) -> None:
        gate = evaluate_source_identity({})
        self.assertTrue(gate["safe"])
        self.assertFalse(gate["hard_block"])

    def test_branch_latest_is_hard_block(self) -> None:
        gate = evaluate_source_identity({
            "source_origin": "icafe_branch_fallback",
            "commit_policy": "branch_latest",
        })
        self.assertTrue(gate["hard_block"])
        self.assertFalse(gate["safe"])

    def test_enterprise_repair_safety_signal_is_hard_block(self) -> None:
        gate = evaluate_source_identity({
            "source_identity": {
                "source_origin": "icafe_branch_fallback",
                "commit_policy": "branch_latest",
                "source_may_include_fix": True,
            },
            "repair_safety": {
                "hard_block_auto_repair": True,
                "signal": "forbid_auto_code_write",
            },
        })
        self.assertTrue(gate["hard_block"])
        self.assertIn("forbid_auto_code_write", gate["reasons"])


if __name__ == "__main__":
    unittest.main()
