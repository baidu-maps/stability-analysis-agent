#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""code_fixer 写回时保留行尾与缩进风格的单测。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from services.code_fixer import (
    CodeFixer,
    _adapt_replacement_indent,
    _align_leading_newline,
    _ensure_newline_before_replacement,
    _read_text_raw,
    _source_newline_style,
    _try_read_source_raw,
    _write_text_raw,
)


class CodeFixerStylePreserveTests(unittest.TestCase):
    """行尾 / 缩进 / 注释粘连。"""

    def test_raw_read_write_preserves_crlf(self) -> None:
        """read/write 使用 newline=''，CRLF 文件不应被规范化成 LF。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.cpp"
            raw = "int a;\r\nint b;\r\n"
            path.write_bytes(raw.encode("utf-8"))
            text, enc = _try_read_source_raw(path)
            self.assertEqual(enc, "utf-8")
            self.assertEqual(_source_newline_style(text), "\r\n")
            self.assertIn("\r\n", text)
            _write_text_raw(path, text.replace("int b;", "int c;"), encoding="utf-8")
            written = path.read_bytes()
            self.assertIn(b"\r\n", written)
            self.assertNotIn(b"int b;", written)
            # 不应出现被 Python text mode 二次转换后的混用异常；整文件仍是 CRLF
            self.assertEqual(written.count(b"\r\n"), 2)

    def test_adapt_spaces_to_tabs(self) -> None:
        """原函数用 Tab 缩进时，LLM 的 4 空格应折成 Tab。"""
        old = "VVoid Foo::Bar()\n{\n\tint x = 1;\n\tif (x) {\n\t\tx++;\n\t}\n}\n"
        repl = (
            "VVoid Foo::Bar()\n"
            "{\n"
            "    int x = 1;\n"
            "    if (x) {\n"
            "        x++;\n"
            "    }\n"
            "}\n"
        )
        adapted = _adapt_replacement_indent(repl, old)
        self.assertIn("\tint x = 1;", adapted)
        self.assertIn("\t\tx++;", adapted)
        self.assertNotIn("    int x", adapted)

    def test_ensure_newline_after_block_comment(self) -> None:
        """`*/` 后紧接签名时应插入换行。"""
        prefix = "\t/** 参见类定义*/"
        repl = "VVoid CNaviEngineControl::HandleRemoveRouteMessage()\n{\n}\n"
        out = _ensure_newline_before_replacement(prefix, repl)
        self.assertTrue(out.startswith("\nVVoid"))
        glued = prefix + out
        self.assertIn("*/\nVVoid", glued)
        self.assertNotIn("*/VVoid", glued)

    def test_align_leading_newline_prevents_comment_glue(self) -> None:
        """old_block 含前导换行而补丁没有时，补上换行。"""
        old = "\nVVoid Foo::Bar()\n{\n}\n"
        repl = "VVoid Foo::Bar()\n{\n\treturn;\n}\n"
        out = _align_leading_newline(old, repl)
        self.assertTrue(out.startswith("\nVVoid"))

    def test_replace_function_block_keeps_comment_on_own_line(self) -> None:
        """替换时不把上一行 `*/` 与新签名挤到同一行。"""
        source = (
            "class C {\n"
            "/** 参见类定义*/\n"
            "VVoid C::HandleRemoveRouteMessage()\n"
            "{\n"
            "\tm_pclCurRoute = V_NULL;\n"
            "}\n"
            "};\n"
        )
        # 模拟「签名起点被算到注释后、且替换点紧贴 */」的粘连场景：
        # 把注释与签名写在同一逻辑位置前的 prefix 上。
        mashed_source = (
            "class C {\n"
            "/** 参见类定义*/VVoid C::HandleRemoveRouteMessage()\n"
            "{\n"
            "\tm_pclCurRoute = V_NULL;\n"
            "}\n"
            "};\n"
        )
        replacement = (
            "VVoid C::HandleRemoveRouteMessage()\n"
            "{\n"
            "\tm_clRouteInfoMutex.Lock(V_INFINITY_WAIT);\n"
            "\tm_pclCurRoute = V_NULL;\n"
            "\tm_clRouteInfoMutex.Unlock();\n"
            "}\n"
        )
        new_src, old = CodeFixer.replace_function_block(
            mashed_source,
            "VVoid C::HandleRemoveRouteMessage()",
            replacement,
        )
        self.assertIsNotNone(old)
        self.assertIn("*/\nVVoid C::HandleRemoveRouteMessage()", new_src)
        self.assertNotIn("*/VVoid", new_src)
        # 正常分行源也仍可替换
        new2, _ = CodeFixer.replace_function_block(
            source,
            "VVoid C::HandleRemoveRouteMessage()",
            replacement,
        )
        self.assertIn("HandleRemoveRouteMessage", new2)
        self.assertIn("m_clRouteInfoMutex", new2)

    def test_read_text_raw_roundtrip_bytes(self) -> None:
        """_read_text_raw 与 Path.read_text 在 CRLF 上行为不同。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.cpp"
            path.write_bytes(b"a;\r\nb;\r\n")
            self.assertNotIn("\r\n", path.read_text(encoding="utf-8"))
            self.assertIn("\r\n", _read_text_raw(path, encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
