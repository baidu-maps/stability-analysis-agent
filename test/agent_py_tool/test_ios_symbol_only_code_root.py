#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""iOS 已符号化堆栈（无 file:line）经 code_root 定位源码的链路测试。"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from tools.add2line_resolver_tool import add2line_resolver
from tools.code_content_provider_tool import CodeContentProvider
from tools.crash_log_parser_tool import crash_log_parser

SAMPLE = """\
* SIGSEGV: 0x000000024c911b04 A28FFCAD-5B21-3D66-91E4-C7573948C36A + 9874169856
* DemoApp CrashFunc(int)
0 0 DemoApp 0x0000000100010000 CrashFunc(int)
1 1 DemoApp 0x0000000100010100 Caller()
"""


class TestIosSymbolOnlyCodeRoot(unittest.TestCase):
    def test_symbol_only_stack_enriched_with_code_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            src_dir = os.path.join(tmp, "src")
            os.makedirs(src_dir)
            src_file = os.path.join(src_dir, "crash.cpp")
            with open(src_file, "w", encoding="utf-8") as f:
                f.write(
                    "void CrashFunc(int x) {\n"
                    "  volatile int* p = nullptr;\n"
                    "  *p = x;\n"
                    "}\n"
                    "void Caller() {\n"
                    "  CrashFunc(1);\n"
                    "}\n"
                )

            parsed = json.loads(crash_log_parser(SAMPLE))
            resolved = json.loads(add2line_resolver(json.dumps(parsed), library_dir=None))
            self.assertEqual(resolved.get("resolution_source"), "log_symbolicated_passthrough")
            from tools.resolve_stack_errors import flatten_resolved_frames_from_stack

            top = flatten_resolved_frames_from_stack(resolved)[0]
            self.assertIn("CrashFunc", top.get("resolved_function") or "")

            provider = CodeContentProvider(max_symbol_only_rescues=4)
            out = provider.code_content_provider(json.dumps(resolved), code_root=tmp)
            data = json.loads(out)
            summary = data.get("crash_summary") or {}
            self.assertIn("crash.cpp", str(summary.get("node_id") or ""))
            self.assertGreater(int(summary.get("crash_line_number") or 0), 0)
            self.assertEqual(summary.get("crash_location_source"), "from_log_deduce")


if __name__ == "__main__":
    unittest.main()
