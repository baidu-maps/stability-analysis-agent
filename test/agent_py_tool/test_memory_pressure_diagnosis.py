#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OOM / 内存压力 log_kind 与 04d 旁路单测。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from tools.crash_parser.log_kind_classifier import (
    LOG_KIND_MEMORY_PRESSURE,
    LOG_KIND_MIXED_OOM_CRASH,
    LOG_KIND_NATIVE_CRASH,
    LOG_KIND_OOM_KILL,
    classify_log_kind,
    is_oom_family_kind,
    workflow_name_for_log_kind,
)
from tools.crash_parser.meta import extract_meta_info
from tools.memory_diagnosis.core import (
    run_memory_pressure_diagnosis,
    should_run_memory_analysis,
)


class TestOomLogKind(unittest.TestCase):
    def test_java_oom(self):
        text = (
            "FATAL EXCEPTION: main\n"
            "java.lang.OutOfMemoryError: Failed to allocate a 48 byte allocation\n"
        )
        r = classify_log_kind(text)
        self.assertEqual(r.log_kind, LOG_KIND_OOM_KILL)
        self.assertTrue(is_oom_family_kind(r.log_kind))
        # 阶段 A：仍走 crash workflow
        self.assertEqual(workflow_name_for_log_kind(r.log_kind), "crash_analysis")

    def test_jetsam_with_sigsegv_mixed(self):
        text = "jetsam event\nkilled due to memory\nFatal signal 11 (SIGSEGV)\n"
        r = classify_log_kind(text)
        self.assertEqual(r.log_kind, LOG_KIND_MIXED_OOM_CRASH)

    def test_memory_pressure_soft(self):
        text = "onTrimMemory level=complete\nmemory pressure warning\n"
        r = classify_log_kind(text)
        self.assertEqual(r.log_kind, LOG_KIND_MEMORY_PRESSURE)

    def test_native_crash_not_oom(self):
        text = "Fatal signal 11 (SIGSEGV), code 1, fault addr 0x0\n#00 pc 1234\n"
        r = classify_log_kind(text)
        self.assertEqual(r.log_kind, LOG_KIND_NATIVE_CRASH)
        self.assertFalse(is_oom_family_kind(r.log_kind))

    def test_meta_oom_suspected(self):
        meta = extract_meta_info("OutOfMemoryError: Java heap space\n")
        self.assertEqual(meta.log_kind, LOG_KIND_OOM_KILL)
        self.assertTrue(meta.oom_suspected)


class TestMemoryDiagnosis(unittest.TestCase):
    def test_should_run_on_oom_kind(self):
        self.assertTrue(
            should_run_memory_analysis({"meta_info": {"log_kind": LOG_KIND_OOM_KILL}})
        )
        self.assertFalse(
            should_run_memory_analysis({"meta_info": {"log_kind": LOG_KIND_NATIVE_CRASH}})
        )
        self.assertTrue(should_run_memory_analysis({}, force=True))

    def test_run_produces_04d_payload(self):
        parse = {
            "meta_info": {
                "log_kind": LOG_KIND_OOM_KILL,
                "oom_suspected": True,
            },
            "crash_info": {"category": "oom", "crash_reason": "OutOfMemoryError"},
            "threads": [
                {
                    "tid": "1",
                    "is_crash_thread": True,
                    "frames": [
                        {"function": "std::vector::push_back", "module": "libapp.so"},
                        {"function": "Cache::put", "module": "libapp.so"},
                    ],
                }
            ],
        }
        log = (
            "java.lang.OutOfMemoryError: Java heap space\n"
            "Java heap: 512MB\n"
            "PSS: 800MB\n"
        )
        out = run_memory_pressure_diagnosis(parse, {"resolved_threads": []}, log)
        self.assertIsNotNone(out)
        assert out is not None
        self.assertTrue(out.get("analyzed"))
        self.assertEqual(out.get("memory_subtype"), "java_oom")
        self.assertIn("OutOfMemoryError", out.get("memory_indicators", {}).get("keywords_hit") or [])
        self.assertIn("内存压力", out.get("prompt_section_zh") or "")
        self.assertTrue(out.get("fault_mode_matches") is not None)

    def test_not_suspected_returns_none(self):
        self.assertIsNone(
            run_memory_pressure_diagnosis(
                {"meta_info": {"log_kind": LOG_KIND_NATIVE_CRASH}},
                {},
            )
        )


if __name__ == "__main__":
    unittest.main()
