#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import unittest
from unittest.mock import patch
from pathlib import Path

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

from tools._stack_symbol_utils import (
    is_cpp_native_stack_symbol,
    is_objc_stack_symbol,
    looks_like_cpp_qualified_stack,
    looks_like_rtf,
    rtf_to_plain_text,
    sanitize_stack_symbol,
    should_keep_frame_for_cpp_stack_output,
)
from tools.code_content_provider_tool import CodeContentProvider


class TestStackSymbolUtils(unittest.TestCase):
    def test_sanitize_rtf_trailing_backslash(self):
        self.assertEqual(
            sanitize_stack_symbol("walk_navi::CRoute::GetLegSize\\"),
            "walk_navi::CRoute::GetLegSize",
        )
        self.assertEqual(sanitize_stack_symbol("_pthread_start}"), "_pthread_start")

    def test_is_cpp_native_symbol_without_parens(self):
        self.assertTrue(
            is_cpp_native_stack_symbol("walk_navi::CRoute::GetLegSize")
        )
        self.assertTrue(
            is_cpp_native_stack_symbol("walk_navi::CRoute::GetLegSize\\")
        )
        self.assertFalse(is_cpp_native_stack_symbol("-[Foo bar:]"))
        self.assertTrue(is_objc_stack_symbol("-[Foo bar:]"))

    def test_should_keep_frame_for_cpp_stack_output(self):
        self.assertFalse(
            should_keep_frame_for_cpp_stack_output(
                {"address": "0x1", "resolved_function": "-[CKCrashReporter record:]"}
            )
        )
        self.assertTrue(
            should_keep_frame_for_cpp_stack_output(
                {
                    "address": "0x2",
                    "resolved_function": "_baidu_vi::MTLVertexBuffer::createPrivateBuffer",
                }
            )
        )
        self.assertFalse(
            should_keep_frame_for_cpp_stack_output(
                {"address": "0xdead", "function": "", "module": "AGXMetalA11"}
            )
        )

    def test_extract_cpp_qualified_parts_without_parens(self):
        p = CodeContentProvider()
        self.assertEqual(
            p._extract_cpp_qualified_parts("walk_navi::CRoute::GetLegSize"),
            ("walk_navi::CRoute", "GetLegSize"),
        )

    def test_looks_like_cpp_qualified_stack(self):
        sample = """
        Thread 0 Crashed:
        0 10ae3b7c8 walk_navi::CRoute::GetLegSize + 0
        1 10b170ecc walk_navi::CPanoramaRouteDataFactory::HandleDataFail + 0
        """
        self.assertTrue(looks_like_cpp_qualified_stack(sample))

    def test_rtf_to_plain_text(self):
        rtf = r"{\rtf1\ansi walk_navi::CRoute::GetLegSize\par null pointer dereference\par}"
        self.assertTrue(looks_like_rtf(rtf))
        plain = rtf_to_plain_text(rtf)
        self.assertIn("walk_navi::CRoute::GetLegSize", plain)
        self.assertIn("null pointer dereference", plain)


class TestTopmostLocatableCrashFrame(unittest.TestCase):
    def test_skips_objc_and_picks_cpp_below(self):
        import shutil
        import tempfile

        td = tempfile.mkdtemp()
        try:
            root = Path(td) / "src"
            root.mkdir()
            (root / "buf.mm").write_text(
                "namespace _baidu_vi {\n"
                "void MTLVertexBuffer::createPrivateBuffer() {}\n"
                "}\n",
                encoding="utf-8",
            )
            frames = [
                {
                    "address": "0x1",
                    "resolved_function": "-[Reporter record:]",
                },
                {
                    "address": "0x2",
                    "resolved_function": "_baidu_vi::MTLVertexBuffer::createPrivateBuffer",
                },
            ]
            p = CodeContentProvider(code_parser_backend="regex")
            picked = p._select_topmost_locatable_crash_frame(frames, [str(root)])
            self.assertIsNotNone(picked)
            self.assertIn("MTLVertexBuffer", picked.get("resolved_function", ""))
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_cpp_qualified_rg_empty_does_not_walk_repo(self):
        p = CodeContentProvider(code_parser_backend="regex")
        p._rg_grep_lines = lambda *args, **kwargs: []
        with patch("tools.code_content_provider_tool.os.walk", side_effect=AssertionError("unexpected walk")):
            self.assertIsNone(
                p._find_cpp_qualified_definition_location(
                    "_baidu_vi::MissingClass::missingMethod()", ["/tmp/nonexistent"]
                )
            )

    def test_standard_library_symbol_skips_symbol_only_locate(self):
        p = CodeContentProvider(code_parser_backend="regex")
        p._rg_grep_lines = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("stdlib symbols should not run rg")
        )
        self.assertIsNone(
            p._find_cpp_qualified_definition_location(
                "std::__1::__shared_ptr_emplace<Foo>::__shared_ptr_emplace()", ["/tmp/nonexistent"]
            )
        )


class TestSymbolOnlyPassthrough(unittest.TestCase):
    def test_symbol_only_cpp_qualified_locates_source(self):
        import json
        import shutil
        import tempfile

        td = tempfile.mkdtemp()
        try:
            root = Path(td) / "src"
            root.mkdir()
            (root / "route.cpp").write_text(
                "namespace walk_navi {\n"
                "int CRoute::GetLegSize() { return 0; }\n"
                "void CPanoramaRouteDataFactory::HandleDataFail() {}\n"
                "}\n",
                encoding="utf-8",
            )
            add2line = json.dumps(
                {
                    "resolved_frames": [
                        {
                            "address": "10ae3b7c8",
                            "resolved_function": "walk_navi::CRoute::GetLegSize\\",
                            "resolved_file": None,
                            "resolved_line": None,
                        }
                    ],
                    "os_type": "ios",
                    "success_count": 1,
                    "total_count": 1,
                },
                ensure_ascii=False,
            )
            p = CodeContentProvider(code_parser_backend="regex")
            out = json.loads(p.code_content_provider(add2line, str(root)))
            self.assertNotIn("error", out, out.get("error"))
            self.assertIn("crash_summary", out)
            self.assertIn("route.cpp", out["crash_summary"].get("node_id", ""))
        finally:
            shutil.rmtree(td, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
