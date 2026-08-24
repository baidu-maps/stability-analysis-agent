#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Optional RAG / vector stack — lazy import with broad failure handling.

Avoids pulling sentence-transformers / transformers at workflow registration time
when the stack is missing or broken (e.g. NameError inside transformers).
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Optional, Type

from rag.sqlite_compat import sqlite_meets

logger = logging.getLogger(__name__)

_RAG_LOAD_ATTEMPTED = False
_RAG_ANALYZER_CLASS: Optional[Type[Any]] = None
_RAG_LOAD_ERROR: Optional[str] = None

RAG_INSTALL_HINT = (
    "pip install 'stability-analysis-agent[rag]' "
    "(needs numpy<2, torch>=2.4, transformers<4.52, sentence-transformers<3, accelerate>=0.26)"
)


def get_ai_stability_analyzer_class() -> Optional[Type[Any]]:
    """Return AIStabilityAnalyzerWithVectorDB class, or None if the vector stack is unavailable."""
    global _RAG_LOAD_ATTEMPTED, _RAG_ANALYZER_CLASS, _RAG_LOAD_ERROR
    if _RAG_LOAD_ATTEMPTED:
        return _RAG_ANALYZER_CLASS
    _RAG_LOAD_ATTEMPTED = True
    try:
        from rag.vector_database_integration import AIStabilityAnalyzerWithVectorDB

        _RAG_ANALYZER_CLASS = AIStabilityAnalyzerWithVectorDB
    except Exception as exc:
        _RAG_LOAD_ERROR = str(exc)
        logger.warning(
            "RAG vector stack unavailable (%s). Similar-case retrieval disabled; "
            "parse / symbolize / LLM analysis still work. Install with: %s",
            exc,
            RAG_INSTALL_HINT,
        )
    return _RAG_ANALYZER_CLASS


def sqlite_usable_for_metadata() -> bool:
    """Metadata store needs a working sqlite3; UPSERT-era SQL is no longer required."""
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (id TEXT PRIMARY KEY)")
        conn.close()
    except Exception:
        return False
    return True


def sqlite_usable_for_chroma() -> bool:
    """ChromaDB historically requires SQLite >= 3.35."""
    return sqlite_meets(3, 35)


def rag_stack_available() -> bool:
    if not sqlite_usable_for_metadata():
        return False
    return get_ai_stability_analyzer_class() is not None


def rag_load_error() -> Optional[str]:
    get_ai_stability_analyzer_class()
    return _RAG_LOAD_ERROR
