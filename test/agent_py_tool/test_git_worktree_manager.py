import subprocess
import tempfile
import unittest
from pathlib import Path

from services.git_worktree_manager import (
    map_original_path,
    map_result_paths,
    prepare_isolated_workspace,
    sync_verified_files_back,
)


class GitWorktreeManagerTest(unittest.TestCase):
    @staticmethod
    def _git(root: str, *args: str) -> None:
        subprocess.run(["git", "-C", root, *args], check=True, capture_output=True, text=True)

    def test_map_and_sync_verified_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            source = repo / "src.cpp"
            source.write_text("before\n", encoding="utf-8")
            self._git(str(repo), "init", "-q")
            self._git(str(repo), "config", "user.email", "test@example.com")
            self._git(str(repo), "config", "user.name", "test")
            self._git(str(repo), "add", ".")
            self._git(str(repo), "commit", "-qm", "initial")
            isolated = prepare_isolated_workspace("test_run", [str(repo)], workspace_base=Path(tmp) / "worktrees")
            mapped = Path(map_original_path(isolated, str(source)))
            mapped.write_text("after\n", encoding="utf-8")
            self.assertEqual(map_result_paths(isolated, {"file": str(source)})["file"], str(mapped))
            copied = sync_verified_files_back(isolated, [str(mapped)])
            self.assertEqual(copied, [str(source.resolve())])
            self.assertEqual(source.read_text(encoding="utf-8"), "after\n")


if __name__ == "__main__":
    unittest.main()
