#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""parse_stack_only Markdown 报告生成单测。"""

from __future__ import annotations

import unittest

from services.stack_resolve_report import (
    build_parse_stack_only_markdown,
    format_addr2line_detail,
)


class StackResolveReportTests(unittest.TestCase):
    """两列表格与 addr2line 明细格式。"""

    def test_format_addr2line_detail(self) -> None:
        """函数 + file:line 两行。"""
        text = format_addr2line_detail(
            {
                "resolved_function": "Foo::bar()",
                "resolved_file": "/tmp/a.cpp",
                "resolved_line": 12,
            }
        )
        self.assertEqual(text, "Foo::bar()\n/tmp/a.cpp:12")

    def test_build_table_columns(self) -> None:
        """表格含地址、解析结果；不含原日志行号列。"""
        md = build_parse_stack_only_markdown(
            {
                "success_count": 1,
                "total_count": 1,
                "tool_name": "llvm-addr2line",
                "resolved_threads": [
                    {
                        "tid": "1",
                        "name": "main",
                        "is_crash_thread": True,
                        "is_main_thread": True,
                        "frames": [
                            {
                                "address": "000000000071802c",
                                "resolved_function": "HUF_DecompressFastArgs_init",
                                "resolved_file": "huf_decompress.c",
                                "resolved_line": 0,
                                "resolution_kind": "addr2line",
                            }
                        ],
                    }
                ],
            },
            crash_info={"signal": "SIGSEGV", "crash_reason": "null deref"},
        )
        self.assertIn("## 堆栈地址解析结果", md)
        self.assertIn("| 原始堆栈地址 | 解析结果（addr2line） |", md)
        self.assertNotIn("原日志行号", md)
        self.assertNotIn("raw_log_line", md)
        self.assertIn("`000000000071802c`", md)
        self.assertIn("HUF_DecompressFastArgs_init", md)
        self.assertIn("huf_decompress.c:0", md)
        self.assertIn("SIGSEGV", md)


if __name__ == "__main__":
    unittest.main()
