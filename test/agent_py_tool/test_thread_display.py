#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from tools.thread_display import (
    format_prompt_thread_identity,
    format_prompt_thread_role_flags,
    normalize_harmony_thread_fields,
)


class TestThreadDisplay(unittest.TestCase):
    def test_normalize_harmony_swapped_fields(self):
        tid, name = normalize_harmony_thread_fields("aime-decisdata8", "22985")
        self.assertEqual(tid, "22985")
        self.assertEqual(name, "aime-decisdata8")

    def test_normalize_harmony_attributed_thread_unchanged(self):
        tid, name = normalize_harmony_thread_fields("21840", "com.anjuke.home")
        self.assertEqual(tid, "21840")
        self.assertEqual(name, "com.anjuke.home")

    def test_normalize_harmony_non_numeric_pair_unchanged(self):
        tid, name = normalize_harmony_thread_fields("worker-1", "worker")
        self.assertEqual(tid, "worker-1")
        self.assertEqual(name, "worker")

    def test_format_prompt_thread_identity(self):
        self.assertEqual(
            format_prompt_thread_identity("22985", "aime-decisdata8"),
            "线程ID=22985 线程名=aime-decisdata8",
        )

    def test_format_prompt_thread_role_flags(self):
        self.assertEqual(
            format_prompt_thread_role_flags(False, False),
            "非崩溃线程，非主线程",
        )
        self.assertEqual(
            format_prompt_thread_role_flags(True, True),
            "崩溃线程，主线程",
        )
        self.assertEqual(
            format_prompt_thread_role_flags(True, False),
            "崩溃线程，非主线程",
        )
        self.assertEqual(
            format_prompt_thread_role_flags(False, None),
            "非崩溃线程",
        )


if __name__ == "__main__":
    unittest.main()
