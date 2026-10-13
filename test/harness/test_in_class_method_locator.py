#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""类体内联方法不写 Class::method 时，仍能按限定符号定位。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from services.code_locator import LocatorConfig, LocatorContext, SymbolLocator
from services.context_source_resolver import CodeContextRequestResolver


HEADER = """\
class Other {
    void helper() {
        GetRunLoopQueue();
    }
    inline void GetRunLoopQueue(){ }
};

class CVMapSchedule {
    inline CVRunLoopQueue* GetRunLoopQueue(){return m_runlooperQueue;}
};
"""


class InClassMethodLocatorTests(unittest.TestCase):
    """内联成员与类外定义的定位。"""

    def test_qualified_symbol_finds_inline_method_inside_class(self) -> None:
        """CVMapSchedule::GetRunLoopQueue 命中类体内联定义，而不是其它类的同名方法。"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            header = root / "MapSchedule.h"
            header.write_text(HEADER, encoding="utf-8")
            locator = SymbolLocator(LocatorContext(LocatorConfig()))
            found = locator.find_function_definition_location_for_symbol(
                "CVMapSchedule::GetRunLoopQueue",
                [str(root)],
            )
            self.assertIsNotNone(found)
            assert found is not None
            self.assertEqual(Path(found[0]).name, "MapSchedule.h")
            line = header.read_text(encoding="utf-8").splitlines()[found[1] - 1]
            self.assertIn("return m_runlooperQueue", line)
            self.assertTrue(
                CodeContextRequestResolver._resolved_function_matches_symbol(
                    "CVMapSchedule::GetRunLoopQueue",
                    found[0],
                    found[1],
                    line,
                    line,
                )
            )
            self.assertTrue(
                CodeContextRequestResolver._resolved_snippet_matches_symbol(
                    "CVMapSchedule::GetRunLoopQueue",
                    line,
                    line,
                )
            )

    def test_out_of_line_definition_still_preferred(self) -> None:
        """存在 Class::method 定义时，不改走类体内联搜索。"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "MapSchedule.cpp"
            source.write_text(
                "CVRunLoopQueue* CVMapSchedule::GetRunLoopQueue() {\n"
                "    return m_runlooperQueue;\n"
                "}\n",
                encoding="utf-8",
            )
            locator = SymbolLocator(LocatorContext(LocatorConfig()))
            found = locator.find_function_definition_location_for_symbol(
                "CVMapSchedule::GetRunLoopQueue",
                [str(root)],
            )
            self.assertIsNotNone(found)
            assert found is not None
            self.assertEqual(Path(found[0]).name, "MapSchedule.cpp")
            self.assertEqual(found[1], 1)


if __name__ == "__main__":
    unittest.main()
