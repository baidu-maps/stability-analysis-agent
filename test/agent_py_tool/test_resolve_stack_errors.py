#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest

from tools.resolve_stack_errors import (
    pipeline_skip_metadata_resolve,
    resolved_frame_has_usable_symbol,
    resolved_stack_has_failure,
    resolved_stack_has_usable_resolution,
)


class TestResolveStackErrors(unittest.TestCase):
    def test_usable_symbol_from_log_passthrough(self):
        frame = {
            "address": "0x100",
            "function": "Foo::bar()",
            "resolved_function": "Foo::bar()",
            "resolved_file": None,
            "resolved_line": None,
        }
        self.assertTrue(resolved_frame_has_usable_symbol(frame))
        stack = {
            "resolved_threads": [
                {
                    "is_crash_thread": True,
                    "is_main_thread": None,
                    "frames": [frame],
                    "success_count": 1,
                    "total_count": 1,
                }
            ]
        }
        self.assertTrue(resolved_stack_has_usable_resolution(stack))

    def test_file_line_usable(self):
        frame = {
            "address": "0x100",
            "function": "",
            "resolved_file": "/src/a.cpp",
            "resolved_line": 12,
        }
        self.assertTrue(resolved_frame_has_usable_symbol(frame))

    def test_empty_frames_not_usable(self):
        ctx = {"resolved_threads": [], "success_count": 0, "total_count": 5}
        self.assertFalse(resolved_stack_has_usable_resolution(ctx))

    def test_error_payload(self):
        ctx = {"error": "库路径不存在或未提供"}
        self.assertTrue(resolved_stack_has_failure(ctx))
        self.assertFalse(resolved_stack_has_usable_resolution(ctx))

    def test_address_only_fallback(self):
        frame = {"address": "0xdeadbeef", "function": "", "resolved_function": ""}
        self.assertFalse(resolved_frame_has_usable_symbol(frame))
        self.assertTrue(
            resolved_stack_has_usable_resolution(
                {
                    "resolved_threads": [
                        {
                            "is_crash_thread": True,
                            "frames": [frame],
                            "success_count": 0,
                            "total_count": 1,
                        }
                    ]
                }
            )
        )

    def test_skip_metadata(self):
        meta = pipeline_skip_metadata_resolve({"resolved_threads": []})
        self.assertEqual(meta.get("pipeline_skip_reason"), "no_usable_resolve")


if __name__ == "__main__":
    unittest.main()
