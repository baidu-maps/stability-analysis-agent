#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows 平台的日志识别与符号化工具选择测试。"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from tools.add2line_resolver_tool import Add2lineResolver
from tools.crash_parser.format_detect import detect_os_type
from tools.crash_log_parser_tool import crash_log_parser
from tools.crash_parser.stack_lines import _try_parse_windows_stack_line


class TestWindowsCompatibility(unittest.TestCase):
    def test_detects_windows_crash_text(self):
        content = """\
Exception Code:    0xc0000005
Faulting Module:  demo.exe
Call Stack:
    demo.exe+0x1234
"""
        self.assertEqual(detect_os_type(content), "windows")

    def test_windows_prefers_llvm_addr2line(self):
        resolver = object.__new__(Add2lineResolver)
        resolver.os_type = "windows"
        resolver.resolver_tools = {
            "addr2line": "C:/LLVM/bin/addr2line.exe",
            "llvm-addr2line": "C:/LLVM/bin/llvm-addr2line.exe",
        }
        self.assertEqual(resolver._select_primary_tool(), "llvm-addr2line")

    def test_tool_lookup_uses_python_path_lookup(self):
        resolver = object.__new__(Add2lineResolver)
        resolver.os_type = "windows"
        resolver._protected_search_paths = set()
        resolver._is_android_affinity_path = lambda _path: False
        resolver._test_tool_availability = lambda _name, _path: True

        with patch(
            "tools.add2line_resolver_tool.shutil.which",
            return_value=r"C:\LLVM\bin\llvm-addr2line.exe",
        ) as which:
            result = resolver._find_tool_in_paths("llvm-addr2line", [])

        self.assertEqual(result, r"C:\LLVM\bin\llvm-addr2line.exe")
        which.assert_called_once_with("llvm-addr2line")

    def test_tool_lookup_finds_windows_executable_in_search_directory(self):
        resolver = object.__new__(Add2lineResolver)
        resolver.os_type = "windows"
        resolver._protected_search_paths = set()
        resolver._is_android_affinity_path = lambda _path: False
        resolver._test_tool_availability = lambda _name, _path: True

        with tempfile.TemporaryDirectory() as temp_dir:
            executable = Path(temp_dir) / "llvm-addr2line.exe"
            executable.write_bytes(b"windows test executable")
            with patch("tools.add2line_resolver_tool.shutil.which", return_value=None):
                result = resolver._find_tool_in_paths("llvm-addr2line", [temp_dir])

        self.assertEqual(result, str(executable))

    def test_parses_windows_symbolized_stack_and_metadata(self):
        content = """\
Exception Code:    0xc0000005
Faulting Module:  demo.exe
Call Stack:
  0 0x00007ff612341234 demo.exe!CrashDemo::run+0x2a
  1 0x00007ff612341100 demo.exe!main+0x10
"""
        parsed = __import__("json").loads(crash_log_parser(content))
        self.assertEqual(parsed["meta_info"]["os_type"], "windows")
        self.assertEqual(parsed["crash_info"]["exception_type"], "0XC0000005")
        self.assertEqual(parsed["crash_info"]["category"], "native_crash")
        self.assertEqual(parsed["threads"][0]["frames"][0]["module"], "demo.exe")
        self.assertEqual(parsed["threads"][0]["frames"][0]["function"], "CrashDemo::run")

    def test_windows_stack_parser_accepts_windbg_without_pc(self):
        parsed = _try_parse_windows_stack_line("demo.dll!CrashDemo::run+42")
        self.assertEqual(parsed, ("demo.dll", "", "CrashDemo::run", "42", None))

    def test_windows_symbolizer_uses_pe_object_and_parses_windows_path(self):
        resolver = object.__new__(Add2lineResolver)
        resolver.os_type = "windows"
        with patch(
            "tools.add2line_resolver_tool.subprocess.run",
            return_value=type(
                "Result",
                (),
                {"returncode": 0, "stdout": "CrashDemo::run\nC:\\\\src\\\\demo.cpp:42:7\n", "stderr": ""},
            )(),
        ) as run:
            result = resolver._resolve_with_llvm_symbolizer(
                "0x140001234", r"C:\\symbols\\demo.exe", r"C:\\LLVM\\bin\\llvm-symbolizer.exe"
            )
        self.assertEqual(result.resolved_function, "CrashDemo::run")
        self.assertEqual(result.resolved_file, r"C:\\src\\demo.cpp")
        self.assertEqual(result.resolved_line, 42)
        self.assertEqual(
            run.call_args.args[0],
            [
                r"C:\\LLVM\\bin\\llvm-symbolizer.exe",
                r"--obj=C:\\symbols\\demo.exe",
                "--demangle",
                "0x140001234",
            ],
        )


if __name__ == "__main__":
    unittest.main()
