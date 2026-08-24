#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite helpers that work on CentOS 7 (SQLite 3.7) without UPSERT."""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, Iterable, Optional, Sequence


def sqlite_meets(major: int, minor: int, patch: int = 0) -> bool:
    """Return True when the linked sqlite3 is at least ``major.minor.patch``."""
    info = tuple(int(part) for part in sqlite3.sqlite_version_info[:3])
    while len(info) < 3:
        info = info + (0,)
    return info >= (int(major), int(minor), int(patch))


def upsert_row(
    conn: sqlite3.Connection,
    table: str,
    pk_column: str,
    payload: Dict[str, Any],
    columns: Sequence[str],
    *,
    preserve_on_update: Optional[Iterable[str]] = None,
) -> None:
    """INSERT or UPDATE by primary key without ``ON CONFLICT`` (SQLite 3.24+)."""
    named = ", ".join(columns)
    placeholders = ", ".join(f":{name}" for name in columns)
    pk_value = payload.get(pk_column)
    existing = conn.execute(
        f"SELECT {pk_column} FROM {table} WHERE {pk_column}=?",
        (pk_value,),
    ).fetchone()
    if existing is None:
        conn.execute(
            f"INSERT INTO {table} ({named}) VALUES ({placeholders})",
            payload,
        )
        return
    skip = {pk_column, *(preserve_on_update or ())}
    updates = [name for name in columns if name not in skip]
    if not updates:
        return
    assignments = ", ".join(f"{name}=:{name}" for name in updates)
    conn.execute(
        f"UPDATE {table} SET {assignments} WHERE {pk_column}=:{pk_column}",
        payload,
    )
