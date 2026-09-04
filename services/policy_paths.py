"""Policy helpers for tool path validation."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


def extract_workspace_paths(input_data: Dict[str, Any]) -> List[str]:
    """Collect filesystem paths referenced by a tool invocation."""
    if not isinstance(input_data, dict):
        return []
    paths: List[str] = []
    for key in ("code_root", "workspace", "report_dir", "file_path", "path"):
        value = input_data.get(key)
        if isinstance(value, str) and value.strip():
            paths.append(value.strip())
    roots = input_data.get("code_roots")
    if isinstance(roots, list):
        paths.extend(str(x).strip() for x in roots if str(x).strip())
    return paths


def paths_within_allowed_roots(paths: Sequence[str], allowed_roots: Sequence[Path]) -> bool:
    if not allowed_roots:
        return True
    for raw in paths:
        if not raw:
            continue
        try:
            candidate = Path(raw).expanduser().resolve()
        except (OSError, ValueError):
            return False
        if not any(candidate == allowed or allowed in candidate.parents for allowed in allowed_roots):
            return False
    return True
