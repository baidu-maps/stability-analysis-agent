#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""已符号化精简 iOS 崩溃日志（去哪儿 Crash.txt 格式）解析测试。"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from tools.crash_log_parser_tool import (
    _detect_ios_pre_parsed_symbolized_crash,
    _try_parse_ios_pre_parsed_stack_line,
    crash_log_parser,
)

SAMPLE = """\
* SIGSEGV: 0x000000024c911b04 A28FFCAD-5B21-3D66-91E4-C7573948C36A + 9874169856
* QunariPhone_Cook_CM _baidu_vi::MTLVertexBuffer::createPrivateBuffer(id, id, id)
0 0 QunariPhone_Cook_CM 0x00000001044be644 -[CKCrashReporter recordCrashWithSignal:]
1 1 QunariPhone_Cook_CM 0x00000001044c1648 SignalHandler
2 2 libsystem_platform.dylib 0x00000002146832f4 D02CA3B2-2150-3548-A54A-7B7CADE7FC39 + 8932298752
5 5 QunariPhone_Cook_CM 0x0000000102cbd188 _baidu_vi::MTLVertexBuffer::createPrivateBuffer(id, id, id)
24 24 CoreFoundation 0x00000001c99e5d20 CFRunLoopRunSpecific + 7677067264
29 29 libsystem_pthread.dylib 0x000000021471a72c thread_start + 8932921344
"""


class TestIosPreParsedCrash(unittest.TestCase):
    def test_detect_format(self):
        self.assertTrue(_detect_ios_pre_parsed_symbolized_crash(SAMPLE))

    def test_parse_stack_line(self):
        parsed = _try_parse_ios_pre_parsed_stack_line(
            "5 5 QunariPhone_Cook_CM 0x0000000102cbd188 "
            "_baidu_vi::MTLVertexBuffer::createPrivateBuffer(id, id, id)"
        )
        self.assertIsNotNone(parsed)
        module, addr, func, offset, frame_num = parsed
        self.assertEqual(frame_num, 5)
        self.assertEqual(module, "QunariPhone_Cook_CM")
        self.assertEqual(addr, "0x0000000102cbd188")
        self.assertIn("MTLVertexBuffer::createPrivateBuffer", func)
        self.assertEqual(offset, "0")

    def test_parse_uuid_tail_line(self):
        parsed = _try_parse_ios_pre_parsed_stack_line(
            "2 2 libsystem_platform.dylib 0x00000002146832f4 "
            "D02CA3B2-2150-3548-A54A-7B7CADE7FC39 + 8932298752"
        )
        self.assertIsNotNone(parsed)
        module, addr, func, offset, _ = parsed
        self.assertEqual(module, "libsystem_platform.dylib")
        self.assertEqual(addr, "0x00000002146832f4")
        self.assertEqual(func, "")
        self.assertEqual(offset, "8932298752")

    def test_full_parse(self):
        result = json.loads(crash_log_parser(SAMPLE))
        self.assertEqual(result["meta_info"]["os_type"], "ios")
        self.assertEqual(result["meta_info"]["log_format"], "ios_pre_parsed_symbolized")
        self.assertEqual(result["meta_info"]["process_name"], "QunariPhone_Cook_CM")
        self.assertEqual(result["crash_info"]["signal"], "SIGSEGV")
        self.assertEqual(result["crash_info"]["crash_address"], "0x000000024c911b04")
        frames = result["threads"][0]["frames"]
        self.assertGreaterEqual(len(frames), 6)
        crash_frame = next(
            f for f in frames if "MTLVertexBuffer::createPrivateBuffer" in (f.get("function") or "")
        )
        self.assertEqual(crash_frame["module"], "QunariPhone_Cook_CM")
        self.assertEqual(crash_frame["address"], "0x0000000102cbd188")
        self.assertGreaterEqual(crash_frame["frame_number"], 0)


if __name__ == "__main__":
    unittest.main()
