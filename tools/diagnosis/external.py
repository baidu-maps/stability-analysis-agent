#!/usr/bin/env python3
"""Safe, inspectable execution wrapper for optional platform analyzers."""
from __future__ import annotations

import os
import platform
import subprocess
import time
from dataclasses import asdict, dataclass, field
from typing import List, Mapping, Optional, Sequence


@dataclass
class ExternalToolResult:
    status: str
    command: List[str]
    platform: str
    duration_ms: int = 0
    stdout: str = ""
    stderr: str = ""
    output_files: List[str] = field(default_factory=list)
    return_code: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


def run_external_tool(command: Sequence[str], *, timeout_seconds: int = 120, env: Optional[Mapping[str, str]] = None, output_files: Optional[Sequence[str]] = None) -> ExternalToolResult:
    args = [str(item) for item in command]
    started = time.monotonic()
    base = {"command": args, "platform": f"{platform.system().lower()}-{platform.machine().lower()}", "output_files": [str(item) for item in (output_files or [])]}
    if not args:
        return ExternalToolResult(status="invalid", **base, stderr="empty command")
    try:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout_seconds, env=dict(env) if env else None, check=False)
    except FileNotFoundError as exc:
        return ExternalToolResult(status="unavailable", **base, duration_ms=int((time.monotonic() - started) * 1000), stderr=str(exc))
    except subprocess.TimeoutExpired as exc:
        return ExternalToolResult(status="timeout", **base, duration_ms=int((time.monotonic() - started) * 1000), stdout=str(exc.stdout or ""), stderr=str(exc.stderr or ""))
    status = "success" if completed.returncode == 0 else "failed"
    return ExternalToolResult(status=status, **base, duration_ms=int((time.monotonic() - started) * 1000), stdout=completed.stdout, stderr=completed.stderr, return_code=completed.returncode)
