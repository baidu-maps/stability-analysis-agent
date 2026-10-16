#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""services.code_location_cache 的单元测试：路径 rebase、缺文件未命中、key 行为。"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

from services import code_location_cache as clc


class CodeLocationCacheTests(unittest.TestCase):
    """校验跨运行缓存的归一化/反归一化与 key 计算。"""

    def setUp(self) -> None:
        """隔离缓存目录到临时 XDG_CACHE_HOME，并清理 kill-switch。"""
        self._env_backup = dict(os.environ)
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["XDG_CACHE_HOME"] = self._tmp.name
        os.environ.pop(clc._DISABLE_ENV, None)

    def tearDown(self) -> None:
        """还原环境变量并清理临时目录。"""
        os.environ.clear()
        os.environ.update(self._env_backup)
        self._tmp.cleanup()

    @staticmethod
    def _make_root_with_file(parent: str, name: str) -> str:
        """在 parent/name 下建 src/x.cpp 并返回 root 绝对路径。"""
        root = os.path.join(parent, name)
        os.makedirs(os.path.join(root, "src"), exist_ok=True)
        with open(os.path.join(root, "src", "x.cpp"), "w", encoding="utf-8") as f:
            f.write("int main() { return 0; }\n")
        return root

    def test_store_load_rebases_paths(self) -> None:
        """store(root1) 后用 root2 load，结果中的路径应 rebase 到 root2。"""
        with tempfile.TemporaryDirectory() as base:
            root1 = self._make_root_with_file(base, "run1")
            root2 = self._make_root_with_file(base, "run2")
            file1 = os.path.join(root1, "src", "x.cpp")
            result = {
                "graph": {"nodes": [{"file": file1, "snippet": ["// at " + file1]}]},
                "note": "located at %s please check" % file1,
            }
            key = "deadbeefcafe0001"
            clc.store(key, [root1], result)
            loaded = clc.load(key, [root2])
            self.assertIsNotNone(loaded)
            file2 = os.path.join(root2, "src", "x.cpp")
            self.assertEqual(loaded["graph"]["nodes"][0]["file"], file2)
            self.assertIn(file2, loaded["note"])
            self.assertIn(file2, loaded["graph"]["nodes"][0]["snippet"][0])
            # 原 root1 路径不应残留
            self.assertNotIn(root1, loaded["note"])

    def test_missing_file_is_miss(self) -> None:
        """rebase 后文件不存在时应判为未命中（返回 None）。"""
        with tempfile.TemporaryDirectory() as base:
            root1 = self._make_root_with_file(base, "run1")
            file1 = os.path.join(root1, "src", "x.cpp")
            result = {"graph": {"nodes": [{"file": file1}]}}
            key = "deadbeefcafe0002"
            clc.store(key, [root1], result)
            empty_root = os.path.join(base, "empty")
            os.makedirs(empty_root, exist_ok=True)
            self.assertIsNone(clc.load(key, [empty_root]))

    def test_compute_key_none_without_git(self) -> None:
        """非 git 目录应禁用缓存（compute_key 返回 None）。"""
        with tempfile.TemporaryDirectory() as base:
            root = self._make_root_with_file(base, "plain")
            self.assertIsNone(clc.compute_key([root], {"a": 1}))
            self.assertIsNone(clc.compute_key([], {"a": 1}))

    def test_kill_switch_disables(self) -> None:
        """kill-switch 置真时 compute_key/load/store 全部禁用。"""
        os.environ[clc._DISABLE_ENV] = "1"
        self.assertIsNone(clc.compute_key(["/tmp/whatever"], {"a": 1}))
        clc.store("deadbeefcafe0003", ["/tmp/whatever"], {"x": 1})
        self.assertIsNone(clc.load("deadbeefcafe0003", ["/tmp/whatever"]))

    def _git(self, root: str, *args: str) -> None:
        """在 root 内执行一条 git 命令（测试辅助）。"""
        subprocess.run(["git", "-C", root, *args], check=True, capture_output=True)

    def test_compute_key_path_independent_across_worktrees(self) -> None:
        """同一 commit 的两个 worktree 路径应得到相同 key（路径无关）。"""
        with tempfile.TemporaryDirectory() as base:
            main = os.path.join(base, "main")
            os.makedirs(os.path.join(main, "src"), exist_ok=True)
            with open(os.path.join(main, "src", "x.cpp"), "w", encoding="utf-8") as f:
                f.write("int main() { return 0; }\n")
            try:
                self._git(main, "init")
                self._git(main, "config", "user.email", "t@t.io")
                self._git(main, "config", "user.name", "t")
                self._git(main, "add", "-A")
                self._git(main, "commit", "-m", "init")
                wt = os.path.join(base, "wt")
                self._git(main, "worktree", "add", wt)
            except Exception:
                self.skipTest("git/worktree 不可用")
            det1 = {"resolved_stack": "f at %s" % os.path.join(main, "src", "x.cpp")}
            det2 = {"resolved_stack": "f at %s" % os.path.join(wt, "src", "x.cpp")}
            k1 = clc.compute_key([main], det1)
            k2 = clc.compute_key([wt], det2)
            self.assertIsNotNone(k1)
            self.assertEqual(k1, k2)


if __name__ == "__main__":
    unittest.main()
