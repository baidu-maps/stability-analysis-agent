#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""merge_utils 单元测试。"""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from tools.merge_utils import merge_resolved_view, build_crash_summary_view


class TestMergeResolvedView(unittest.TestCase):
    """merge_resolved_view 合并测试"""

    def test_basic_merge(self):
        """新格式 02（无 per-frame thread 字段）能正确注入线程上下文"""
        parse_result = {
            "threads": [
                {
                    "tid": "123",
                    "name": "main",
                    "thread_index": 0,
                    "is_crash_thread": True,
                    "frames": [
                        {
                            "frame_number": 0,
                            "address": "0x1000",
                            "function": "_Z4testv",
                            "module": "libapp.so",
                            "offset": "56",
                            "library_type": "app",
                            "layer": "native",
                            "language": "cpp",
                        }
                    ],
                }
            ],
        }
        add2line_result = {
            "resolved_threads": [
                {
                    "tid": "123",
                    "name": "main",
                    "thread_index": 0,
                    "is_crash_thread": True,
                    "is_main_thread": True,
                    "frames": [
                        {
                            "address": "0x1000",
                            "function": "_Z4testv",
                            "module": "libapp.so",
                            "resolved_function": "test()",
                            "resolved_file": "/src/test.cpp",
                            "resolved_line": 42,
                            "resolution_kind": "addr2line",
                        }
                    ],
                }
            ],
        }
        merged = merge_resolved_view(parse_result, add2line_result)
        self.assertEqual(len(merged), 1)
        frame = merged[0]
        # 02 的解析结果
        self.assertEqual(frame["resolved_function"], "test()")
        self.assertEqual(frame["resolved_file"], "/src/test.cpp")
        self.assertEqual(frame["resolved_line"], 42)
        self.assertEqual(frame["resolution_kind"], "addr2line")
        # 注入的线程上下文
        self.assertEqual(frame["thread_tid"], "123")
        self.assertEqual(frame["thread_name"], "main")
        self.assertEqual(frame["thread_index"], 0)
        self.assertTrue(frame["thread_is_crash_thread"])
        self.assertTrue(frame["thread_is_main_thread"])
        # 01 的补充字段
        self.assertEqual(frame["offset"], "56")
        self.assertEqual(frame["library_type"], "app")
        self.assertEqual(frame["layer"], "native")
        self.assertEqual(frame["language"], "cpp")

    def test_old_format_passthrough(self):
        """旧格式 02（帧含 thread_* 字段）直接透传"""
        add2line_result = {
            "resolved_threads": [
                {
                    "tid": "1",
                    "name": "main",
                    "is_crash_thread": True,
                    "frames": [
                        {
                            "address": "0x1000",
                            "resolved_function": "foo()",
                            "thread_tid": "1",
                            "thread_name": "main",
                            "thread_is_crash_thread": True,
                            "thread_is_main_thread": True,
                        }
                    ],
                }
            ],
        }
        merged = merge_resolved_view({}, add2line_result)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["thread_tid"], "1")
        self.assertTrue(merged[0]["thread_is_crash_thread"])

    def test_empty_inputs(self):
        """空输入不崩溃"""
        self.assertEqual(merge_resolved_view({}, {}), [])
        self.assertEqual(merge_resolved_view(None, None), [])


class TestBuildCrashSummaryView(unittest.TestCase):
    """build_crash_summary_view 构建测试"""

    def test_builds_from_01_02_03(self):
        """从 01+02+03 正确组装 crash_summary"""
        parse_result = {
            "crash_info": {
                "signal": "11 (SIGSEGV)",
                "crash_reason": "segmentation fault",
                "crash_address": "0x100546ffc",
            }
        }
        add2line_result = {
            "crash_thread_id": "123",
            "crash_thread_name": "main",
            "crash_thread_is_main_thread": True,
            "crash_thread_has_business_frames": True,
            "resolved_threads": [
                {
                    "tid": "123",
                    "name": "main",
                    "is_crash_thread": True,
                    "frames": [
                        {
                            "address": "0x100546ffc",
                            "resolved_function": "crash_nullptr()",
                            "resolved_file": "/src/my_lib.cpp",
                            "resolved_line": 185,
                            "resolution_kind": "addr2line",
                        }
                    ],
                }
            ],
        }
        code_context = {
            "crash_func": {
                "name": "crash_nullptr",
                "signature": "void crash_nullptr()",
                "crash_line": "int* p = nullptr; *p = 1;",
                "crash_line_number": 185,
            },
            "graph": {
                "nodes": [
                    {
                        "id": "func|/src/my_lib.cpp|void crash_nullptr()",
                        "type": "function",
                        "file": "/src/my_lib.cpp",
                        "signature": "void crash_nullptr()",
                        "snippet": ["void crash_nullptr() {", "  ..."],
                    }
                ]
            },
        }
        summary = build_crash_summary_view(parse_result, add2line_result, code_context)
        self.assertEqual(summary["error_type"], "SIGSEGV")
        self.assertEqual(summary["crash_thread_id"], "123")
        self.assertEqual(summary["crash_thread_name"], "main")
        self.assertTrue(summary["is_main_thread_crash"])
        self.assertEqual(summary["function"], "void crash_nullptr()")
        self.assertEqual(summary["file"], "/src/my_lib.cpp")
        self.assertIn("crash_nullptr", summary.get("analysis_entry_function", ""))

    def test_minimal_input(self):
        """最小输入不崩溃"""
        summary = build_crash_summary_view({}, {}, None)
        self.assertIn("error_type", summary)
        self.assertIn("crash_thread_id", summary)

    def test_fallback_to_02_first_frame(self):
        """无 03 graph 时从 02 首帧获取位置"""
        add2line_result = {
            "crash_thread_id": "1",
            "resolved_threads": [
                {
                    "tid": "1",
                    "is_crash_thread": True,
                    "frames": [
                        {
                            "address": "0xabc",
                            "resolved_function": "bar()",
                            "resolved_file": "/src/bar.cpp",
                            "resolved_line": 10,
                            "resolution_kind": "addr2line",
                        }
                    ],
                }
            ],
        }
        summary = build_crash_summary_view(
            {"crash_info": {"signal": "6 (SIGABRT)"}},
            add2line_result,
            {},
        )
        self.assertEqual(summary["error_type"], "SIGABRT")
        self.assertEqual(summary["function"], "bar()")
        self.assertEqual(summary["file"], "/src/bar.cpp")
        self.assertEqual(summary["crash_line_number"], 10)
        self.assertEqual(summary["crash_location_source"], "from_add2line")


if __name__ == "__main__":
    unittest.main()
