"""Per-run Git worktree isolation for daemon code modifications."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence


class WorktreeIsolationError(RuntimeError):
    """Raised when an isolated Git workspace cannot be prepared."""


@dataclass(frozen=True)
class RepositoryWorktree:
    repository: Path
    worktree: Path
    base_commit: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "repository": str(self.repository),
            "worktree": str(self.worktree),
            "base_commit": self.base_commit,
        }


@dataclass(frozen=True)
class IsolatedCodeWorkspace:
    run_id: str
    root: Path
    original_code_roots: List[str]
    isolated_code_roots: List[str]
    repositories: List[RepositoryWorktree]

    def to_dict(self) -> Dict[str, object]:
        return {
            "run_id": self.run_id,
            "workspace_root": str(self.root),
            "original_code_roots": list(self.original_code_roots),
            "isolated_code_roots": list(self.isolated_code_roots),
            "repositories": [item.to_dict() for item in self.repositories],
        }


def default_worktree_root() -> Path:
    configured = str(os.environ.get("STABILITY_AGENT_WORKTREE_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(tempfile.gettempdir()) / "stability-analysis-agent" / "worktrees"


def _run_git(repository: Path, args: Sequence[str]) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repository), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "git command failed").strip()
        raise WorktreeIsolationError(detail)
    return proc.stdout.strip()


def _repository_for(code_root: Path) -> Path:
    if not code_root.is_dir():
        raise WorktreeIsolationError(f"code_root does not exist or is not a directory: {code_root}")
    try:
        top = _run_git(code_root, ["rev-parse", "--show-toplevel"])
    except WorktreeIsolationError as exc:
        raise WorktreeIsolationError(f"code_root is not inside a Git repository: {code_root}") from exc
    repository = Path(top).resolve()
    try:
        code_root.relative_to(repository)
    except ValueError as exc:
        raise WorktreeIsolationError(f"code_root is outside its Git repository: {code_root}") from exc
    return repository


def _scoped_status(repository: Path, code_roots: Sequence[Path]) -> str:
    pathspecs = [str(root.relative_to(repository)) or "." for root in code_roots]
    return _run_git(
        repository,
        ["status", "--porcelain=v1", "--untracked-files=all", "--", *pathspecs],
    )


def _workspace_name(repository: Path, index: int) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", repository.name).strip("_") or "repository"
    digest = hashlib.sha256(str(repository).encode("utf-8")).hexdigest()[:10]
    return f"{index:02d}_{safe_name}_{digest}"


def prepare_isolated_workspace(
    run_id: str,
    code_roots: Sequence[str],
    *,
    workspace_base: Optional[Path] = None,
) -> IsolatedCodeWorkspace:
    """Create detached worktrees and map each code root into its run workspace."""
    original_roots = [str(Path(root).expanduser().resolve()) for root in code_roots if str(root).strip()]
    if not original_roots:
        raise WorktreeIsolationError("code_roots is empty")

    roots_by_repo: Dict[Path, List[Path]] = {}
    root_repositories: List[Path] = []
    for raw_root in original_roots:
        root = Path(raw_root)
        repository = _repository_for(root)
        roots_by_repo.setdefault(repository, []).append(root)
        root_repositories.append(repository)

    for repository, scoped_roots in roots_by_repo.items():
        dirty = _scoped_status(repository, scoped_roots)
        if dirty:
            preview = "\n".join(dirty.splitlines()[:12])
            raise WorktreeIsolationError(
                "code_root contains uncommitted changes; commit or stash them before an isolated run: "
                f"{repository}\n{preview}"
            )

    base = (workspace_base or default_worktree_root()).expanduser().resolve()
    run_root = base / run_id
    if run_root.exists():
        raise WorktreeIsolationError(f"run workspace already exists: {run_root}")
    run_root.mkdir(parents=True, exist_ok=False)

    created: List[RepositoryWorktree] = []
    worktree_by_repo: Dict[Path, Path] = {}
    try:
        for index, repository in enumerate(roots_by_repo, start=1):
            base_commit = _run_git(repository, ["rev-parse", "HEAD"])
            worktree = run_root / _workspace_name(repository, index)
            _run_git(
                repository,
                ["worktree", "add", "--detach", str(worktree), base_commit],
            )
            created.append(
                RepositoryWorktree(
                    repository=repository,
                    worktree=worktree,
                    base_commit=base_commit,
                )
            )
            worktree_by_repo[repository] = worktree
    except Exception:
        for item in reversed(created):
            subprocess.run(
                ["git", "-C", str(item.repository), "worktree", "remove", "--force", str(item.worktree)],
                capture_output=True,
                text=True,
                check=False,
            )
        try:
            run_root.rmdir()
        except OSError:
            pass
        raise

    isolated_roots = [
        str(worktree_by_repo[repository] / Path(raw_root).relative_to(repository))
        for raw_root, repository in zip(original_roots, root_repositories)
    ]
    return IsolatedCodeWorkspace(
        run_id=run_id,
        root=run_root,
        original_code_roots=original_roots,
        isolated_code_roots=isolated_roots,
        repositories=created,
    )


def write_workspace_artifacts(
    workspace: IsolatedCodeWorkspace,
    report_dir: Path,
) -> Dict[str, Optional[str]]:
    """Write the workspace manifest and aggregate Git patch into a run report."""
    report_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = report_dir / "09_ai_fix_workspace.json"
    manifest_path.write_text(
        json.dumps(workspace.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    patch_parts: List[str] = []
    for item in workspace.repositories:
        patch = _run_git(
            item.worktree,
            ["diff", "--binary", "--no-ext-diff", item.base_commit, "--"],
        )
        if patch:
            patch_parts.append(patch.rstrip() + "\n")

    patch_path: Optional[Path] = None
    if patch_parts:
        patch_path = report_dir / "09_ai_fix.patch"
        patch_path.write_text("\n".join(patch_parts), encoding="utf-8")
    return {
        "manifest_path": str(manifest_path),
        "patch_path": str(patch_path) if patch_path is not None else None,
    }
