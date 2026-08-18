#!/usr/bin/env python3
"""Bounded project context discovery for API and crash diagnostics."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence


DEFAULT_SUFFIXES = {".ets", ".ts", ".js", ".cpp", ".cc", ".c", ".h", ".json", ".json5", ".yaml", ".yml"}


def discover_project_context(project_root: Optional[str], needles: Sequence[str] = (), *, max_files: int = 100) -> Dict[str, Any]:
    if not project_root:
        return {"status": "not_requested", "project_root": None, "related_files": [], "api_usage_sites": []}
    root = Path(project_root)
    if not root.is_dir():
        return {"status": "invalid_root", "project_root": str(root), "related_files": [], "api_usage_sites": []}
    terms = [str(item).lower() for item in needles if str(item).strip()]
    related = []
    config = []
    for file in root.rglob("*"):
        if not file.is_file() or file.suffix.lower() not in DEFAULT_SUFFIXES:
            continue
        relative = str(file.relative_to(root))
        if file.name in {"module.json5", "build-profile.json5", "package.json", "CMakeLists.txt"}:
            config.append(relative)
        if terms:
            try:
                content = file.read_text(encoding="utf-8", errors="ignore").lower()
            except OSError:
                continue
            if any(term in content for term in terms):
                related.append(relative)
                if len(related) >= max_files:
                    break
    return {"status": "success", "project_root": str(root), "related_files": related, "api_usage_sites": related, "config_files": config[:max_files]}
