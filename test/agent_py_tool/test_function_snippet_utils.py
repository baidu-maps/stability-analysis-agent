#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from services.code_locator import CodeLocatorService, LocatorConfig, SymbolLocator, LocatorContext
from tools.function_snippet_utils import (
    is_control_flow_source_line,
    is_plausible_function_signature,
)


class TestFunctionSnippetUtils(unittest.TestCase):
    def test_else_if_line_is_control_flow(self):
        line = "} else if (ev == CVRunLoopQueue::Complete) {"
        self.assertTrue(is_control_flow_source_line(line))
        self.assertFalse(is_plausible_function_signature(line))

    def test_mapschedule_m_name_does_not_yield_if_function(self):
        from pathlib import Path

        fp = Path(__file__).resolve().parents[2].parent / "engine-dev"
        map_file = (
            Path("/Users/liuhong_cd/baidu/mapclient/engine-dev")
            / "src/app/map/basemap/vmap/MapSchedule.cpp"
        )
        if not map_file.is_file():
            self.skipTest("MapSchedule.cpp not available locally")
        lines = map_file.read_text(encoding="utf-8", errors="ignore").splitlines()
        ctx = LocatorContext(LocatorConfig())
        sym = SymbolLocator(ctx)
        fn = sym.extract_function_name_at_line(lines, 115)
        self.assertEqual(fn, "onTaskEventHandler")
        cfg = LocatorConfig(max_shared_var_related_functions=50)
        loc = CodeLocatorService(cfg)
        results = loc.find_variable_usages(
            ["m_name"],
            "dummy",
            [str(map_file.parent.parent.parent.parent.parent)],
            stack_priority_files=[str(map_file)],
            crash_local_files=[str(map_file)],
        )
        bad = [
            r
            for r in results
            if str(r.name) in {"if", "else"}
            or (
                r.snippet
                and "else if (ev == CVRunLoopQueue::Complete)" in (r.snippet[0] or "")
            )
        ]
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
