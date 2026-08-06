#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ANR/Freeze 诊断接线冒烟测试。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from tools.anr_diagnosis.core import run_anr_freeze_diagnosis, should_run_anr_analysis


class TestAnrFreezeDiagnosis(unittest.TestCase):
    def test_should_run_only_when_suspected_or_forced(self):
        self.assertFalse(should_run_anr_analysis({"meta_info": {}}))
        self.assertTrue(
            should_run_anr_analysis({"meta_info": {"anr_suspected": True}})
        )
        self.assertTrue(should_run_anr_analysis({}, force=True))

    def test_force_runs_hotspot_and_matches_lock(self):
        parse = {
            "meta_info": {"os_type": "android"},
            "threads": [
                {
                    "tid": "1",
                    "name": "main",
                    "is_crash_thread": True,
                    "frames": [
                        {"function": "pthread_mutex_lock", "module": "libc.so"},
                        {"function": "Business::onClick", "module": "libapp.so"},
                    ],
                },
                {
                    "tid": "2",
                    "name": "worker",
                    "is_crash_thread": False,
                    "frames": [
                        {"function": "pthread_mutex_lock", "module": "libc.so"},
                        {"function": "Worker::run", "module": "libapp.so"},
                    ],
                },
            ],
        }
        resolved = {
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
                            "resolved_function": "Business::onClick",
                            "function": "Business::onClick",
                            "module": "libapp.so",
                        },
                    ],
                },
                {
                    "tid": "2",
                    "name": "worker",
                    "is_crash_thread": False,
                    "frames": [
                        {
                            "resolved_function": "pthread_mutex_lock",
                            "function": "pthread_mutex_lock",
                            "module": "libc.so",
                        },
                        {
                            "resolved_function": "Worker::run",
                            "function": "Worker::run",
                            "module": "libapp.so",
                        },
                    ],
                },
            ]
        }
        out = run_anr_freeze_diagnosis(parse, resolved, force=True)
        self.assertIsNotNone(out)
        assert out is not None
        self.assertTrue(out.get("analyzed"))
        hotspots = out.get("stack_hotspots") or {}
        self.assertGreaterEqual(int(hotspots.get("total_frames_analyzed") or 0), 4)
        blockers = hotspots.get("blocking_indicators") or []
        self.assertTrue(any("锁" in str(b) or "mutex" in str(b).lower() for b in blockers))
        self.assertIn("ANR/Freeze", out.get("prompt_section_zh") or "")
        modes = out.get("fault_mode_matches") or []
        self.assertTrue(any(m.get("mode_id") == "main_thread_blocking" for m in modes))

    def test_not_suspected_returns_none(self):
        self.assertIsNone(
            run_anr_freeze_diagnosis({"meta_info": {}}, {"resolved_threads": []})
        )


if __name__ == "__main__":
    unittest.main()
