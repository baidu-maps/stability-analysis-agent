#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest

from tools.crash_location_display import (
    LOCATION_TYPE_ZH_LOG_DEDUCE,
    LOCATION_TYPE_ZH_RESOLVED_ADD2LINE,
    CRASH_POSITION_PROMPT_ZH_WEAK,
    LOCATION_TYPE_ZH_UNRESOLVED_NO_BUSINESS,
    format_crash_position_summary_line,
    SOURCE_FROM_ADD2LINE,
    SOURCE_FROM_LOG_DEDUCE,
    SOURCE_UNRESOLVED_CRASH_THREAD_NO_BUSINESS,
    STATUS_RESOLVED_TO_BUSINESS_FRAME,
    STATUS_UNRESOLVED_CRASH_THREAD_NO_BUSINESS,
    display_location_type,
    normalize_location_type,
    resolve_crash_location_status_source,
)
from tools.code_content_provider_tool import _build_readable_crash_summary
from workflows.crash_analysis_workflow import BaseCrashAnalysisWorkflow


class TestCrashLocationDisplay(unittest.TestCase):
    def test_display_unresolved_no_business(self):
        text = display_location_type(
            STATUS_UNRESOLVED_CRASH_THREAD_NO_BUSINESS,
            SOURCE_UNRESOLVED_CRASH_THREAD_NO_BUSINESS,
        )
        self.assertEqual(text, LOCATION_TYPE_ZH_UNRESOLVED_NO_BUSINESS)

    def test_display_resolved_add2line(self):
        cs = {"crash_line_number": 10, "crash_line_code": "x = 1;"}
        text = display_location_type(
            STATUS_RESOLVED_TO_BUSINESS_FRAME,
            SOURCE_FROM_ADD2LINE,
            crash_summary=cs,
        )
        self.assertEqual(text, LOCATION_TYPE_ZH_RESOLVED_ADD2LINE)

    def test_display_log_deduce(self):
        text = display_location_type(
            STATUS_RESOLVED_TO_BUSINESS_FRAME,
            SOURCE_FROM_LOG_DEDUCE,
        )
        self.assertEqual(text, LOCATION_TYPE_ZH_LOG_DEDUCE)

    def test_normalize_roundtrip(self):
        st, src = normalize_location_type(LOCATION_TYPE_ZH_UNRESOLVED_NO_BUSINESS)
        self.assertEqual(st, STATUS_UNRESOLVED_CRASH_THREAD_NO_BUSINESS)
        self.assertEqual(src, SOURCE_UNRESOLVED_CRASH_THREAD_NO_BUSINESS)
        st, src = normalize_location_type(LOCATION_TYPE_ZH_RESOLVED_ADD2LINE)
        self.assertEqual(st, STATUS_RESOLVED_TO_BUSINESS_FRAME)
        self.assertEqual(src, SOURCE_FROM_ADD2LINE)

    def test_resolve_from_v2_crash_location(self):
        st, src = resolve_crash_location_status_source(
            {"location_type": LOCATION_TYPE_ZH_UNRESOLVED_NO_BUSINESS}
        )
        self.assertEqual(st, STATUS_UNRESOLVED_CRASH_THREAD_NO_BUSINESS)
        self.assertEqual(src, SOURCE_UNRESOLVED_CRASH_THREAD_NO_BUSINESS)

    def test_build_readable_uses_location_type_only(self):
        cs = {
            "attributed_crash_location_status": (
                STATUS_UNRESOLVED_CRASH_THREAD_NO_BUSINESS
            ),
            "crash_location_source": SOURCE_UNRESOLVED_CRASH_THREAD_NO_BUSINESS,
            "crash_line_note": "旧 reason 不应出现在 03",
            "selected_analysis_is_crash_thread": False,
        }
        out = _build_readable_crash_summary(cs)
        loc = out.get("crash_location") or {}
        self.assertEqual(loc.get("location_type"), LOCATION_TYPE_ZH_UNRESOLVED_NO_BUSINESS)
        self.assertNotIn("status", loc)
        self.assertNotIn("source", loc)
        self.assertNotIn("reason", loc)
        self.assertNotIn("line", loc)

    def test_format_crash_position_merges_weak_attribution(self):
        text = format_crash_position_summary_line(
            {
                "crash_thread_has_business_frames": False,
                "selected_analysis_is_crash_thread": False,
                "attributed_crash_location_status": (
                    STATUS_UNRESOLVED_CRASH_THREAD_NO_BUSINESS
                ),
                "crash_location": {
                    "location_type": LOCATION_TYPE_ZH_UNRESOLVED_NO_BUSINESS,
                },
            }
        )
        self.assertEqual(text, CRASH_POSITION_PROMPT_ZH_WEAK)

    def test_format_crash_position_shows_func_line_when_resolved(self):
        text = format_crash_position_summary_line(
            {
                "crash_location_source": "from_add2line",
                "crash_line_number": 42,
                "crash_line_code": "*p = 0;",
            },
            {"signature": "void Crash::hit()", "file": "/tmp/Crash.cpp"},
        )
        self.assertIn("日志崩溃线程上的崩溃位置:", text)
        self.assertIn("/tmp/Crash.cpp:42", text)
        self.assertIn("*p = 0;", text)

    def test_compat_restores_internal_enums_for_05(self):
        compat = BaseCrashAnalysisWorkflow._compat_crash_summary(
            {
                "crash_location": {
                    "location_type": LOCATION_TYPE_ZH_UNRESOLVED_NO_BUSINESS,
                }
            }
        )
        self.assertEqual(
            compat.get("attributed_crash_location_status"),
            STATUS_UNRESOLVED_CRASH_THREAD_NO_BUSINESS,
        )
        self.assertEqual(
            compat.get("crash_location_source"),
            SOURCE_UNRESOLVED_CRASH_THREAD_NO_BUSINESS,
        )
        self.assertEqual(
            compat.get("crash_line_note"),
            LOCATION_TYPE_ZH_UNRESOLVED_NO_BUSINESS,
        )


if __name__ == "__main__":
    unittest.main()
