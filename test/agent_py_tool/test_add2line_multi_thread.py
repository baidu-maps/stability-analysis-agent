#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多线程 01 → 02 resolved_threads 结构测试。"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from tools.add2line_resolver_tool import add2line_resolver
from tools.crash_log_parser_tool import crash_log_parser
from tools.crash_parser.types import CrashParseOptions
from tools.resolve_stack_errors import (
    flatten_resolved_frames_from_stack,
    resolved_stack_has_usable_resolution,
    resolved_threads_from_stack,
)


HARMONY_PC = (
    "#00 pc 00000000001d8250 /system/lib/ld-musl-aarch64.so.1\n"
    "#02 pc 0000000000255f20 /data/storage/el1/bundle/libs/arm64/libapp_BaiduVIlib.so\n"
)

DOC = {
    "meta_info": {"os_type": "harmonyos", "arch": "arm64"},
    "crash_info": {"signal": "11 (SIGSEGV)", "crash_reason": "segmentation fault"},
    "threads": [
        {
            "tid": "21840",
            "name": "main",
            "thread_index": None,
            "is_crash_thread": True,
            "is_main_thread": True,
            "frames": [
                {
                    "address": "0x10",
                    "function": "OHOS::Ace::CrashHere()",
                    "module": "libace_compatible.z.so",
                    "library_type": "system",
                }
            ],
        },
        {
            "tid": "worker-1",
            "name": "22985",
            "thread_index": 0,
            "is_crash_thread": False,
            "is_main_thread": False,
            "frames": [
                {
                    "address": "0x255f20",
                    "module": "libapp_BaiduVIlib.so",
                    "library_type": "app",
                }
            ],
        },
    ],
}


class TestAdd2lineMultiThread(unittest.TestCase):
    def test_resolved_threads_structure(self):
        lib_dir = tempfile.mkdtemp(prefix="add2line_mt_")
        lib_path = os.path.join(lib_dir, "libapp_BaiduVIlib.so")
        with open(lib_path, "wb"):
            pass
        try:
            raw = add2line_resolver(json.dumps(DOC, ensure_ascii=False), library_dir=lib_dir)
            out = json.loads(raw)
            self.assertNotIn("error", out)
            threads = resolved_threads_from_stack(out)
            mods = {
                f.get("module")
                for t in threads
                for f in (t.get("frames") or [])
            }
            self.assertIn("libapp_BaiduVIlib.so", mods)
            self.assertNotIn("libace_compatible.z.so", mods)
            self.assertNotIn("libmmi-client.z.so", mods)
            self.assertNotIn("resolved_frames", out)
            flat = flatten_resolved_frames_from_stack(out)
            flat_mods = {f.get("module") for f in flat}
            self.assertIn("libapp_BaiduVIlib.so", flat_mods)
            self.assertTrue(resolved_stack_has_usable_resolution(out))
            self.assertIsNotNone(out.get("frame_count_total"))
        finally:
            os.remove(lib_path)
            os.rmdir(lib_dir)

    def test_integration_with_harmony_parse(self):
        doc = {
            "platform": "Harmony",
            "attributes": {"exp_info": {"name": "SIGSEGV(11,1)"}},
            "body": {
                "attributed_stack": {
                    "thread_id": 21840,
                    "thread_name": "app",
                    "stack_frames": [
                        {
                            "frame_addr": "00000000001076a8",
                            "image": "/system/lib64/libmmi-client.z.so",
                            "local_symbol": "OHOS::RefBase::~RefBase()",
                            "type": "native",
                        }
                    ],
                },
                "stacks": [
                    {
                        "thread_id": "w1",
                        "thread_name": "22985",
                        "call_stack": HARMONY_PC,
                    }
                ],
            },
        }
        lib_dir = tempfile.mkdtemp(prefix="add2line_hy_")
        with open(os.path.join(lib_dir, "libapp_BaiduVIlib.so"), "wb"):
            pass
        try:
            sample = "crashDiagnosis: " + json.dumps(doc, ensure_ascii=False)
            one = json.loads(
                crash_log_parser(sample, options=CrashParseOptions(library_dir=lib_dir))
            )
            two = json.loads(add2line_resolver(json.dumps(one), library_dir=lib_dir))
            self.assertGreaterEqual(len(two.get("resolved_threads") or []), 1)
            self.assertEqual(two.get("crash_thread_id"), "21840")
            all_mods = {
                f.get("module")
                for t in (two.get("resolved_threads") or [])
                for f in (t.get("frames") or [])
            }
            self.assertNotIn("libace_compatible.z.so", all_mods)
        finally:
            for fn in os.listdir(lib_dir):
                os.remove(os.path.join(lib_dir, fn))
            os.rmdir(lib_dir)


if __name__ == "__main__":
    unittest.main()
