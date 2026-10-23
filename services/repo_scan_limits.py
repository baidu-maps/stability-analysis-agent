#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bounds for repository scans so one analysis cannot retain a whole monorepo."""

from __future__ import annotations

import os
from typing import List, Optional, Sequence

MAX_SOURCE_BYTES = 1024 * 1024
MAX_INDEX_FILES = 8000
MAX_REFERENCES_PER_FILE = 32
MAX_EDGES_PER_FILE = 8
MAX_SYMBOL_FANOUT = 25
TREE_SITTER_MAX_WORKERS = 2


def collapse_nested_roots(roots: Sequence[str], *, require_dir: bool = True) -> List[str]:
    """Drop a root that already sits inside another root.

    engine-dev plus its submodule checkout should be scanned once. Missing
    paths stay when ``require_dir`` is false so callers can still report them.
    """
    absolute: List[str] = []
    seen = set()
    for raw in roots or []:
        if not raw:
            continue
        try:
            path = os.path.abspath(os.path.expanduser(str(raw)))
        except OSError:
            continue
        if path in seen:
            continue
        if require_dir and not os.path.isdir(path):
            continue
        seen.add(path)
        absolute.append(path)
    absolute.sort(key=len)
    kept: List[str] = []
    for path in absolute:
        if any(path == parent or path.startswith(parent + os.sep) for parent in kept):
            continue
        kept.append(path)
    return kept


def remember_priority_path(
    matched: List[str],
    extras: List[str],
    path: str,
    terms: Sequence[str],
    max_files: int,
) -> None:
    """Keep stack-adjacent paths ahead of the rest, both lists capped."""
    haystack = path.lower()
    if terms and any(term and term in haystack for term in terms):
        if len(matched) < max_files:
            matched.append(path)
        return
    if len(extras) < max_files:
        extras.append(path)


def merge_priority_paths(matched: Sequence[str], extras: Sequence[str], max_files: int) -> List[str]:
    """Return preferred paths first, then fillers, truncated to ``max_files``."""
    ordered: List[str] = []
    seen = set()
    for path in list(matched) + list(extras):
        if path in seen:
            continue
        seen.add(path)
        ordered.append(path)
        if len(ordered) >= max(1, int(max_files)):
            break
    return ordered


def prefer_terms_lower(terms: Optional[Sequence[str]]) -> List[str]:
    """Normalize path fragments used to prefer crash-adjacent files."""
    lowered: List[str] = []
    seen = set()
    for raw in terms or []:
        text = str(raw or "").strip().lower()
        if len(text) < 2 or text in seen:
            continue
        seen.add(text)
        lowered.append(text)
    return lowered
