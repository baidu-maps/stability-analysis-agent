"""Crash-safe JSON snapshots for daemon runs and resumable verification."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from services.run_snapshot import HarnessRunSnapshot


def store_root() -> Path:
    return Path(os.environ.get("STABILITY_AGENT_RUN_STORE", "reports/.daemon_runs")).expanduser().resolve()


def save_snapshot(run: Any) -> Path:
    root = store_root()
    root.mkdir(parents=True, exist_ok=True)
    payload = HarnessRunSnapshot.from_daemon_run(run).to_dict()
    target = root / f"{run.run_id}.json"
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(target)
    return target


def load_snapshots() -> list[Dict[str, Any]]:
    root = store_root()
    if not root.is_dir():
        return []
    values = []
    for path in sorted(root.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                values.append(value)
        except (OSError, ValueError):
            continue
    return values


def restore_snapshot_to_run(snapshot: Dict[str, Any], run: Any) -> None:
    HarnessRunSnapshot.from_dict(snapshot).apply_to_daemon_run(run)
