"""Per-run Git worktree isolation for daemon code modifications."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import shutil
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


def isolated_workspace_from_dict(payload: object) -> Optional[IsolatedCodeWorkspace]:
    """Rehydrate a persisted workspace manifest without creating worktrees."""
    if not isinstance(payload, dict):
        return None
    try:
        repositories = [
            RepositoryWorktree(
                Path(item["repository"]).expanduser().resolve(),
                Path(item["worktree"]).expanduser().resolve(),
                str(item["base_commit"]),
            )
            for item in payload.get("repositories", [])
            if isinstance(item, dict)
        ]
        return IsolatedCodeWorkspace(
            run_id=str(payload["run_id"]),
            root=Path(payload["workspace_root"]).expanduser().resolve(),
            original_code_roots=[str(Path(item).expanduser().resolve()) for item in payload.get("original_code_roots", [])],
            isolated_code_roots=[str(Path(item).expanduser().resolve()) for item in payload.get("isolated_code_roots", [])],
            repositories=repositories,
        )
    except (KeyError, TypeError, ValueError):
        return None


def workspace_source_revision(workspace: IsolatedCodeWorkspace) -> str:
    """Stable revision of all original repositories bound to this run."""
    payload = [
        {"repository": str(item.repository.resolve()), "base_commit": item.base_commit}
        for item in workspace.repositories
    ]
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def workspace_revision(workspace: IsolatedCodeWorkspace) -> str:
    """Content revision including each detached worktree's uncommitted diff."""
    payload = []
    for item in workspace.repositories:
        head = _run_git(item.worktree, ["rev-parse", "HEAD"])
        diff = _run_git(item.worktree, ["diff", "--binary", "--no-ext-diff", "HEAD", "--"])
        untracked = _run_git(item.worktree, ["ls-files", "--others", "--exclude-standard"])
        untracked_hashes = []
        for relative in sorted(line for line in untracked.splitlines() if line.strip()):
            path = (item.worktree / relative).resolve()
            if path.is_file():
                untracked_hashes.append((relative, hashlib.sha256(path.read_bytes()).hexdigest()))
        payload.append({
            "worktree": str(item.worktree.resolve()),
            "head": head,
            "diff_hash": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
            "untracked": untracked_hashes,
        })
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def collect_workspace_diff(workspace: IsolatedCodeWorkspace) -> Dict[str, object]:
    """Return the authoritative changed paths and aggregate Git diff."""
    changed_files: List[str] = []
    diff_parts: List[str] = []
    original_contents: Dict[str, str] = {}
    changed_contents: Dict[str, str] = {}
    for item in workspace.repositories:
        # Preserve the two-column porcelain status prefix.  Using the common
        # stripped Git helper here removes the leading space for modifications
        # and shifts the parsed path by one character.
        status_proc = subprocess.run(
            ["git", "-C", str(item.worktree), "status", "--porcelain=v1", "--untracked-files=all"],
            capture_output=True, text=True, check=False,
        )
        if status_proc.returncode != 0:
            raise WorktreeIsolationError((status_proc.stderr or status_proc.stdout or "git status failed").strip())
        status = status_proc.stdout
        for line in status.splitlines():
            relative = line[3:].strip() if len(line) > 3 else ""
            if " -> " in relative:
                relative = relative.split(" -> ", 1)[1]
            if not relative:
                continue
            path = (item.worktree / relative).resolve()
            changed_files.append(str(path))
            if path.is_file():
                changed_contents[str(path)] = path.read_text(encoding="utf-8", errors="replace")
            old = subprocess.run(
                ["git", "-C", str(item.worktree), "show", f"{item.base_commit}:{relative}"],
                capture_output=True, text=True, check=False,
            )
            if old.returncode == 0:
                original_contents[str(path)] = old.stdout
        diff = _run_git(item.worktree, ["diff", "--no-ext-diff", "--unified=3", item.base_commit, "--"])
        if diff:
            diff_parts.append(diff)
    return {
        "changed_files": sorted(set(changed_files)),
        "diff_text": "\n".join(diff_parts),
        "original_contents": original_contents,
        "changed_contents": changed_contents,
    }


