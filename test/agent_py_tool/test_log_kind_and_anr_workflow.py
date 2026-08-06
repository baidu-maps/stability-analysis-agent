#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""log_kind 强分类与 ANR workflow 路由单测。"""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from tools.crash_parser.log_kind_classifier import (
    LOG_KIND_ANR_TRACE,
    LOG_KIND_APP_FREEZE,
    LOG_KIND_MIXED_ANR_CRASH,
    LOG_KIND_NATIVE_CRASH,
    LOG_KIND_UNKNOWN,
    LOG_KIND_WATCHDOG,
    classify_log_kind,
    workflow_name_for_log_kind,
)
from tools.crash_parser.meta import extract_meta_info
from tools.anr_diagnosis.core import should_run_anr_analysis


class TestLogKindClassifier(unittest.TestCase):
    def test_anr_keyword(self):
        text = "10-01 12:00:00 I am_anr: ANR in com.example.app\n----- pid 123 at -----\n"
        r = classify_log_kind(text)
        self.assertEqual(r.log_kind, LOG_KIND_ANR_TRACE)
        self.assertGreaterEqual(r.confidence, 0.8)
        self.assertTrue(any("ANR" in x for x in r.reasons))

    def test_appfreeze(self):
        r = classify_log_kind("REASON: AppFreeze\nTid:1 Name:main\n")
        self.assertEqual(r.log_kind, LOG_KIND_APP_FREEZE)

    def test_watchdog(self):
        r = classify_log_kind("watchdog timeout detected on main thread\n")
        self.assertEqual(r.log_kind, LOG_KIND_WATCHDOG)

    def test_mixed_anr_and_crash(self):
        text = "ANR in com.foo\nFatal signal 11 (SIGSEGV), code 1\n#00 pc 00001234\n"
        r = classify_log_kind(text)
        self.assertEqual(r.log_kind, LOG_KIND_MIXED_ANR_CRASH)

    def test_tombstone_native(self):
        text = (
            "*** *** *** *** *** *** *** *** *** *** *** *** *** *** *** ***\n"
            "Build fingerprint: 'google/sdk'\n"
            "Fatal signal 11 (SIGSEGV), code 1, fault addr 0x0\n"
            "#00 pc 0000abcd  /system/lib64/libc.so\n"
        )
        r = classify_log_kind(text)
        self.assertEqual(r.log_kind, LOG_KIND_NATIVE_CRASH)

    def test_unknown(self):
        r = classify_log_kind("hello world nothing useful")
        self.assertEqual(r.log_kind, LOG_KIND_UNKNOWN)

    def test_workflow_routing(self):
        self.assertEqual(
            workflow_name_for_log_kind(LOG_KIND_ANR_TRACE),
            "anr_freeze_analysis",
        )
        self.assertEqual(
            workflow_name_for_log_kind(LOG_KIND_NATIVE_CRASH),
            "crash_analysis",
        )
        self.assertEqual(
            workflow_name_for_log_kind(LOG_KIND_UNKNOWN),
            "crash_analysis",
        )
        self.assertEqual(
            workflow_name_for_log_kind(LOG_KIND_NATIVE_CRASH, force_anr=True),
            "anr_freeze_analysis",
        )

    def test_meta_info_writes_log_kind(self):
        meta = extract_meta_info("ANR in com.example\nBuild fingerprint: test\n")
        self.assertEqual(meta.log_kind, LOG_KIND_ANR_TRACE)
        self.assertTrue(meta.anr_suspected)
        self.assertIsNotNone(meta.log_kind_confidence)
        self.assertTrue(meta.log_kind_reasons)

    def test_should_run_prefers_log_kind(self):
        self.assertTrue(
            should_run_anr_analysis({"meta_info": {"log_kind": LOG_KIND_ANR_TRACE}})
        )
        self.assertFalse(
            should_run_anr_analysis({"meta_info": {"log_kind": LOG_KIND_NATIVE_CRASH}})
        )
        self.assertTrue(
            should_run_anr_analysis({"meta_info": {"anr_suspected": True}})
        )


class TestAnrFreezeWorkflowParseStack(unittest.TestCase):
    def test_parse_stack_only_emits_04c(self):
        from workflows.anr_freeze_workflow import AnrFreezeAnalysisWorkflow

        wf = AnrFreezeAnalysisWorkflow()
        parse_result = {
            "meta_info": {
                "os_type": "android",
                "log_kind": LOG_KIND_ANR_TRACE,
                "log_kind_confidence": 0.9,
                "log_kind_reasons": ["keyword:ANR"],
                "anr_suspected": True,
            },
            "crash_info": {"crash_reason": "ANR", "category": "anr"},
            "threads": [
                {
                    "tid": "1",
                    "name": "main",
                    "is_crash_thread": True,
                    "frames": [
                        {"function": "pthread_mutex_lock", "module": "libc.so"},
                        {"function": "App::onClick", "module": "libapp.so"},
                    ],
                }
            ],
        }

        def execute_tool(name, params):
            if name == "crash_log_parser":
                return parse_result
            if name == "add2line_resolver":
                return {
                    "resolved_threads": [
                        {
                            "tid": "1",
                            "name": "main",
                            "is_crash_thread": True,
                            "frames": [
                                {
                                    "resolved_function": "pthread_mutex_lock",
                                    "function": "pthread_mutex_lock",
                                    "module": "libc.so",
                                },
                                {
                                    "resolved_function": "App::onClick",
                                    "function": "App::onClick",
                                    "module": "libapp.so",
                                },
                            ],
                        }
                    ]
                }
            return {}

        ctx = MagicMock()
        ctx.execute_tool.side_effect = execute_tool

        out = wf.solve(
            {
                "crash_log": "ANR in com.example\n",
                "scope": "parse_stack_only",
                "library_dir": "",
            },
            ctx,
        )
        self.assertEqual(out.get("status"), "success")
        self.assertEqual(out.get("workflow"), "anr_freeze_analysis")
        anr = out.get("anr_diagnosis") or {}
        self.assertTrue(anr.get("analyzed"))
        self.assertIn("prompt_section_zh", anr)


if __name__ == "__main__":
    unittest.main()
