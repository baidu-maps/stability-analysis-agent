"""Deterministic integrity checks for agent-produced source changes.

These checks answer whether the edit was applied within the declared workspace.
They deliberately make no claim about functional correctness or Crash repair.
"""
from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


CORE_SAFETY_CHECKS = (
    "patch_applied",
    "workspace_boundary",
    "change_manifest",
)


def _paths(values: Iterable[str]) -> List[str]:
    return sorted({str(Path(value).expanduser().resolve()) for value in values if str(value).strip()})


def _git_submodule_files(repo_root: str, entry: str) -> List[str]:
    """把父仓库里一行 ``<sub>`` gitlink 改动展开为子模块内部实际改动文件。

    父仓库 ``git status`` 只会显示 `` M <submodule>``，无法反映子模块内部哪几个
    文件被改；安全校验据此比对会出现 declared(子模块内文件) 与 actual(子模块目录)
    不一致。这里对子模块再跑一次 porcelain，把内部改动文件按父仓库绝对路径展开。
    不可用时安全退回原 entry，不改判定语义。
    """
    try:
        sub = Path(repo_root).expanduser().resolve() / entry
        if not sub.is_dir():
            return []
        proc = subprocess.run(
            ["git", "-C", str(sub), "status", "--porcelain=v1", "--untracked-files=no"],
            capture_output=True, text=True, check=False, timeout=20,
        )
    except Exception:
        return []
    out: List[str] = []
    for line in (proc.stdout or "").splitlines():
        if len(line) <= 3:
            continue
        rel = line[3:].strip()
        if not rel:
            continue
        out.append(str((sub / rel).resolve()))
    return out


def _expand_submodule_links(actual: List[str], roots: List[str]) -> List[str]:
    """把 actual 中作为父仓库子模块目录出现的条目展开为子模块内部文件路径。

    父仓库 ``git status`` 只会显示 `` M <submodule>``；安全校验据此比对会出现
    declared(子模块内文件) 与 actual(子模块目录) 不一致。这里把子模块目录条目
    替换为子模块内部实际改动的文件绝对路径；展开失败的目录条目原样保留。
    """
    out: List[str] = []
    for path in actual:
        p = Path(path)
        is_submodule_dir = p.is_dir() and (p / ".git").exists()
        if not is_submodule_dir:
            out.append(path)
            continue
        files = _git_submodule_files(str(p.parent), p.name)
        if files:
            out.extend(files)
        else:
            out.append(path)
    return sorted(set(out))


def _inside(path: str, roots: Sequence[str]) -> bool:
    candidate = Path(path).expanduser().resolve()
    for root in roots:
        try:
            candidate.relative_to(Path(root).expanduser().resolve())
            return True
        except ValueError:
            continue
    return False


@dataclass(frozen=True)
class SafetyContext:
    """Inputs for deterministic patch-safety checks."""

    applied_result: Mapping[str, Any]
    declared_changed_files: Sequence[str]
    actual_changed_files: Sequence[str]
    workspace_roots: Sequence[str]
    diff_text: str = ""


@dataclass(frozen=True)
class SafetyCheckResult:
    """One deterministic safety-check outcome."""

    name: str
    status: str
    summary: str
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SafetyVerificationRunner:
    """Run only exact, deterministic checks; no heuristic rules or LLM calls."""

    def __init__(self, checks: Optional[Sequence[str]] = None) -> None:
        requested = tuple(str(item).strip() for item in (checks or CORE_SAFETY_CHECKS) if str(item).strip())
        unknown = sorted(set(requested) - set(CORE_SAFETY_CHECKS))
        if unknown:
            raise ValueError("unknown safety checks: " + ", ".join(unknown))
        # Code-writing tasks always enforce the integrity core. Configuration
        # may reorder checks but cannot disable workspace/write protections.
        self.checks = tuple(dict.fromkeys((*requested, *CORE_SAFETY_CHECKS)))

    @classmethod
    def from_mapping(cls, config: Optional[Mapping[str, Any]]) -> "SafetyVerificationRunner":
        root = config if isinstance(config, Mapping) else {}
        verification = root.get("verification") if isinstance(root.get("verification"), Mapping) else root
        safety = verification.get("safety") if isinstance(verification, Mapping) else None
        checks = safety.get("checks") if isinstance(safety, Mapping) else None
        return cls(checks if isinstance(checks, (list, tuple)) else None)

    def run(self, context: SafetyContext) -> Dict[str, Any]:
        declared = _paths(context.declared_changed_files)
        actual = _expand_submodule_links(_paths(context.actual_changed_files), _paths(context.workspace_roots))
        roots = _paths(context.workspace_roots)
        results: List[SafetyCheckResult] = []
        for name in self.checks:
            if name == "patch_applied":
                passed = bool(context.applied_result.get("success")) and bool(declared) and bool(actual)
                result = SafetyCheckResult(
                    name, "passed" if passed else "failed",
                    "补丁已应用并产生实际 Git 修改" if passed else "补丁未成功应用或未产生实际 Git 修改",
                    {"declared_count": len(declared), "actual_count": len(actual)},
                )
            elif name == "workspace_boundary":
                outside = [path for path in actual if not _inside(path, roots)]
                result = SafetyCheckResult(
                    name, "passed" if not outside else "failed",
                    "所有修改均位于指定工作区" if not outside else "存在工作区之外的修改",
                    {"workspace_roots": roots, "outside_files": outside},
                )
            elif name == "change_manifest":
                missing = sorted(set(declared) - set(actual))
                undeclared = sorted(set(actual) - set(declared))
                passed = not missing and not undeclared
                result = SafetyCheckResult(
                    name, "passed" if passed else "failed",
                    "补丁记录与实际 Git 修改完全一致" if passed else "补丁记录与实际 Git 修改不一致",
                    {"missing_files": missing, "undeclared_files": undeclared},
                )
            results.append(result)
        status = "passed" if all(item.status == "passed" for item in results) else "failed"
        return {
            "status": status,
            "checks": [item.to_dict() for item in results],
            "summary": "代码修改完整性检查通过" if status == "passed" else "代码修改完整性检查失败",
        }


def verify_sync_integrity(*, expected_files: Sequence[str], copied_files: Sequence[str]) -> Dict[str, Any]:
    """Verify that sync copied exactly the expected set of source files."""
    expected = _paths(expected_files)
    copied = _paths(copied_files)
    missing = sorted(set(expected) - set(copied))
    unexpected = sorted(set(copied) - set(expected))
    status = "passed" if not missing and not unexpected else "failed"
    return {
        "name": "sync_integrity",
        "status": status,
        "summary": "同步文件与预期完全一致" if status == "passed" else "同步文件与预期不一致",
        "evidence": {"expected_files": expected, "copied_files": copied,
                     "missing_files": missing, "unexpected_files": unexpected},
    }