def revision_for_code_roots(code_roots: Sequence[str], *, include_diff: bool = False) -> Optional[str]:
    """Stable combined Git revision for one or more source roots."""
    repositories: Dict[str, Path] = {}
    for raw in code_roots:
        if not str(raw).strip():
            continue
        try:
            repository = _repository_for(Path(raw).expanduser().resolve())
        except WorktreeIsolationError:
            continue
        repositories[str(repository)] = repository
    if not repositories:
        return None
    payload = []
    for key, repository in sorted(repositories.items()):
        head = _run_git(repository, ["rev-parse", "HEAD"])
        item = {"repository": key, "head": head}
        if include_diff:
            diff = _run_git(repository, ["diff", "--binary", "--no-ext-diff", "HEAD", "--"])
            item["diff_hash"] = hashlib.sha256(diff.encode("utf-8")).hexdigest()
        payload.append(item)
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


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


def map_original_path(workspace: IsolatedCodeWorkspace, path: str) -> str:
    """Map one original code-root path into its isolated worktree."""
    original = Path(path).expanduser().resolve()
    for original_root, isolated_root in zip(
        workspace.original_code_roots, workspace.isolated_code_roots
    ):
        root = Path(original_root).resolve()
        try:
            relative = original.relative_to(root)
        except ValueError:
            continue
        return str((Path(isolated_root) / relative).resolve())
    return str(original)


def map_result_paths(workspace: IsolatedCodeWorkspace, value: object) -> object:
    """Recursively map absolute source paths in an analysis result."""
    if isinstance(value, str):
        if value.startswith(os.sep):
            resolved_value = str(Path(value).expanduser().resolve())
            if resolved_value != value:
                value = resolved_value
        for original_root, isolated_root in zip(
            workspace.original_code_roots, workspace.isolated_code_roots
        ):
            original_prefix = str(Path(original_root).resolve())
            if value == original_prefix or value.startswith(original_prefix + os.sep):
                return str(Path(isolated_root).resolve()) + value[len(original_prefix):]
        return value
    if isinstance(value, list):
        return [map_result_paths(workspace, item) for item in value]
    if isinstance(value, dict):
        return {key: map_result_paths(workspace, item) for key, item in value.items()}
    return value


def sync_verified_files_back(
    workspace: IsolatedCodeWorkspace,
    isolated_files: Sequence[str],
) -> List[str]:
    """Copy only verified files from the isolated roots back to originals."""
    copied: List[str] = []
    allowed: Dict[Path, Path] = {}
    for original_root, isolated_root in zip(
        workspace.original_code_roots, workspace.isolated_code_roots
    ):
        allowed[Path(isolated_root).resolve()] = Path(original_root).resolve()
    for raw_path in isolated_files:
        source = Path(raw_path).expanduser().resolve()
        destination = None
        for isolated_root, original_root in allowed.items():
            try:
                relative = source.relative_to(isolated_root)
            except ValueError:
                continue
            destination = (original_root / relative).resolve()
            try:
                destination.relative_to(original_root)
            except ValueError:
                destination = None
            break
        if destination is None or not source.is_file():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied.append(str(destination))
    return copied


def cleanup_isolated_workspace(workspace: IsolatedCodeWorkspace, *, force: bool = False) -> List[str]:
    """Remove per-run worktrees while retaining already-written report artifacts."""
    removed: List[str] = []
    for item in reversed(workspace.repositories):
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(item.worktree))
        proc = subprocess.run(["git", "-C", str(item.repository), *args], capture_output=True, text=True, check=False)
        if proc.returncode == 0:
            removed.append(str(item.worktree))
    try:
        workspace.root.rmdir()
    except OSError:
        pass
    return removed


def scan_worktree_runs(root: Optional[Path] = None) -> List[Dict[str, str]]:
    """List run worktree directories that have no active in-memory owner."""
    base = (root or default_worktree_root()).expanduser().resolve()
    if not base.is_dir():
        return []
    out: List[Dict[str, str]] = []
    for item in sorted(base.iterdir()):
        if item.is_dir():
            out.append({"run_id": item.name, "path": str(item)})
    return out
