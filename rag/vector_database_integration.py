#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stability memory system integration:
Rule Table -> Vector Index -> Metadata reasoning.
"""

import json
import logging
import re
import hashlib
from datetime import datetime
from typing import Any, Dict, List, Optional

from .rule_store import RuleStore
from .metadata_store import MetadataStore

logger = logging.getLogger(__name__)


def _hash_id(prefix: str, content: str) -> str:
    h = hashlib.md5(content.encode()).hexdigest()
    return f"{prefix}_{h}"


def _normalize_text(s: Any) -> str:
    return str(s or "").strip()


class StabilityMemorySystem:
    def __init__(self, db_path: str = "./vector_db"):
        from .pattern_index import PatternIndex

        self.db_path = db_path
        self.rule_store = RuleStore(f"{db_path}/metadata.sqlite3")
        self.meta_store = MetadataStore(f"{db_path}/metadata.sqlite3")
        self.pattern_index = PatternIndex(db_path)

    # ========== Rule Table ==========
    def add_rule(self, rule: Dict[str, Any]) -> None:
        if not rule.get("rule_id"):
            rule["rule_id"] = _hash_id("rule", json.dumps(rule, ensure_ascii=False))
        self.rule_store.upsert_rule(rule)

    def match_rules(self, features: Dict[str, Any], min_confidence: float = 0.8) -> List[Dict[str, Any]]:
        hits: List[Dict[str, Any]] = []
        for rule in self.rule_store.list_enabled_rules():
            required_features = rule.get("required_features") or []
            if any(not features.get(f) for f in required_features):
                continue
            if not self._evaluate_condition(rule.get("trigger_condition", ""), features):
                continue
            conf = float(rule.get("confidence_score", 0.0))
            hits.append(
                {
                    "rule_id": rule.get("rule_id"),
                    "rule_name": rule.get("rule_name"),
                    "conclusion_type": rule.get("conclusion_type"),
                    "conclusion_payload": rule.get("conclusion_payload"),
                    "confidence_score": conf,
                    "matched_features": {k: features.get(k) for k in required_features},
                    "is_high_confidence": conf >= min_confidence,
                }
            )
        return hits

    def _evaluate_condition(self, expr: str, features: Dict[str, Any]) -> bool:
        if not expr:
            return False
        or_parts = [p.strip() for p in re.split(r"\s+OR\s+", expr, flags=re.IGNORECASE)]
        for part in or_parts:
            and_parts = [p.strip() for p in re.split(r"\s+AND\s+", part, flags=re.IGNORECASE)]
            if all(self._eval_clause(c, features) for c in and_parts if c):
                return True
        return False

    def _eval_clause(self, clause: str, features: Dict[str, Any]) -> bool:
        m = re.match(r"^([A-Za-z0-9_.-]+)\s*(=|!=|~=|~)\s*(.+)$", clause)
        if not m:
            return False
        key, op, raw_value = m.group(1), m.group(2), m.group(3)
        value = _normalize_text(raw_value).strip('"').strip("'")
        target = _normalize_text(features.get(key))
        if op == "=":
            return target.lower() == value.lower()
        if op == "!=":
            return target.lower() != value.lower()
        if op in ("~", "~="):
            try:
                return re.search(value, target, flags=re.IGNORECASE) is not None
            except Exception:
                return value.lower() in target.lower()
        return False

    # ========== Pattern Index ==========
    def add_pattern(self, pattern: Dict[str, Any]) -> bool:
        if not pattern.get("pattern_id"):
            seed = f"{pattern.get('pattern_summary')}{pattern.get('crash_signature')}{pattern.get('created_at')}"
            pattern["pattern_id"] = _hash_id("pattern", seed)
        self.meta_store.upsert_pattern(pattern)
        meta_for_index = {
            "platform_scope": json.dumps(pattern.get("platform_scope") or {}, ensure_ascii=False),
            "crash_category": pattern.get("crash_category"),
            "confidence_score": float(pattern.get("confidence_score", 0.0)),
            "validation_state": pattern.get("validation_state", "draft"),
            "source_type": pattern.get("source_type", "internal_case"),
        }
        return self.pattern_index.add_pattern(
            pattern_id=pattern["pattern_id"],
            pattern_summary=_normalize_text(pattern.get("pattern_summary")),
            crash_signature=_normalize_text(pattern.get("crash_signature")),
            metadata=meta_for_index,
        )

    def retrieve_patterns(
        self,
        query_text: str,
        n_results: int = 5,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        where = filters or None
        hits = self.pattern_index.query(query_text, n_results=n_results, where=where)
        out: List[Dict[str, Any]] = []
        for h in hits:
            pattern_id = h.get("pattern_id")
            meta = self.meta_store.get_pattern(pattern_id) or {}
            if meta.get("validation_state") == "deprecated":
                continue
            distance = float(h.get("distance", 0.0))
            similarity = 1.0 - distance if distance <= 1.0 else 1.0 / (1.0 + distance)
            conf = float(meta.get("confidence_score", 0.0))
            usage_boost = min(0.2, (meta.get("adopted_count", 0) * 0.02))
            score = similarity + conf + usage_boost
            out.append(
                {
                    "pattern_id": pattern_id,
                    "pattern_summary": meta.get("pattern_summary"),
                    "crash_signature": meta.get("crash_signature"),
                    "confidence_score": conf,
                    "validation_state": meta.get("validation_state"),
                    "usage_stats": {
                        "hit_count": meta.get("hit_count", 0),
                        "adopted_count": meta.get("adopted_count", 0),
                        "rejected_count": meta.get("rejected_count", 0),
                    },
                    "distance": distance,
                    "similarity": similarity,
                    "score": score,
                    "platform_scope": meta.get("platform_scope"),
                    "crash_category": meta.get("crash_category"),
                    "evidence_requirements": meta.get("evidence_requirements"),
                    "source_type": meta.get("source_type"),
                }
            )
        out.sort(key=lambda x: x.get("score", 0.0), reverse=True)
        for item in out:
            self.meta_store.update_usage(item["pattern_id"], hit_inc=1)
        return out

    # ========== Evidence & Strategy ==========
    def add_evidence(self, evidence: Dict[str, Any]) -> None:
        if not evidence.get("evidence_id"):
            seed = f"{evidence.get('pattern_id')}{evidence.get('evidence_type')}{evidence.get('raw_content')}"
            evidence["evidence_id"] = _hash_id("evidence", seed)
        # 使用 upsert：同一 evidence_id（pattern_id + 内容）多次写入不会产生重复记录。
        self.meta_store.upsert_evidence(evidence)

    def get_evidence(self, pattern_id: str) -> List[Dict[str, Any]]:
        return self.meta_store.list_evidence(pattern_id)

    def add_fix_strategy(self, strategy: Dict[str, Any]) -> None:
        if not strategy.get("strategy_id"):
            seed = f"{strategy.get('fix_intent')}{strategy.get('constraints')}{strategy.get('created_at')}"
            strategy["strategy_id"] = _hash_id("strategy", seed)
        self.meta_store.upsert_fix_strategy(strategy)

    def get_fix_strategies(self, pattern_ids: List[str]) -> List[Dict[str, Any]]:
        return self.meta_store.list_fix_strategies(pattern_ids)

    def get_guidance_blocks(
        self, rule_ids: List[str], pattern_ids: List[str]
    ) -> List[Dict[str, Any]]:
        return self.meta_store.list_guidance_blocks(rule_ids, pattern_ids)

    def add_guidance_block(self, block: Dict[str, Any]) -> None:
        if not block.get("block_id"):
            block["block_id"] = _hash_id(
                "block",
                f"{block.get('block_type','')}{block.get('content','')[:200]}{datetime.now().isoformat()}",
            )
        self.meta_store.upsert_guidance_block(block)

    # ========== Feedback & Governance ==========
    def record_feedback(self, pattern_id: str, feedback_type: str, comment: str = "") -> None:
        feedback = {
            "feedback_id": _hash_id("feedback", f"{pattern_id}{feedback_type}{datetime.now().isoformat()}"),
            "pattern_id": pattern_id,
            "feedback_type": feedback_type,
            "comment": comment,
            "created_at": datetime.now().isoformat(),
        }
        self.meta_store.add_feedback(feedback)
        if feedback_type == "adopted":
            self.meta_store.update_usage(pattern_id, adopted_inc=1)
        elif feedback_type == "rejected":
            self.meta_store.update_usage(pattern_id, rejected_inc=1)

    def decay_confidence(self, decay: float = 0.01) -> None:
        for p in self.meta_store.list_patterns():
            new_conf = max(0.0, float(p.get("confidence_score", 0.0)) - decay)
            p["confidence_score"] = new_conf
            self.meta_store.upsert_pattern(p)

    def gc_patterns(self, min_confidence: float = 0.2, rejected_threshold: int = 5) -> List[str]:
        deprecated: List[str] = []
        for p in self.meta_store.list_patterns():
            if float(p.get("confidence_score", 0.0)) < min_confidence or int(p.get("rejected_count", 0)) >= rejected_threshold:
                self.meta_store.mark_deprecated(p["pattern_id"])
                deprecated.append(p["pattern_id"])
        return deprecated

    def get_stats(self) -> Dict[str, Any]:
        return {
            "rules": self.rule_store.count_rules(),
            "patterns": self.meta_store.count_patterns(),
            "pattern_index": self.pattern_index.count(),
            "evidence": self.meta_store.count_evidence(),
            "strategies": self.meta_store.count_strategies(),
            "guidance_blocks": self.meta_store.count_guidance_blocks(),
        }

    def export_snapshot(self) -> Dict[str, Any]:
        return {
            "schema_version": 1,
            "exported_at": datetime.now().isoformat(),
            "stats": self.get_stats(),
            "data": {
                "rules": self.rule_store.list_rules(),
                "patterns": self.meta_store.list_patterns(),
                "evidence": self.meta_store.list_all_evidence(),
                "fix_strategies": self.meta_store.list_all_fix_strategies(),
                "pattern_feedback": self.meta_store.list_all_feedback(),
                "guidance_blocks": self.meta_store.list_all_guidance_blocks(),
            },
        }

    def clear_all(self) -> None:
        """清空向量数据库（规则、模式、证据、策略、反馈及向量索引）。"""
        self.rule_store.clear_all()
        self.meta_store.clear_all()
        self.pattern_index.clear_all()

    def import_snapshot(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        data = snapshot.get("data", snapshot)
        rules = data.get("rules", []) or []
        patterns = data.get("patterns", []) or []
        evidence = data.get("evidence", []) or []
        strategies = data.get("fix_strategies", []) or []
        feedback = data.get("pattern_feedback", []) or []
        guidance_blocks = data.get("guidance_blocks", []) or []

        counts = {
            "rules": 0,
            "patterns": 0,
            "evidence": 0,
            "fix_strategies": 0,
            "pattern_feedback": 0,
            "pattern_index": 0,
            "guidance_blocks": 0,
        }
        for rule in rules:
            try:
                self.rule_store.upsert_rule(rule)
                counts["rules"] += 1
            except Exception:
                continue
        for pattern in patterns:
            try:
                added = self.add_pattern(pattern)
                counts["patterns"] += 1
                if added:
                    counts["pattern_index"] += 1
            except Exception:
                continue
        for ev in evidence:
            try:
                self.meta_store.upsert_evidence(ev)
                counts["evidence"] += 1
            except Exception:
                continue
        for st in strategies:
            try:
                self.meta_store.upsert_fix_strategy(st)
                counts["fix_strategies"] += 1
            except Exception:
                continue
        for fb in feedback:
            try:
                self.meta_store.upsert_feedback(fb)
                counts["pattern_feedback"] += 1
            except Exception:
                continue
        for blk in guidance_blocks:
            try:
                self.meta_store.upsert_guidance_block(blk)
                counts["guidance_blocks"] += 1
            except Exception:
                continue
        return counts


class AIStabilityAnalyzerWithVectorDB:
    """
    Backward-compatible wrapper for agent integration.
    """

    def __init__(self, vector_db_path: str = "./vector_db"):
        self.memory = StabilityMemorySystem(vector_db_path)
        logger.info("StabilityMemorySystem 初始化完成")

    def get_database_statistics(self) -> Dict[str, Any]:
        return self.memory.get_stats()

    def add_rule(self, rule: Dict[str, Any]) -> None:
        self.memory.add_rule(rule)

    def add_pattern(self, pattern: Dict[str, Any]) -> bool:
        return self.memory.add_pattern(pattern)

    def add_evidence(self, evidence: Dict[str, Any]) -> None:
        self.memory.add_evidence(evidence)

    def add_fix_strategy(self, strategy: Dict[str, Any]) -> None:
        self.memory.add_fix_strategy(strategy)

    def match_rules(self, features: Dict[str, Any], min_confidence: float = 0.8) -> List[Dict[str, Any]]:
        return self.memory.match_rules(features, min_confidence=min_confidence)

    def retrieve_patterns(
        self,
        query_text: str,
        n_results: int = 5,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        return self.memory.retrieve_patterns(query_text, n_results=n_results, filters=filters)

    def get_evidence(self, pattern_id: str) -> List[Dict[str, Any]]:
        return self.memory.get_evidence(pattern_id)

    def get_fix_strategies(self, pattern_ids: List[str]) -> List[Dict[str, Any]]:
        return self.memory.get_fix_strategies(pattern_ids)

    def get_guidance_blocks(
        self, rule_ids: List[str], pattern_ids: List[str]
    ) -> List[Dict[str, Any]]:
        return self.memory.get_guidance_blocks(rule_ids, pattern_ids)

    def add_guidance_block(self, block: Dict[str, Any]) -> None:
        self.memory.add_guidance_block(block)

    def record_feedback(self, pattern_id: str, feedback_type: str, comment: str = "") -> None:
        self.memory.record_feedback(pattern_id, feedback_type, comment)

    def decay_confidence(self, decay: float = 0.01) -> None:
        self.memory.decay_confidence(decay)

    def gc_patterns(self, min_confidence: float = 0.2, rejected_threshold: int = 5) -> List[str]:
        return self.memory.gc_patterns(min_confidence=min_confidence, rejected_threshold=rejected_threshold)

    def export_snapshot(self) -> Dict[str, Any]:
        return self.memory.export_snapshot()

    def import_snapshot(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        return self.memory.import_snapshot(snapshot)

    def clear_all(self) -> None:
        """清空向量数据库（规则、模式、证据、策略、反馈及向量索引）。"""
        self.memory.clear_all()
