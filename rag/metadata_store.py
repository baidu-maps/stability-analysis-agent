#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Metadata store for crash patterns, evidence, and fix strategies (SQLite).
"""

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime

from rag.sqlite_compat import upsert_row


class MetadataStore:
    def __init__(self, db_path: str = "./vector_db/metadata.sqlite3"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS crash_pattern_index (
                    pattern_id TEXT PRIMARY KEY,
                    pattern_summary TEXT,
                    crash_signature TEXT,
                    platform_scope TEXT,
                    crash_category TEXT,
                    evidence_requirements TEXT,
                    confidence_score REAL,
                    validation_state TEXT,
                    source_type TEXT,
                    hit_count INTEGER DEFAULT 0,
                    adopted_count INTEGER DEFAULT 0,
                    rejected_count INTEGER DEFAULT 0,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS crash_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    pattern_id TEXT,
                    evidence_type TEXT,
                    raw_content TEXT,
                    normalized_features TEXT,
                    reliability_score REAL,
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fix_strategy (
                    strategy_id TEXT PRIMARY KEY,
                    applicable_pattern_ids TEXT,
                    fix_intent TEXT,
                    constraints TEXT,
                    risk_level TEXT,
                    confidence_score REAL,
                    example_diff TEXT,
                    notes TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pattern_feedback (
                    feedback_id TEXT PRIMARY KEY,
                    pattern_id TEXT,
                    feedback_type TEXT,
                    comment TEXT,
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS analysis_guidance_blocks (
                    block_id TEXT PRIMARY KEY,
                    pattern_id TEXT,
                    rule_id TEXT,
                    block_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    priority INTEGER DEFAULT 0,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )

    def upsert_pattern(self, pattern: Dict[str, Any]) -> None:
        now = datetime.now().isoformat()
        payload = {
            "pattern_id": pattern.get("pattern_id"),
            "pattern_summary": pattern.get("pattern_summary"),
            "crash_signature": pattern.get("crash_signature"),
            "platform_scope": json.dumps(pattern.get("platform_scope") or {}, ensure_ascii=False),
            "crash_category": pattern.get("crash_category"),
            "evidence_requirements": json.dumps(pattern.get("evidence_requirements") or [], ensure_ascii=False),
            "confidence_score": float(pattern.get("confidence_score", 0.0)),
            "validation_state": pattern.get("validation_state", "draft"),
            "source_type": pattern.get("source_type", "internal_case"),
            "hit_count": int(pattern.get("hit_count", 0)),
            "adopted_count": int(pattern.get("adopted_count", 0)),
            "rejected_count": int(pattern.get("rejected_count", 0)),
            "created_at": pattern.get("created_at") or now,
            "updated_at": now,
        }
        with self._connect() as conn:
            upsert_row(
                conn,
                "crash_pattern_index",
                "pattern_id",
                payload,
                (
                    "pattern_id",
                    "pattern_summary",
                    "crash_signature",
                    "platform_scope",
                    "crash_category",
                    "evidence_requirements",
                    "confidence_score",
                    "validation_state",
                    "source_type",
                    "hit_count",
                    "adopted_count",
                    "rejected_count",
                    "created_at",
                    "updated_at",
                ),
                preserve_on_update=("created_at",),
            )

    def get_pattern(self, pattern_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM crash_pattern_index WHERE pattern_id=?",
                (pattern_id,),
            ).fetchone()
        return self._row_to_pattern(row) if row else None

    def list_patterns(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM crash_pattern_index ORDER BY updated_at DESC"
            ).fetchall()
        return [self._row_to_pattern(r) for r in rows]

    def update_usage(self, pattern_id: str, hit_inc: int = 0, adopted_inc: int = 0, rejected_inc: int = 0) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE crash_pattern_index
                SET hit_count = hit_count + ?,
                    adopted_count = adopted_count + ?,
                    rejected_count = rejected_count + ?,
                    updated_at = ?
                WHERE pattern_id = ?
                """,
                (hit_inc, adopted_inc, rejected_inc, datetime.now().isoformat(), pattern_id),
            )

    def mark_deprecated(self, pattern_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE crash_pattern_index
                SET validation_state = ?, updated_at = ?
                WHERE pattern_id = ?
                """,
                ("deprecated", datetime.now().isoformat(), pattern_id),
            )

    def add_evidence(self, evidence: Dict[str, Any]) -> None:
        payload = {
            "evidence_id": evidence.get("evidence_id"),
            "pattern_id": evidence.get("pattern_id"),
            "evidence_type": evidence.get("evidence_type"),
            "raw_content": evidence.get("raw_content"),
            "normalized_features": json.dumps(evidence.get("normalized_features") or {}, ensure_ascii=False),
            "reliability_score": float(evidence.get("reliability_score", 0.0)),
            "created_at": evidence.get("created_at") or datetime.now().isoformat(),
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO crash_evidence (
                    evidence_id, pattern_id, evidence_type, raw_content,
                    normalized_features, reliability_score, created_at
                ) VALUES (
                    :evidence_id, :pattern_id, :evidence_type, :raw_content,
                    :normalized_features, :reliability_score, :created_at
                )
                """,
                payload,
            )

    def upsert_evidence(self, evidence: Dict[str, Any]) -> None:
        payload = {
            "evidence_id": evidence.get("evidence_id"),
            "pattern_id": evidence.get("pattern_id"),
            "evidence_type": evidence.get("evidence_type"),
            "raw_content": evidence.get("raw_content"),
            "normalized_features": json.dumps(evidence.get("normalized_features") or {}, ensure_ascii=False),
            "reliability_score": float(evidence.get("reliability_score", 0.0)),
            "created_at": evidence.get("created_at") or datetime.now().isoformat(),
        }
        with self._connect() as conn:
            upsert_row(
                conn,
                "crash_evidence",
                "evidence_id",
                payload,
                (
                    "evidence_id",
                    "pattern_id",
                    "evidence_type",
                    "raw_content",
                    "normalized_features",
                    "reliability_score",
                    "created_at",
                ),
            )

    def list_evidence(self, pattern_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM crash_evidence WHERE pattern_id=? ORDER BY created_at DESC",
                (pattern_id,),
            ).fetchall()
        return [self._row_to_evidence(r) for r in rows]

    def list_all_evidence(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM crash_evidence ORDER BY created_at DESC"
            ).fetchall()
        return [self._row_to_evidence(r) for r in rows]

    def upsert_fix_strategy(self, strategy: Dict[str, Any]) -> None:
        now = datetime.now().isoformat()
        payload = {
            "strategy_id": strategy.get("strategy_id"),
            "applicable_pattern_ids": json.dumps(strategy.get("applicable_pattern_ids") or [], ensure_ascii=False),
            "fix_intent": strategy.get("fix_intent"),
            "constraints": json.dumps(strategy.get("constraints") or {}, ensure_ascii=False),
            "risk_level": strategy.get("risk_level"),
            "confidence_score": float(strategy.get("confidence_score", 0.0)),
            "example_diff": strategy.get("example_diff"),
            "notes": strategy.get("notes"),
            "created_at": strategy.get("created_at") or now,
            "updated_at": now,
        }
        with self._connect() as conn:
            upsert_row(
                conn,
                "fix_strategy",
                "strategy_id",
                payload,
                (
                    "strategy_id",
                    "applicable_pattern_ids",
                    "fix_intent",
                    "constraints",
                    "risk_level",
                    "confidence_score",
                    "example_diff",
                    "notes",
                    "created_at",
                    "updated_at",
                ),
                preserve_on_update=("created_at",),
            )

    def list_fix_strategies(self, pattern_ids: List[str]) -> List[Dict[str, Any]]:
        if not pattern_ids:
            return []
        placeholders = ",".join(["?"] * len(pattern_ids))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM fix_strategy",
            ).fetchall()
        strategies = [self._row_to_strategy(r) for r in rows]
        out = []
        for s in strategies:
            ids = s.get("applicable_pattern_ids") or []
            if any(pid in ids for pid in pattern_ids):
                out.append(s)
        return out

    def list_all_fix_strategies(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM fix_strategy ORDER BY updated_at DESC").fetchall()
        return [self._row_to_strategy(r) for r in rows]

    def add_feedback(self, feedback: Dict[str, Any]) -> None:
        payload = {
            "feedback_id": feedback.get("feedback_id"),
            "pattern_id": feedback.get("pattern_id"),
            "feedback_type": feedback.get("feedback_type"),
            "comment": feedback.get("comment"),
            "created_at": feedback.get("created_at") or datetime.now().isoformat(),
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pattern_feedback (
                    feedback_id, pattern_id, feedback_type, comment, created_at
                ) VALUES (
                    :feedback_id, :pattern_id, :feedback_type, :comment, :created_at
                )
                """,
                payload,
            )

    def upsert_feedback(self, feedback: Dict[str, Any]) -> None:
        payload = {
            "feedback_id": feedback.get("feedback_id"),
            "pattern_id": feedback.get("pattern_id"),
            "feedback_type": feedback.get("feedback_type"),
            "comment": feedback.get("comment"),
            "created_at": feedback.get("created_at") or datetime.now().isoformat(),
        }
        with self._connect() as conn:
            upsert_row(
                conn,
                "pattern_feedback",
                "feedback_id",
                payload,
                (
                    "feedback_id",
                    "pattern_id",
                    "feedback_type",
                    "comment",
                    "created_at",
                ),
            )

    def list_all_feedback(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pattern_feedback ORDER BY created_at DESC"
            ).fetchall()
        return [self._row_to_feedback(r) for r in rows]

    def count_patterns(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) as c FROM crash_pattern_index").fetchone()
        return int(row["c"]) if row else 0

    def count_evidence(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) as c FROM crash_evidence").fetchone()
        return int(row["c"]) if row else 0

    def count_strategies(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) as c FROM fix_strategy").fetchone()
        return int(row["c"]) if row else 0

    def count_guidance_blocks(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) as c FROM analysis_guidance_blocks").fetchone()
        return int(row["c"]) if row else 0

    def upsert_guidance_block(self, block: Dict[str, Any]) -> None:
        now = datetime.now().isoformat()
        payload = {
            "block_id": block.get("block_id"),
            "pattern_id": block.get("pattern_id"),
            "rule_id": block.get("rule_id"),
            "block_type": block.get("block_type", "analysis_steps"),
            "content": block.get("content", ""),
            "priority": int(block.get("priority", 0)),
            "created_at": block.get("created_at") or now,
            "updated_at": now,
        }
        with self._connect() as conn:
            upsert_row(
                conn,
                "analysis_guidance_blocks",
                "block_id",
                payload,
                (
                    "block_id",
                    "pattern_id",
                    "rule_id",
                    "block_type",
                    "content",
                    "priority",
                    "created_at",
                    "updated_at",
                ),
                preserve_on_update=("created_at",),
            )

    def list_guidance_blocks(
        self, rule_ids: List[str], pattern_ids: List[str]
    ) -> List[Dict[str, Any]]:
        """Return blocks matching rule_ids, pattern_ids, or default (both null). Sorted by priority, block_type."""
        with self._connect() as conn:
            seen: set = set()
            out: List[Dict[str, Any]] = []
            # Default blocks (both null)
            rows = conn.execute(
                """
                SELECT * FROM analysis_guidance_blocks
                WHERE rule_id IS NULL AND pattern_id IS NULL
                ORDER BY priority ASC, block_type ASC
                """
            ).fetchall()
            for r in rows:
                out.append(self._row_to_guidance_block(r))
                seen.add(r["block_id"])
            # Rule-bound blocks
            if rule_ids:
                placeholders = ",".join(["?"] * len(rule_ids))
                rows = conn.execute(
                    f"""
                    SELECT * FROM analysis_guidance_blocks
                    WHERE rule_id IN ({placeholders})
                    ORDER BY priority ASC, block_type ASC
                    """,
                    rule_ids,
                ).fetchall()
                for r in rows:
                    if r["block_id"] not in seen:
                        out.append(self._row_to_guidance_block(r))
                        seen.add(r["block_id"])
            # Pattern-bound blocks
            if pattern_ids:
                placeholders = ",".join(["?"] * len(pattern_ids))
                rows = conn.execute(
                    f"""
                    SELECT * FROM analysis_guidance_blocks
                    WHERE pattern_id IN ({placeholders})
                    ORDER BY priority ASC, block_type ASC
                    """,
                    pattern_ids,
                ).fetchall()
                for r in rows:
                    if r["block_id"] not in seen:
                        out.append(self._row_to_guidance_block(r))
                        seen.add(r["block_id"])
            return out

    def list_all_guidance_blocks(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM analysis_guidance_blocks ORDER BY priority ASC, block_type ASC"
            ).fetchall()
        return [self._row_to_guidance_block(r) for r in rows]

    def clear_all(self) -> None:
        """清空模式、证据、策略、反馈、指导片段表。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM crash_evidence")
            conn.execute("DELETE FROM fix_strategy")
            conn.execute("DELETE FROM pattern_feedback")
            conn.execute("DELETE FROM analysis_guidance_blocks")
            conn.execute("DELETE FROM crash_pattern_index")

    def _row_to_pattern(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "pattern_id": row["pattern_id"],
            "pattern_summary": row["pattern_summary"],
            "crash_signature": row["crash_signature"],
            "platform_scope": json.loads(row["platform_scope"] or "{}"),
            "crash_category": row["crash_category"],
            "evidence_requirements": json.loads(row["evidence_requirements"] or "[]"),
            "confidence_score": row["confidence_score"],
            "validation_state": row["validation_state"],
            "source_type": row["source_type"],
            "hit_count": row["hit_count"],
            "adopted_count": row["adopted_count"],
            "rejected_count": row["rejected_count"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _row_to_evidence(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "evidence_id": row["evidence_id"],
            "pattern_id": row["pattern_id"],
            "evidence_type": row["evidence_type"],
            "raw_content": row["raw_content"],
            "normalized_features": json.loads(row["normalized_features"] or "{}"),
            "reliability_score": row["reliability_score"],
            "created_at": row["created_at"],
        }

    def _row_to_strategy(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "strategy_id": row["strategy_id"],
            "applicable_pattern_ids": json.loads(row["applicable_pattern_ids"] or "[]"),
            "fix_intent": row["fix_intent"],
            "constraints": json.loads(row["constraints"] or "{}"),
            "risk_level": row["risk_level"],
            "confidence_score": row["confidence_score"],
            "example_diff": row["example_diff"],
            "notes": row["notes"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _row_to_guidance_block(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "block_id": row["block_id"],
            "pattern_id": row["pattern_id"],
            "rule_id": row["rule_id"],
            "block_type": row["block_type"],
            "content": row["content"],
            "priority": row["priority"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
