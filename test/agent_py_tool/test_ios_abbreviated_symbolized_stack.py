#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""缩写 iOS 栈：无帧号的「模块 0x地址 符号」，以及 Thread: 名字。"""

import json
import unittest

from tools.crash_log_parser_tool import crash_log_parser


_SAMPLE = """\
BUG复现步骤：

EXC_BAD_ACCESS: Exception 1, Code 1, Subcode 0 >

Attempted to dereference null pointer.

  map_plugin          0x10ae3b7c8  walk_navi::CRoute::GetLegSize

  libsystem_pthread   0x1e04ef44c  _pthread_start

Thread: Tmcom-Socket

  map_plugin          0x10ae3b7c8  walk_navi::CRoute::GetLegSize

  libsystem_pthread   0x1e04ef44c  _pthread_start

期望结果：

实际结果：
"""


class TestIosAbbreviatedSymbolizedStack(unittest.TestCase):
    def test_splits_named_thread_and_keeps_module(self):
        out = json.loads(crash_log_parser(_SAMPLE))
        self.assertEqual(out.get("parse_status"), "ok")
        meta = out.get("meta_info") or {}
        self.assertEqual(meta.get("os_type"), "ios")
        threads = out.get("threads") or []
        self.assertEqual(len(threads), 2)
        crash = threads[0]
        other = threads[1]
        self.assertTrue(crash.get("is_crash_thread"))
        self.assertFalse(other.get("is_crash_thread"))
        self.assertEqual(other.get("name"), "Tmcom-Socket")
        crash_frames = crash.get("frames") or []
        self.assertEqual(len(crash_frames), 2)
        self.assertEqual(crash_frames[0].get("module"), "map_plugin")
        self.assertEqual(crash_frames[0].get("address"), "0x10ae3b7c8")
        self.assertEqual(crash_frames[0].get("function"), "walk_navi::CRoute::GetLegSize")
        self.assertEqual(crash_frames[1].get("module"), "libsystem_pthread")
        self.assertEqual(crash_frames[1].get("library_type"), "system")
        other_frames = other.get("frames") or []
        self.assertEqual(len(other_frames), 2)
        self.assertEqual(other_frames[0].get("module"), "map_plugin")


if __name__ == "__main__":
    unittest.main()
