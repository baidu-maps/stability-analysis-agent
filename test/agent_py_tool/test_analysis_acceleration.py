#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for analysis acceleration services."""

import os
import tempfile
import unittest
from unittest.mock import patch

from services.code_locator import CodeLocatorService, LocatorConfig
from services.code_index_service import CodeIndexService, get_code_index_for_roots
from services.ctags_function_index import CtagsFunctionIndex


class TestCodeIndexService(unittest.TestCase):
    def test_filename_lookup_after_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src")
            os.makedirs(src)
            path = os.path.join(src, "VMapControl.cpp")
            with open(path, "w", encoding="utf-8") as f:
                f.write("void foo() {}\n")
            service = CodeIndexService(tmp)
            self.assertTrue(service.wait_ready(timeout=5.0))
            hits = service.lookup("VMapControl.cpp")
            self.assertIn(os.path.abspath(path), [os.path.abspath(p) for p in hits])

    def test_multi_root_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = os.path.join(tmp, "a")
            os.makedirs(a)
            with open(os.path.join(a, "a.cpp"), "w", encoding="utf-8") as f:
                f.write("int main(){return 0;}\n")
            idx1 = get_code_index_for_roots([a])
            idx2 = get_code_index_for_roots([a])
            self.assertIs(idx1, idx2)


class TestCtagsFunctionIndex(unittest.TestCase):
    def test_parse_tags_and_lookup(self):
        sample = "\n".join(
            [
                "!_TAG_FILE_FORMAT\t2\t/extended format/",
                "ReleaseAllLayers\t/tmp/VMapControl.cpp\t/^VVoid CVMapControl::ReleaseAllLayers()/;\"\tf\tline:774",
                "main\t/tmp/main.cpp\t/^int main()/;\"\tf\tline:10",
            ]
        )
        index = CtagsFunctionIndex([])
        index._parse_tags_stream(sample.splitlines())
        index._ready = True
        hit = index.lookup("ReleaseAllLayers", ["/tmp"])
        self.assertIsNotNone(hit)
        self.assertEqual(hit[1], 774)

    def test_parse_bsd_ctags_format(self):
        with tempfile.NamedTemporaryFile("w", suffix=".cpp", delete=False) as tmp:
            tmp.write("VVoid CVMapControl::ReleaseAllLayers() {}\n")
            tmp_path = tmp.name
        try:
            sample = f"ReleaseAllLayers\t{tmp_path}\t/^VVoid CVMapControl::ReleaseAllLayers()/"
            index = CtagsFunctionIndex([])
            index._parse_tags_stream([sample])
            index._ready = True
            hit = index.lookup("ReleaseAllLayers", [os.path.dirname(tmp_path)])
            self.assertIsNotNone(hit)
            self.assertEqual(hit[0], os.path.abspath(tmp_path))
        finally:
            os.remove(tmp_path)


class TestCodeLocatorService(unittest.TestCase):
    def test_rg_empty_function_definition_does_not_walk_repo(self):
        locator = CodeLocatorService(LocatorConfig())
        locator.ctx.rg_grep_lines = lambda *args, **kwargs: []
        with patch("services.code_locator.os.walk", side_effect=AssertionError("unexpected walk")):
            self.assertIsNone(locator.find_function_definition("missingFunction", ["/tmp/nonexistent"]))


if __name__ == "__main__":
    unittest.main()
