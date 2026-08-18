#!/usr/bin/env python3
"""Git worktree isolation tests for daemon auto-fix runs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.git_worktree_manager import (
    WorktreeIsolationError,
    prepare_isolated_workspace,
    write_workspace_artifacts,
)
from daemon import server
from protocol.models import RunRequest


def _git(repository: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repository), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout)
    return proc.stdout.strip()


class GitWorktreeIsolationTests(unittest.TestCase):
    def _make_repository(self, root: Path) -> Path:
        repository = root / "source"
        source_dir = repository / "src"
        source_dir.mkdir(parents=True)
        (source_dir / "main.cpp").write_text("int value() { return 1; }\n", encoding="utf-8")
        _git(repository, "init")
        _git(repository, "config", "user.email", "tests@example.invalid")
        _git(repository, "config", "user.name", "Test User")
        _git(repository, "add", "src/main.cpp")
        _git(repository, "commit", "-m", "initial")
        return repository

    def test_two_runs_are_isolated_from_source_and_each_other(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = self._make_repository(root)
            workspace_base = root / "worktrees"

            first = prepare_isolated_workspace(
                "run-first",
                [str(repository / "src")],
                workspace_base=workspace_base,
            )
            second = prepare_isolated_workspace(
                "run-second",
                [str(repository / "src")],
                workspace_base=workspace_base,
            )

            first_file = Path(first.isolated_code_roots[0]) / "main.cpp"
            second_file = Path(second.isolated_code_roots[0]) / "main.cpp"
            first_file.write_text("int value() { return 2; }\n", encoding="utf-8")
            second_file.write_text("int value() { return 3; }\n", encoding="utf-8")

            self.assertIn("return 1", (repository / "src" / "main.cpp").read_text(encoding="utf-8"))
            self.assertIn("return 2", first_file.read_text(encoding="utf-8"))
            self.assertIn("return 3", second_file.read_text(encoding="utf-8"))

    def test_writes_manifest_and_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = self._make_repository(root)
            workspace = prepare_isolated_workspace(
                "run-patch",
                [str(repository / "src")],
                workspace_base=root / "worktrees",
            )
            isolated_file = Path(workspace.isolated_code_roots[0]) / "main.cpp"
            isolated_file.write_text("int value() { return 42; }\n", encoding="utf-8")

            artifacts = write_workspace_artifacts(workspace, root / "report")

            manifest = json.loads(Path(str(artifacts["manifest_path"])).read_text(encoding="utf-8"))
            patch = Path(str(artifacts["patch_path"])).read_text(encoding="utf-8")
            self.assertEqual(manifest["original_code_roots"], [str((repository / "src").resolve())])
            self.assertIn("+int value() { return 42; }", patch)

    def test_non_git_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code_root = root / "plain"
            code_root.mkdir()
            with self.assertRaisesRegex(WorktreeIsolationError, "not inside a Git repository"):
                prepare_isolated_workspace(
                    "run-non-git",
                    [str(code_root)],
                    workspace_base=root / "worktrees",
                )

    def test_daemon_rewrites_request_roots_to_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = self._make_repository(root)
            run = server.RunState(
                run_id="run-daemon-map",
                status="queued",
                created_at=0.0,
            )
            request = RunRequest(
                crash_log="/tmp/crash.log",
                code_roots=[str(repository / "src")],
                apply_ai_fixes=True,
            )
            old_root = os.environ.get("STABILITY_AGENT_WORKTREE_DIR")
            os.environ["STABILITY_AGENT_WORKTREE_DIR"] = str(root / "worktrees")
            try:
                isolated_request, workspace = server._prepare_isolated_run(run, request)
            finally:
                if old_root is None:
                    os.environ.pop("STABILITY_AGENT_WORKTREE_DIR", None)
                else:
                    os.environ["STABILITY_AGENT_WORKTREE_DIR"] = old_root

            self.assertIsNotNone(workspace)
            self.assertNotEqual(isolated_request.code_roots, request.code_roots)
            self.assertTrue(Path(isolated_request.code_roots[0]).is_dir())
            self.assertEqual(run.original_code_roots, [str((repository / "src").resolve())])

    def test_uncommitted_changes_in_code_root_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = self._make_repository(root)
            (repository / "src" / "main.cpp").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(WorktreeIsolationError, "uncommitted changes"):
                prepare_isolated_workspace(
                    "run-dirty",
                    [str(repository / "src")],
                    workspace_base=root / "worktrees",
                )


if __name__ == "__main__":
    unittest.main()
