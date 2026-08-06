#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HarmonyOS 堆栈地址解析工具优先级测试。"""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from tools.add2line_resolver_tool import Add2lineResolver


def _make_executable(path: Path, marker: str) -> None:
    path.write_text(
        f"#!/bin/sh\n"
        f"if [ \"$1\" = \"--version\" ]; then\n"
        f"  echo {marker}\n"
        f"fi\n"
        f"exit 0\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class TestHarmonyOsToolPriority(unittest.TestCase):
    def test_harmonyos_skips_android_ndk_path(self):
        with tempfile.TemporaryDirectory(prefix="harmony_tool_priority_") as td:
            root = Path(td)
            android_bin = root / "android" / "ndk" / "bin"
            harmony_bin = root / "OpenHarmony" / "Sdk" / "native" / "llvm" / "bin"
            android_bin.mkdir(parents=True)
            harmony_bin.mkdir(parents=True)

            _make_executable(android_bin / "llvm-addr2line", "android")
            _make_executable(harmony_bin / "llvm-addr2line", "harmony")

            with patch.object(Add2lineResolver, "_load_config_file", return_value={}), patch.object(
                Add2lineResolver, "_load_environment_variables", lambda self: None
            ), patch.object(Add2lineResolver, "_show_environment_info", lambda self: None):
                resolver = Add2lineResolver()

            resolver.os_type = "harmonyos"
            picked = resolver._find_tool_in_paths(
                "llvm-addr2line",
                [str(android_bin), str(harmony_bin)],
            )

            self.assertEqual(picked, str(harmony_bin / "llvm-addr2line"))


if __name__ == "__main__":
    unittest.main()
