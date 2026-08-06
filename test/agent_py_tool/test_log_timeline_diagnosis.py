#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""崩溃前日志时序 / 业务路径旁路（04e）单测。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from tools.timeline_diagnosis.core import (
    run_log_timeline_diagnosis,
    should_run_timeline_analysis,
)


_SAMPLE_LOGCAT = """
01-02 10:00:01.001  1234  1234 I ActivityManager: Start proc com.example
01-02 10:00:02.002  1234  1234 I MainActivity: onCreate
01-02 10:00:03.003  1234  1234 I MainActivity: onResume
01-02 10:00:04.004  1234  1234 D UI: button click submit
01-02 10:00:05.005  1234  1234 I OkHttp: HTTP request GET /api/user
01-02 10:00:06.006  1234  1234 E NativeCrash: Fatal signal 11 (SIGSEGV)
01-02 10:00:06.007  1234  1234 E DEBUG: pid: 1234, tid: 1234
01-02 10:00:06.008  1234  1234 E DEBUG: signal 11 (SIGSEGV), code 1
""".strip()


_PURE_STACK = "\n".join(
    [
        "Fatal signal 11 (SIGSEGV)",
        "#00 pc 00001234 /system/lib64/libc.so",
        "#01 pc 00005678 /data/app/libapp.so",
    ]
    * 8
)


_GENERIC_TS_ONLY = "\n".join(
    [
        "Date/Time:       2026-04-08 10:43:08.123 +0800",
        "OS Version:      macOS 15.0",
        "Exception Type:  EXC_BAD_ACCESS (SIGSEGV)",
        "2026-04-08 10:43:01 something happened before",
        "2026-04-08 10:43:05 another line with timestamp",
        "2026-04-08 10:43:08 crash moment",
        "Thread 0 Crashed:",
        "0   libsystem  0x0000  null deref",
    ]
)


class TestTimelineDiagnosis(unittest.TestCase):
    def test_should_run_requires_log_signals(self):
        self.assertTrue(should_run_timeline_analysis({}, _SAMPLE_LOGCAT))
        self.assertFalse(should_run_timeline_analysis({}, "short"))
        self.assertFalse(should_run_timeline_analysis({}, _PURE_STACK))
        self.assertFalse(should_run_timeline_analysis({}, _GENERIC_TS_ONLY))
        self.assertTrue(should_run_timeline_analysis({}, "x", force=True))
        self.assertTrue(
            should_run_timeline_analysis(
                {"raw_log_sections": ["hilog", "maps"]},
                _PURE_STACK,
            )
        )

    def test_extract_logcat_and_business(self):
        out = run_log_timeline_diagnosis(
            {"raw_content": _SAMPLE_LOGCAT},
            _SAMPLE_LOGCAT,
        )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertTrue(out.get("analyzed"))
        self.assertEqual(out.get("format_detected"), "android_logcat")
        self.assertGreaterEqual(int(out.get("entry_count") or 0), 3)
        biz = out.get("business_flow") or {}
        self.assertTrue(out.get("has_business_ops") or biz.get("operations"))
        prompt = out.get("prompt_section_zh") or ""
        self.assertIn("业务路径", prompt)
        self.assertIn("时序", prompt)

    def test_pure_stack_skipped_without_force(self):
        out = run_log_timeline_diagnosis({"raw_content": _PURE_STACK}, _PURE_STACK)
        self.assertIsNone(out)

    def test_generic_timestamp_not_analyzed_unless_force(self):
        out = run_log_timeline_diagnosis(
            {"raw_content": _GENERIC_TS_ONLY},
            _GENERIC_TS_ONLY,
        )
        # 无强格式信号 → 默认不跑
        self.assertIsNone(out)

        forced = run_log_timeline_diagnosis(
            {"raw_content": _GENERIC_TS_ONLY},
            _GENERIC_TS_ONLY,
            force=True,
        )
        self.assertIsNotNone(forced)
        assert forced is not None
        # force 下可抽 generic；若抽到也允许 analyzed
        self.assertIn("analyzed", forced)

    def test_force_still_returns_structure(self):
        out = run_log_timeline_diagnosis(
            {},
            "Fatal signal only\n#00 pc\n#01 pc\n",
            force=True,
        )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertIn("analyzed", out)


if __name__ == "__main__":
    unittest.main()
