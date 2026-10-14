#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内联工程函数应成为崩溃修改对象。"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

from tools.code_content_provider_tool import CodeContentProvider


def _write_sources(root: Path) -> None:
    """写入 CancelTask 与外层调用者两个最小源文件。"""
    (root / "GridLayer.cpp").write_text(
        "void CGridLayer::CancelTask(bool wait) {\n"
        "    list_walk();\n"
        "}\n",
        encoding="utf-8",
    )
    (root / "VMapControl.cpp").write_text(
        "void CVMapControl::RemoveMapControl() {\n"
        "    layer->CancelTask(false);\n"
        "}\n",
        encoding="utf-8",
    )


class TestInlineProjectFrame(unittest.TestCase):
    """栈顶落在标准库头文件时，改用内联进工程源码的函数。"""

    def test_selector_prefers_inlined_project_function(self):
        """可定位的内联调用者优先于下一帧的外层函数。"""
        td = tempfile.mkdtemp()
        try:
            root = Path(td) / "src"
            root.mkdir()
            _write_sources(root)
            frames = [
                {
                    "address": "0x4926b4",
                    "resolved_function": (
                        "std::__ndk1::shared_ptr<CVTask>::operator->() const"
                    ),
                    "resolved_file": (
                        "/opt/ndk/toolchains/llvm/prebuilt/linux-x86_64/"
                        "sysroot/usr/include/c++/v1/shared_ptr.h"
                    ),
                    "resolved_line": 721,
                    "inline_frames": [
                        {
                            "function": "CGridLayer::CancelTask(bool)",
                            "file": "/root/ONLINE/src/GridLayer.cpp",
                            "line": 1,
                        }
                    ],
                },
                {
                    "address": "0x3e0738",
                    "resolved_function": "CVMapControl::RemoveMapControl()",
                    "resolved_file": str(root / "VMapControl.cpp"),
                    "resolved_line": 1,
                },
            ]
            provider = CodeContentProvider(code_parser_backend="regex")
            picked = provider._select_topmost_locatable_crash_frame(
                frames, [str(root)]
            )
            self.assertIsNotNone(picked)
            self.assertIn("CancelTask", picked.get("resolved_function", ""))
            self.assertIn("GridLayer.cpp", picked.get("resolved_file", ""))
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_project_frame_is_not_replaced_by_inline(self):
        """栈帧自己已在代码库中时，不改用更外层的内联函数。"""
        td = tempfile.mkdtemp()
        try:
            root = Path(td) / "src"
            root.mkdir()
            _write_sources(root)
            frames = [
                {
                    "address": "0x1",
                    "resolved_function": "CVMapControl::RemoveMapControl()",
                    "resolved_file": str(root / "VMapControl.cpp"),
                    "resolved_line": 1,
                    "inline_frames": [
                        {
                            "function": "CGridLayer::CancelTask(bool)",
                            "file": "/root/ONLINE/src/GridLayer.cpp",
                            "line": 1,
                        }
                    ],
                }
            ]
            provider = CodeContentProvider(code_parser_backend="regex")
            picked = provider._select_topmost_locatable_crash_frame(
                frames, [str(root)]
            )
            self.assertIsNotNone(picked)
            self.assertIn("RemoveMapControl", picked.get("resolved_function", ""))
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_code_context_crash_function_is_inlined_callee(self):
        """过滤栈帧后，崩溃函数是内联的 CancelTask，不是外层 RemoveMapControl。"""
        td = tempfile.mkdtemp()
        try:
            root = Path(td) / "src"
            root.mkdir()
            _write_sources(root)
            add2line = {
                "os_type": "android",
                "success_count": 2,
                "total_count": 2,
                "resolved_threads": [
                    {
                        "thread_id": "1",
                        "thread_name": "map-loaddata",
                        "is_crash_thread": True,
                        "frames": [
                            {
                                "address": "0x4926b4",
                                "module": "libmap.so",
                                "resolved_function": (
                                    "std::__ndk1::shared_ptr<CVTask>::operator->() const"
                                ),
                                "resolved_file": (
                                    "/opt/ndk/toolchains/llvm/prebuilt/linux-x86_64/"
                                    "sysroot/usr/include/c++/v1/shared_ptr.h"
                                ),
                                "resolved_line": 721,
                                "inline_frames": [
                                    {
                                        "function": "CGridLayer::CancelTask(bool)",
                                        "file": "/root/ONLINE/src/GridLayer.cpp",
                                        "line": 80,
                                    }
                                ],
                            },
                            {
                                "address": "0x3e0738",
                                "module": "libmap.so",
                                "resolved_function": "CVMapControl::RemoveMapControl()",
                                "resolved_file": "/root/ONLINE/src/VMapControl.cpp",
                                "resolved_line": 2183,
                            },
                        ],
                    }
                ],
            }
            provider = CodeContentProvider(
                code_parser_backend="regex",
                max_static_call_chain_depth=1,
                max_direct_callers=1,
                max_shared_var_related_functions=1,
                max_crash_caller_search_files=2,
                code_context_timeout_sec=30,
                use_ctags_index=False,
            )
            out = json.loads(
                provider.code_content_provider(json.dumps(add2line), str(root))
            )
            self.assertNotIn("error", out, out.get("error"))
            nodes = (out.get("graph") or {}).get("nodes") or []
            functions = [node for node in nodes if node.get("type") == "function"]
            self.assertTrue(functions, msg=list(out.keys()))
            crash = functions[0]
            signature = crash.get("signature") or crash.get("name") or ""
            self.assertIn("CancelTask", signature)
            self.assertNotIn("RemoveMapControl", signature)
            self.assertIn("GridLayer.cpp", crash.get("file") or "")
            self.assertIn("list_walk", "\n".join(crash.get("snippet") or []))
        finally:
            shutil.rmtree(td, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
