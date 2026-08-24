#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite upsert compatibility for old libsqlite."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rag.metadata_store import MetadataStore


class MetadataUpsertTests(unittest.TestCase):
    """INSERT/UPDATE works without ON CONFLICT."""

    def test_upsert_pattern_twice(self) -> None:
        """Second write updates summary and keeps created_at."""
        with tempfile.TemporaryDirectory() as tmp:
            store = MetadataStore(str(Path(tmp) / "meta.sqlite3"))
            store.upsert_pattern(
                {
                    "pattern_id": "p1",
                    "pattern_summary": "first",
                    "crash_signature": "sig",
                    "created_at": "2020-01-01T00:00:00",
                }
            )
            store.upsert_pattern(
                {
                    "pattern_id": "p1",
                    "pattern_summary": "second",
                    "crash_signature": "sig",
                    "created_at": "2026-01-01T00:00:00",
                }
            )
            row = store.get_pattern("p1")
        self.assertIsNotNone(row)
        self.assertEqual(row["pattern_summary"], "second")
        self.assertEqual(row["created_at"], "2020-01-01T00:00:00")


if __name__ == "__main__":
    unittest.main()
