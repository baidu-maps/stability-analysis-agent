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
            path = os.path.join(src, "DemoMapControl.cpp")
            with open(path, "w", encoding="utf-8") as f:
                f.write("void foo() {}\n")
            service = CodeIndexService(tmp)
            self.assertTrue(service.wait_ready(timeout=5.0))
            hits = service.lookup("DemoMapControl.cpp")
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
                "ReleaseAllLayers\t/tmp/DemoMapControl.cpp\t/^void DemoMapControl::ReleaseAllLayers()/;\"\tf\tline:774",
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
            tmp.write("void DemoMapControl::ReleaseAllLayers() {}\n")
            tmp_path = tmp.name
        try:
            sample = f"ReleaseAllLayers\t{tmp_path}\t/^void DemoMapControl::ReleaseAllLayers()/"
            index = CtagsFunctionIndex([])
            index._parse_tags_stream([sample])
            index._ready = True
            hit = index.lookup("ReleaseAllLayers", [os.path.dirname(tmp_path)])
            self.assertIsNotNone(hit)
            self.assertEqual(hit[0], os.path.abspath(tmp_path))
        finally:
            os.remove(tmp_path)

    def test_cache_from_other_worktree_rebases_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_root = os.path.join(tmp, "old_worktree")
            new_root = os.path.join(tmp, "new_worktree")
            os.makedirs(os.path.join(new_root, "src"))
            new_file = os.path.join(new_root, "src", "Demo.cpp")
            with open(new_file, "w", encoding="utf-8") as f:
                f.write("void Demo::Run() {}\n")
            index = CtagsFunctionIndex([])
            index.code_roots = [new_root]
            index._path_rebase = index._build_path_rebase({"roots": [old_root]})
            old_file = os.path.join(old_root, "src", "Demo.cpp")
            index._parse_tags_stream([f"Run\t{old_file}\t/^void Demo::Run()/;\"\tf\tline:1"])
            hit = index.lookup("Run", [new_root])
            self.assertEqual(hit, (new_file, 1))

    def test_unmappable_multi_root_cache_forces_rebuild(self):
        index = CtagsFunctionIndex([])
        index.code_roots = ["/tmp/a", "/tmp/b"]
        self.assertIsNone(index._build_path_rebase({"roots": ["/old/a", "/old/b"]}))


class TestFixRecipe(unittest.TestCase):
    def test_exclusive_recipe_locates_file_under_root(self):
        from knowledge_plane.fix_recipe import collect_recipe_edits

        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "repo")
            os.makedirs(os.path.join(root, "inc"))
            header = os.path.join(root, "inc", "Group.h")
            with open(header, "w", encoding="utf-8") as f:
                f.write("class Group {\n    int m_count;\n};\n")
            pack = os.path.join(tmp, "pack")
            os.makedirs(pack)
            with open(os.path.join(pack, "group.fix.json"), "w", encoding="utf-8") as f:
                f.write(
                    '{"id": "g", "exclusive": true, "match": {"require_all": ["Group"]},'
                    ' "edits": [{"file_suffix": "inc/Group.h", "old_text": "int m_count;",'
                    ' "new_text": "std::atomic<int> m_count;"}]}'
                )
            edits = collect_recipe_edits(
                trigger_text="Group race",
                candidate_files=[],
                pack_dirs=[pack],
                search_roots=[root],
            )
            self.assertEqual(len(edits), 1)
            self.assertEqual(edits[0]["file"], header)
            self.assertTrue(edits[0]["exclusive"])


class TestCodeLocatorService(unittest.TestCase):
    def test_rg_empty_function_definition_does_not_walk_repo(self):
        locator = CodeLocatorService(LocatorConfig())
        locator.ctx.rg_grep_lines = lambda *args, **kwargs: []
        with patch("services.code_locator.os.walk", side_effect=AssertionError("unexpected walk")):
            self.assertIsNone(locator.find_function_definition("missingFunction", ["/tmp/nonexistent"]))


if __name__ == "__main__":
    unittest.main()
