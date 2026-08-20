#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest

from tools.parse_crash_errors import (
    flatten_frames_from_parse_result,
    frame_has_usable_info,
    parse_result_failure_message,
    parse_result_has_failure,
    parse_result_has_usable_crash_data,
    pipeline_skip_metadata,
)


class TestParseCrashErrors(unittest.TestCase):
    def test_usable_frame_address(self):
        self.assertTrue(frame_has_usable_info({"address": "0x1000", "function": ""}))
        self.assertTrue(frame_has_usable_info({"address": "000000000160b4dc", "function": ""}))

    def test_usable_frame_symbol(self):
        self.assertTrue(frame_has_usable_info({"function": "MyClass::foo()"}))

    def test_not_usable_empty(self):
        self.assertFalse(frame_has_usable_info({"address": "", "function": ""}))

    def test_parse_error_payload(self):
        ctx = {"error": "parse failed"}
        self.assertTrue(parse_result_has_failure(ctx))
        self.assertFalse(parse_result_has_usable_crash_data(ctx))

    def test_parse_status_error(self):
        ctx = {
            "parse_status": "error",
            "threads": [],
            "crash_info": {},
            "meta_info": {"os_type": "ios"},
        }
        self.assertFalse(parse_result_has_usable_crash_data(ctx))

    def test_usable_threads(self):
        ctx = {
            "parse_status": "partial_log",
            "threads": [{"frames": [{"address": "0x1", "function": "bar"}]}],
            "crash_info": {},
            "meta_info": {"os_type": "ios"},
        }
        self.assertTrue(parse_result_has_usable_crash_data(ctx))
        self.assertEqual(len(flatten_frames_from_parse_result(ctx)), 1)

    def test_all_frames_empty_not_usable(self):
        ctx = {
            "parse_status": "ok",
            "threads": [{"frames": [{"address": "", "function": ""}]}],
            "crash_info": {},
            "meta_info": {"os_type": "linux"},
        }
        self.assertFalse(parse_result_has_usable_crash_data(ctx))

    def test_pipeline_skip_metadata(self):
        ctx = {"parse_status": "error", "threads": [], "meta_info": {"os_type": "ios"}}
        meta = pipeline_skip_metadata(ctx)
        self.assertTrue(meta.get("pipeline_skipped"))
        self.assertTrue(meta.get("llm_skipped"))
        self.assertIn("堆栈", parse_result_failure_message(ctx) or meta.get("pipeline_skip_user_message", ""))


if __name__ == "__main__":
    unittest.main()
