#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import unittest
from pathlib import Path

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

from tools.analysis_entry_display import (
    CONFIDENCE_DIRECT_CRASH_THREAD,
    CONFIDENCE_INVESTIGATION_HINT,
    ENTRY_TYPE_ZH_ALTERNATE,
    ENTRY_TYPE_ZH_DIRECT,
    SOURCE_CRASH_THREAD_BUSINESS_FRAME,
    SOURCE_RESOLVED_BUSINESS_THREAD,
    SOURCE_SELECTED_STACK_FRAME,
    display_entry_type,
    normalize_entry_type,
    resolve_analysis_entry_confidence_source,
)
from tools.code_content_provider_tool import _build_readable_crash_summary


class TestAnalysisEntryDisplay(unittest.TestCase):
    def test_display_entry_type_two_tiers(self):
        self.assertEqual(
            display_entry_type(
                CONFIDENCE_DIRECT_CRASH_THREAD,
                SOURCE_CRASH_THREAD_BUSINESS_FRAME,
            ),
            ENTRY_TYPE_ZH_DIRECT,
        )
        self.assertEqual(
            display_entry_type(
                CONFIDENCE_INVESTIGATION_HINT,
                SOURCE_RESOLVED_BUSINESS_THREAD,
            ),
            ENTRY_TYPE_ZH_ALTERNATE,
        )
        self.assertEqual(
            display_entry_type(
                CONFIDENCE_INVESTIGATION_HINT,
                SOURCE_SELECTED_STACK_FRAME,
            ),
            ENTRY_TYPE_ZH_ALTERNATE,
        )

    def test_normalize_entry_type_roundtrip(self):
        conf, src = normalize_entry_type(ENTRY_TYPE_ZH_DIRECT)
        self.assertEqual(conf, CONFIDENCE_DIRECT_CRASH_THREAD)
        self.assertEqual(src, SOURCE_CRASH_THREAD_BUSINESS_FRAME)
        conf, src = normalize_entry_type(ENTRY_TYPE_ZH_ALTERNATE)
        self.assertEqual(conf, CONFIDENCE_INVESTIGATION_HINT)
        self.assertEqual(src, SOURCE_RESOLVED_BUSINESS_THREAD)

    def test_resolve_from_v2_analysis_entry(self):
        conf, src = resolve_analysis_entry_confidence_source(
            {"entry_type": ENTRY_TYPE_ZH_ALTERNATE}
        )
        self.assertEqual(conf, CONFIDENCE_INVESTIGATION_HINT)
        self.assertEqual(src, SOURCE_RESOLVED_BUSINESS_THREAD)

    def test_build_readable_omits_analysis_entry_when_not_direct(self):
        cs = {
            "error_type": "SIGSEGV",
            "selected_analysis_confidence": CONFIDENCE_INVESTIGATION_HINT,
            "selected_analysis_source": SOURCE_RESOLVED_BUSINESS_THREAD,
            "selected_analysis_is_crash_thread": False,
            "crash_thread_has_business_frames": False,
            "attributed_crash_location_status": "unresolved_crash_thread_no_business_frame",
            "crash_location_source": "unresolved_crash_thread_no_business_frame",
        }
        out = _build_readable_crash_summary(cs)
        self.assertNotIn("analysis_entry", out)
        self.assertIn("crash_location", out)

    def test_build_readable_enriches_crash_location_for_direct_attribution(self):
        cs = {
            "error_type": "SIGSEGV",
            "selected_analysis_confidence": CONFIDENCE_DIRECT_CRASH_THREAD,
            "selected_analysis_source": SOURCE_CRASH_THREAD_BUSINESS_FRAME,
            "selected_analysis_is_crash_thread": True,
            "attributed_crash_location_status": "resolved_to_business_frame",
            "crash_location_source": "from_add2line",
            "analysis_entry_file": "/tmp/Foo.cpp",
            "analysis_entry_function": "void Crash()",
            "crash_line_number": 10,
            "crash_line_code": "x();",
            "stack_address": "0x1000",
            "node_id": "func|/tmp/Foo.cpp|void Crash()",
        }
        out = _build_readable_crash_summary(cs)
        self.assertNotIn("analysis_entry", out)
        loc = out.get("crash_location") or {}
        self.assertEqual(loc.get("file"), "/tmp/Foo.cpp")
        self.assertEqual(loc.get("function"), "void Crash()")
        self.assertEqual(loc.get("line"), 10)
        self.assertEqual(loc.get("code"), "x();")
        self.assertEqual(loc.get("source"), "from_add2line")
        self.assertEqual(loc.get("stack_address"), "0x1000")

    def test_build_readable_weak_attribution_has_location_type_only(self):
        cs = {
            "error_type": "SIGSEGV",
            "selected_analysis_confidence": CONFIDENCE_INVESTIGATION_HINT,
            "selected_analysis_source": SOURCE_RESOLVED_BUSINESS_THREAD,
            "selected_analysis_is_crash_thread": False,
            "attributed_crash_location_status": "unresolved_crash_thread_no_business_frame",
            "crash_location_source": "unresolved_crash_thread_no_business_frame",
            "analysis_entry_file": "/tmp/Other.cpp",
            "crash_line_number": 99,
        }
        out = _build_readable_crash_summary(cs)
        self.assertNotIn("analysis_entry", out)
        loc = out.get("crash_location") or {}
        self.assertIn("location_type", loc)
        self.assertNotIn("line", loc)
        self.assertNotIn("file", loc)


if __name__ == "__main__":
    unittest.main()
