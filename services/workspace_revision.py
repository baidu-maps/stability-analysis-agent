"""Stable workspace fingerprints for Git and non-Git source roots."""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Optional, Sequence, Tuple


def workspace_revisions(code_roots: Sequence[str]) -> Tuple[Optional[str], Optional[str]]:
    roots = [Path(str(root)).expanduser().resolve() for root in code_roots or [] if str(root).strip()]
    source_parts = []
    for root in roots:
        try:
            git = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
            if git.returncode == 0:
                source_parts.append(git.stdout.strip())
                continue
        except OSError:
            pass
        for dirpath, _, filenames in os.walk(root):
            for name in sorted(filenames):
                path = Path(dirpath) / name
                try:
                    stat = path.stat()
                    source_parts.append(f"{path}:{stat.st_size}:{stat.st_mtime_ns}")
                except OSError:
                    continue
    source = hashlib.sha256("\n".join(source_parts).encode()).hexdigest() if source_parts else None
    return source, source
