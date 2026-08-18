#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vector store configuration: local (implemented) and remote (reserved)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


class VectorStoreNotImplementedError(RuntimeError):
    """Remote vector store is not implemented in this release."""


def _default_local_path() -> Path:
    return (Path.home() / ".config" / "stability-analysis-agent" / "vector_db").resolve()


def _default_vector_db_config() -> Dict[str, Any]:
    return {
        "mode": "local",
        "local_path": str(_default_local_path()),
        "remote_url": "",
        "remote_token": "",
    }


def normalize_vector_db_config(raw: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base = _default_vector_db_config()
    if not isinstance(raw, dict):
        return base
    mode = str(raw.get("mode") or "local").strip().lower()
    if mode not in {"local", "remote"}:
        mode = "local"
    base["mode"] = mode
    local_path = str(raw.get("local_path") or "").strip()
    if local_path:
        base["local_path"] = str(Path(local_path).expanduser().resolve())
    base["remote_url"] = str(raw.get("remote_url") or raw.get("url") or "").strip()
    base["remote_token"] = str(raw.get("remote_token") or raw.get("token") or "").strip()
    return base


def load_vector_db_config_from_preferences() -> Dict[str, Any]:
    try:
        from daemon.web_preferences import load_web_preferences

        prefs = load_web_preferences()
        return normalize_vector_db_config(prefs.get("vector_db"))
    except Exception:
        return _default_vector_db_config()


def resolve_vector_db_path(
    *,
    cli_path: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> str:
    explicit = (cli_path or os.environ.get("STABILITY_AGENT_VECTOR_DB_PATH", "")).strip()
    if explicit:
        return str(Path(explicit).expanduser().resolve())
    cfg = normalize_vector_db_config(config) if config is not None else load_vector_db_config_from_preferences()
    if cfg.get("mode") == "local":
        return str(Path(str(cfg.get("local_path") or _default_local_path())).expanduser().resolve())
    return str(_default_local_path())


@dataclass
class VectorStoreHandle:
    mode: str
    local_path: str = ""
    remote_url: str = ""
    remote_token: str = ""
    analyzer: Any = field(default=None, repr=False)


def get_vector_store(
    config: Optional[Dict[str, Any]] = None,
    *,
    cli_path: Optional[str] = None,
) -> VectorStoreHandle:
    from rag.runtime import get_ai_stability_analyzer_class, rag_load_error, RAG_INSTALL_HINT

    cfg = normalize_vector_db_config(config) if config is not None else load_vector_db_config_from_preferences()
    mode = str(cfg.get("mode") or "local")
    if mode == "remote":
        raise VectorStoreNotImplementedError(
            "remote vector store is not implemented yet; use mode=local for debugging"
        )
    analyzer_cls = get_ai_stability_analyzer_class()
    if analyzer_cls is None:
        err = rag_load_error() or "unknown"
        raise RuntimeError(f"RAG runtime unavailable ({err}). Install with: {RAG_INSTALL_HINT}")
    local_path = resolve_vector_db_path(cli_path=cli_path, config=cfg)
    Path(local_path).mkdir(parents=True, exist_ok=True)
    return VectorStoreHandle(
        mode="local",
        local_path=local_path,
        analyzer=analyzer_cls(vector_db_path=local_path),
    )
