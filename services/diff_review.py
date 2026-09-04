"""Deterministic review of an agent-produced change set."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

_DANGEROUS_PATTERNS = (
    "system(", "popen(", "exec(", "eval(", "dlopen(",
    "strcpy(", "sprintf(", "memcpy(", "deserialize(",
)


@dataclass(frozen=True)
class DiffReview:
    status: str
    changed_files: List[str] = field(default_factory=list)
    unauthorized_files: List[str] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "changed_files": list(self.changed_files),
                "unauthorized_files": list(self.unauthorized_files), "issues": list(self.issues),
                "metrics": dict(self.metrics)}


def _normalize(path: str) -> str:
    value = str(path or "").strip().replace("\\", "/")
    try:
        return Path(value).as_posix()
    except Exception:
        return value


def review_changed_files(changed_files: Iterable[str], allowed_files: Iterable[str] = (), *,
                         diff_text: str = "", max_files: Optional[int] = None,
                         max_diff_lines: Optional[int] = None,
                         changed_contents: Optional[Dict[str, str]] = None,
                         workspace: Optional[str] = None,
                         original_contents: Optional[Dict[str, str]] = None,
                         max_function_lines: Optional[int] = None,
                         declared_files: Optional[Iterable[str]] = None,
                         changed_functions: Optional[Iterable[str]] = None,
                         allowed_functions: Optional[Iterable[str]] = None,
                         allow_dependency_changes: bool = False,
                         allow_public_api_changes: bool = False) -> DiffReview:
    changed = sorted({_normalize(item) for item in changed_files if str(item).strip()})
    allowed = {_normalize(item) for item in allowed_files if str(item).strip()}
    unauthorized = sorted(set(changed) - allowed) if allowed else []
    issues = [f"unauthorized file: {item}" for item in unauthorized]
    declared = {_normalize(item) for item in (declared_files or ()) if str(item).strip()}
    undeclared = sorted(set(changed) - declared) if declared else []
    issues.extend(f"worktree change was not declared by patch action: {item}" for item in undeclared)
    if workspace:
        root = Path(workspace).expanduser().resolve()
        for item in changed:
            try:
                Path(item).expanduser().resolve().relative_to(root)
            except ValueError:
                issues.append(f"changed file outside workspace: {item}")
            except OSError:
                issues.append(f"changed file path is invalid: {item}")
    if max_files is not None and len(changed) > max(0, int(max_files)):
        issues.append(f"changed file count exceeds limit: {len(changed)}>{int(max_files)}")
    diff_lines = sum(
        1 for line in str(diff_text or "").splitlines()
        if (line.startswith("+") and not line.startswith("+++"))
        or (line.startswith("-") and not line.startswith("---"))
    )
    metrics = {"changed_file_count": len(changed), "diff_line_count": diff_lines,
               "undeclared_files": undeclared}
    if max_diff_lines is not None and diff_lines > max(0, int(max_diff_lines)):
        issues.append(f"diff line count exceeds limit: {diff_lines}>{int(max_diff_lines)}")
    normalized_original = {_normalize(path): str(content) for path, content in (original_contents or {}).items()}
    content_map = {_normalize(path): str(content) for path, content in (changed_contents or {}).items()}
    allowed_function_set = {str(x).strip() for x in (allowed_functions or ()) if str(x).strip()}
    changed_function_set = {str(x).strip() for x in (changed_functions or ()) if str(x).strip()}
    unrelated_functions = sorted(changed_function_set - allowed_function_set) if allowed_function_set else []
    issues.extend(f"function is unrelated to diagnosis targets: {item}" for item in unrelated_functions)
    metrics["changed_functions"] = sorted(changed_function_set)
    metrics["unrelated_functions"] = unrelated_functions
    for path, content in content_map.items():
        lowered = str(content or "").lower()
        for pattern in _DANGEROUS_PATTERNS:
            if pattern.lower() in lowered:
                issues.append(f"dangerous API in changed file {path}: {pattern}")
        old = normalized_original.get(path, "")
        old_dependency_lines = {
            line.strip() for line in old.splitlines()
            if line.lstrip().startswith("#include") or line.lstrip().startswith("import ")
        }
        new_dependency_lines = {
            line.strip() for line in str(content).splitlines()
            if line.lstrip().startswith("#include") or line.lstrip().startswith("import ")
        }
        added_dependencies = sorted(new_dependency_lines - old_dependency_lines)
        if added_dependencies:
            metrics.setdefault("dependency_changes", {})[path] = added_dependencies
            if not allow_dependency_changes:
                issues.append(f"new dependency/include/import in {path}: {', '.join(added_dependencies)}")
        if max_function_lines is not None:
            for block in str(content).split("{"):
                if "}" in block and len(block.splitlines()) > int(max_function_lines):
                    issues.append(f"function replacement exceeds limit in {path}")
                    break
        if old:
            removed = set(old.splitlines()) - set(str(content).splitlines())
            removed_text = "\n".join(removed).lower()
            for marker in ("lock", "unlock", "release", "error", "check"):
                if marker in removed_text:
                    issues.append(f"possible {marker}/error-check removal in {path}")
            public_markers = ("public:", "__attribute__", "export ", "extern ")
            suffix = Path(path).suffix.lower()
            removed_declarations = {
                line.strip() for line in removed
                if suffix in {".h", ".hh", ".hpp", ".hxx"}
                and re.search(r"(?:\w|[>&*])\s+\w+\s*\([^;{}]*\)\s*(?:const\s*)?;", line.strip())
            }
            public_api_changed = any(token in removed_text for token in public_markers) or bool(removed_declarations)
            if public_api_changed:
                metrics.setdefault("public_api_change_files", []).append(path)
                if not allow_public_api_changes:
                    issues.append(f"possible public API/ABI change in {path}")
    return DiffReview("failed" if issues else "passed", changed, unauthorized, issues, metrics)
