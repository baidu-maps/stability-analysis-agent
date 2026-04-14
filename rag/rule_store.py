#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rule store for crash rules (SQLite).
"""

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime


class RuleStore:
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
                CREATE TABLE IF NOT EXISTS crash_rules (
                    rule_id TEXT PRIMARY KEY,
                    rule_name TEXT,
                    trigger_condition TEXT,
                    required_features TEXT,
                    conclusion_type TEXT,
                    conclusion_payload TEXT,
                    confidence_score REAL,
                    enabled INTEGER,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )

    def upsert_rule(self, rule: Dict[str, Any]) -> None:
        now = datetime.now().isoformat()
        payload = {
            "rule_id": rule.get("rule_id"),
            "rule_name": rule.get("rule_name"),
            "trigger_condition": rule.get("trigger_condition"),
            "required_features": json.dumps(rule.get("required_features") or [], ensure_ascii=False),
            "conclusion_type": rule.get("conclusion_type"),
            "conclusion_payload": json.dumps(rule.get("conclusion_payload") or {}, ensure_ascii=False),
            "confidence_score": float(rule.get("confidence_score", 0.0)),
            "enabled": 1 if rule.get("enabled", True) else 0,
            "created_at": rule.get("created_at") or now,
            "updated_at": now,
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO crash_rules (
                    rule_id, rule_name, trigger_condition, required_features,
                    conclusion_type, conclusion_payload, confidence_score,
                    enabled, created_at, updated_at
                ) VALUES (
                    :rule_id, :rule_name, :trigger_condition, :required_features,
                    :conclusion_type, :conclusion_payload, :confidence_score,
                    :enabled, :created_at, :updated_at
                )
                ON CONFLICT(rule_id) DO UPDATE SET
                    rule_name=excluded.rule_name,
                    trigger_condition=excluded.trigger_condition,
                    required_features=excluded.required_features,
                    conclusion_type=excluded.conclusion_type,
                    conclusion_payload=excluded.conclusion_payload,
                    confidence_score=excluded.confidence_score,
                    enabled=excluded.enabled,
                    updated_at=excluded.updated_at
                """,
                payload,
            )

    def list_enabled_rules(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM crash_rules WHERE enabled=1 ORDER BY updated_at DESC"
            ).fetchall()
        return [self._row_to_rule(r) for r in rows]

    def list_rules(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM crash_rules ORDER BY updated_at DESC"
            ).fetchall()
        return [self._row_to_rule(r) for r in rows]

    def get_rule(self, rule_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM crash_rules WHERE rule_id=?", (rule_id,)
            ).fetchone()
        return self._row_to_rule(row) if row else None

    def delete_rule(self, rule_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM crash_rules WHERE rule_id=?", (rule_id,))

    def count_rules(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) as c FROM crash_rules").fetchone()
        return int(row["c"]) if row else 0

    def clear_all(self) -> None:
        """清空规则表。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM crash_rules")

    def _row_to_rule(self, row: sqlite3.Row) -> Dict[str, Any]:
        if row is None:
            return {}
        return {
            "rule_id": row["rule_id"],
            "rule_name": row["rule_name"],
            "trigger_condition": row["trigger_condition"],
            "required_features": json.loads(row["required_features"] or "[]"),
            "conclusion_type": row["conclusion_type"],
            "conclusion_payload": json.loads(row["conclusion_payload"] or "{}"),
            "confidence_score": row["confidence_score"],
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
