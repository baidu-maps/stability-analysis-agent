#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""已符号化 iOS 日志在无 library-dir 时的 02 回填测试。"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from tools.add2line_resolver_tool import add2line_resolver
from tools.crash_log_parser_tool import crash_log_parser
from tools.resolve_stack_errors import flatten_resolved_frames_from_stack

SAMPLE = """\
* SIGSEGV: 0x000000024c911b04 A28FFCAD-5B21-3D66-91E4-C7573948C36A + 9874169856
* QunariPhone_Cook_CM _baidu_vi::MTLVertexBuffer::createPrivateBuffer(id, id, id)
0 0 QunariPhone_Cook_CM 0x00000001044be644 -[CKCrashReporter recordCrashWithSignal:]
3 3 AGXMetalA11 0x000000024c911b04 A28FFCAD-5B21-3D66-91E4-C7573948C36A + 9874169856
5 5 QunariPhone_Cook_CM 0x0000000102cbd188 _baidu_vi::MTLVertexBuffer::createPrivateBuffer(id, id, id)
"""


class TestAdd2linePassthrough(unittest.TestCase):
    def test_filters_empty_symbol_and_objc_frames(self):
        parsed = json.loads(crash_log_parser(SAMPLE))
        result = json.loads(add2line_resolver(json.dumps(parsed), library_dir=None))
        self.assertEqual(result.get("resolution_source"), "log_symbolicated_passthrough")
        flat = flatten_resolved_frames_from_stack(result)
        addrs = [f.get("address") for f in flat]
        self.assertNotIn("0x000000024c911b04", addrs)
        symbols = [
            (f.get("resolved_function") or f.get("function") or "")
            for f in flat
        ]
        self.assertNotIn("-[CKCrashReporter recordCrashWithSignal:]", symbols)
        self.assertIn(
            "_baidu_vi::MTLVertexBuffer::createPrivateBuffer(id, id, id)",
            symbols,
        )


if __name__ == "__main__":
    unittest.main()
