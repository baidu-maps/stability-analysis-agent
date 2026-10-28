#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定位阶段优先使用 scan_roots，检出根留给改码。"""

from __future__ import annotations

import unittest

from workflows.crash_analysis_workflow import location_scan_roots


class LocationScanRootTests(unittest.TestCase):
    """scan_roots 与 code_roots 的选择。"""

    def test_prefers_scan_roots(self) -> None:
        """有扫描目录时不用完整检出根做定位。"""
        roots = location_scan_roots(
            {"scan_roots": ["/narrow"], "code_roots": ["/wide"]},
            ["/wide"],
        )
        self.assertEqual(roots, ["/narrow"])

    def test_falls_back_to_code_roots(self) -> None:
        """未提供扫描目录时继续扫检出根。"""
        roots = location_scan_roots({"code_roots": ["/wide"]}, ["/wide"])
        self.assertEqual(roots, ["/wide"])

    def test_ignores_blank_scan_roots(self) -> None:
        """空白扫描目录回退到检出根。"""
        roots = location_scan_roots({"scan_roots": ["", "  "]}, ["/wide"])
        self.assertEqual(roots, ["/wide"])


if __name__ == "__main__":
    unittest.main()
