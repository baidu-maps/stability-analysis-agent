#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""addr2line -i 内联帧解析。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from pathlib import Path

from tools.add2line_resolver_tool import (
    Add2lineResolver,
    ResolvedFrame,
    format_inline_caller_note,
)


class Addr2lineInlineTests(unittest.TestCase):
    """默认展开内联，并保留外层调用者。"""

    def test_parse_keeps_innermost_and_inline_caller(self) -> None:
        """第一对是崩溃指令，后面的对是内联调用者。"""
        output = "\n".join(
            [
                "std::shared_ptr<CVTask>::operator->() const",
                "/opt/ndk/shared_ptr.h:721:12",
                "CGridLayer::CancelTask(bool)",
                "/src/GridLayer.cpp:4103",
            ]
        )
        frames = Add2lineResolver._parse_add2line_frames(output)
        self.assertEqual(frames[0][0], "std::shared_ptr<CVTask>::operator->() const")
        self.assertEqual(frames[0][1], "/opt/ndk/shared_ptr.h")
        self.assertEqual(frames[0][2], 721)
        self.assertEqual(frames[1][0], "CGridLayer::CancelTask(bool)")
        self.assertEqual(frames[1][2], 4103)

    def test_resolve_passes_inline_flag(self) -> None:
        """llvm-addr2line 默认带 -i，结果写入 inline_frames。"""
        resolver = object.__new__(Add2lineResolver)
        resolver._llvm_addr2line_alias_path = None
        stdout = "\n".join(
            [
                "operator->",
                "shared_ptr.h:721",
                "CGridLayer::CancelTask(bool)",
                "GridLayer.cpp:4103",
            ]
        )
        with patch(
            "tools.add2line_resolver_tool.subprocess.run",
            return_value=type(
                "Result",
                (),
                {"returncode": 0, "stdout": stdout, "stderr": ""},
            )(),
        ) as run:
            result = resolver._resolve_with_addr2line("0x4926b4", "lib.so", "llvm-addr2line")
        self.assertEqual(result.resolved_function, "operator->")
        self.assertEqual(result.resolved_line, 721)
        self.assertEqual(result.inline_frames[0]["function"], "CGridLayer::CancelTask(bool)")
        self.assertEqual(result.inline_frames[0]["line"], 4103)
        self.assertIn("-i", run.call_args.args[0])

    def test_inline_note(self) -> None:
        """提示文本带出内联调用者。"""
        note = format_inline_caller_note(
            {
                "resolved_function": "operator->",
                "inline_frames": [
                    {
                        "function": "CGridLayer::CancelTask(bool)",
                        "file": "GridLayer.cpp",
                        "line": 4103,
                    }
                ],
            }
        )
        self.assertEqual(note, "内联于 CGridLayer::CancelTask(bool) (GridLayer.cpp:4103)")

    def test_thread_result_keeps_inline_callers(self) -> None:
        """按线程写回堆栈时，保留 addr2line -i 得到的内联调用者。"""
        resolver = object.__new__(Add2lineResolver)
        resolver.quick_mode = True
        resolver.resolver_tools = {"llvm-addr2line": "llvm-addr2line"}
        inline = [
            {
                "function": "CGridLayer::CancelTask(bool)",
                "file": "GridLayer.cpp",
                "line": 4103,
            }
        ]
        resolved = ResolvedFrame(
            address="0x4926b4",
            resolved_function="std::shared_ptr<CVTask>::operator->() const",
            resolved_file="shared_ptr.h",
            resolved_line=721,
            inline_frames=inline,
        )
        with patch(
            "tools.add2line_resolver_tool.match_libraries_for_module",
            return_value=[Path("libmap.so")],
        ), patch.object(
            resolver, "_resolve_address_with_tool", return_value=resolved
        ):
            frame, ok, err = resolver._resolve_single_frame_for_thread(
                {"address": "0x4926b4", "module": "libmap.so"},
                {"is_crash_thread": True},
                library_files=[Path("libmap.so")],
                crash_os_type="android",
                selected_tool="llvm-addr2line",
                module_base_addresses={},
                library_dir="arm64-v8a",
                attempt_addr2line=True,
            )
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual(frame.resolved_line, 721)
        self.assertEqual(frame.inline_frames, inline)
        self.assertEqual(frame.resolution_kind, "addr2line")


if __name__ == "__main__":
    unittest.main()
