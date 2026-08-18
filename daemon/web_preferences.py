#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local Web UI preferences (workspace paths + skill toggles)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

DEFAULT_DEMO = {
    "library_dir": "examples/crash_cases/demo_basic/lib/mac",
    "code_roots": ["examples/crash_cases/demo_basic/code_dir"],
}

_PREFS_LOCK = __import__("threading").Lock()


def _prefs_path() -> Path:
    override = os.environ.get("STABILITY_AGENT_WEB_PREFS_FILE", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".config" / "stability-analysis-agent" / "web_preferences.json").resolve()


def _default_prefs() -> Dict[str, Any]:
    from rag.vector_store_config import _default_vector_db_config

    return {
        "workspace": dict(DEFAULT_DEMO),
        "disabled_skills": [],
        "vector_db": _default_vector_db_config(),
    }


def load_web_preferences() -> Dict[str, Any]:
    path = _prefs_path()
    with _PREFS_LOCK:
        if not path.is_file():
            return _default_prefs()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return _default_prefs()
    if not isinstance(data, dict):
        return _default_prefs()
    base = _default_prefs()
    workspace = data.get("workspace") if isinstance(data.get("workspace"), dict) else {}
    base["workspace"] = {
        "library_dir": str(workspace.get("library_dir") or DEFAULT_DEMO["library_dir"]),
        "code_roots": [
            str(x).strip()
            for x in (workspace.get("code_roots") or DEFAULT_DEMO["code_roots"])
            if str(x).strip()
        ],
    }
    disabled = data.get("disabled_skills")
    if isinstance(disabled, list):
        base["disabled_skills"] = [str(x).strip() for x in disabled if str(x).strip()]
    if isinstance(data.get("vector_db"), dict):
        from rag.vector_store_config import normalize_vector_db_config

        base["vector_db"] = normalize_vector_db_config(data.get("vector_db"))
    return base


def save_web_preferences(payload: Dict[str, Any]) -> Dict[str, Any]:
    current = load_web_preferences()
    if isinstance(payload.get("workspace"), dict):
        ws = payload["workspace"]
        if "library_dir" in ws:
            current["workspace"]["library_dir"] = str(ws.get("library_dir") or "").strip()
        if "code_roots" in ws and isinstance(ws["code_roots"], list):
            current["workspace"]["code_roots"] = [
                str(x).strip() for x in ws["code_roots"] if str(x).strip()
            ]
    if "disabled_skills" in payload and isinstance(payload["disabled_skills"], list):
        current["disabled_skills"] = [str(x).strip() for x in payload["disabled_skills"] if str(x).strip()]
    if isinstance(payload.get("vector_db"), dict):
        from rag.vector_store_config import normalize_vector_db_config

        current["vector_db"] = normalize_vector_db_config(
            {**(current.get("vector_db") or {}), **payload["vector_db"]}
        )
    path = _prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _PREFS_LOCK:
        path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return current


def toggle_skill(name: str, *, enabled: bool) -> Dict[str, Any]:
    prefs = load_web_preferences()
    disabled: List[str] = list(prefs.get("disabled_skills") or [])
    key = str(name).strip()
    if not key:
        return prefs
    if enabled and key in disabled:
        disabled.remove(key)
    elif not enabled and key not in disabled:
        disabled.append(key)
    prefs["disabled_skills"] = disabled
    return save_web_preferences({"disabled_skills": disabled})
