#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.repo_search import (
    merge_repo_search_context,
    normalize_code_roots,
    path_under_code_roots,
    render_repo_search_context,
)
from tools.repo_search_tool import RepoSearchTool


class TestRepoSearch(unittest.TestCase):
    def test_normalize_and_path_guard(self):
        with tempfile.TemporaryDirectory() as td:
            roots = normalize_code_roots([td])
            self.assertEqual(len(roots), 1)
            f = Path(td) / "a.cpp"
            f.write_text("void foo() {}\n", encoding="utf-8")
            self.assertTrue(path_under_code_roots(str(f.resolve()), roots))
            self.assertFalse(path_under_code_roots("/etc/passwd", roots))

    def test_read_file_mode(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "sample.cpp"
            f.write_text("line1\nline2\nline3\n", encoding="utf-8")
            tool = RepoSearchTool()
            out = tool.execute(
                {
                    "code_roots": [td],
                    "mode": "read_file",
                    "file_path": "sample.cpp",
                    "line_start": 2,
                    "line_end": 2,
                }
            )
            self.assertTrue(out.get("success"))
            self.assertIn("line2", out.get("content", ""))

    def test_render_and_merge_context(self):
        block = render_repo_search_context(
            {
                "success": True,
                "mode": "grep",
                "query": "foo",
                "matches": [{"file": "/x/a.cpp", "line": 1, "line_text": "int foo();"}],
            }
        )
        self.assertIn("repo_search", block)
        merged = merge_repo_search_context([block])
        self.assertIn("仓库检索补充", merged)


if __name__ == "__main__":
    unittest.main()
