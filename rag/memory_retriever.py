#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规则 + 向量库检索，生成供 05 提示词使用的 memory_context。"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from rag.feature_extractor import build_pattern_query, extract_features
from rag.runtime import RAG_INSTALL_HINT, get_ai_stability_analyzer_class, rag_stack_available

logger = logging.getLogger(__name__)


def render_memory_context(
    rule_hits: List[Dict[str, Any]],
    pattern_hits: List[Dict[str, Any]],
    evidence_map: Dict[str, Any],
    strategy_hits: List[Dict[str, Any]],
) -> str:
    """将检索结果渲染为可拼入 05 的 Markdown 文本。"""
    parts: List[str] = []
    if rule_hits:
        lines = []
        for item in rule_hits[:3]:
            payload = item.get("conclusion_payload") or {}
            hint = payload.get("hint") or payload.get("pattern") or json.dumps(
                payload, ensure_ascii=False
            )
            lines.append(f"- {item.get('rule_name') or item.get('rule_id')}: {hint}")
        if lines:
            parts.append("规则命中:\n" + "\n".join(lines))
    if pattern_hits:
        lines = []
        for item in pattern_hits[:3]:
            lines.append(f"- {item.get('pattern_summary') or item.get('pattern_id')}")
        if lines:
            parts.append("经验模式召回:\n" + "\n".join(lines))
    if strategy_hits:
        lines = []
        for item in strategy_hits[:3]:
            lines.append(f"- {item.get('fix_intent') or item.get('strategy_id')}")
        if lines:
            parts.append("可参考修复策略:\n" + "\n".join(lines))
    if evidence_map:
        evidence_count = sum(len(v or []) for v in evidence_map.values())
        if evidence_count > 0:
            parts.append(f"证据片段: 共 {evidence_count} 条")
    return "\n\n".join(parts).strip()


def collect_memory_context(
    *,
    parse_result: Dict[str, Any],
    resolved_stack: Dict[str, Any],
    code_context: Dict[str, Any],
    vector_db_path: str = "./vector_db",
    rule_confidence_threshold: float = 0.85,
    vector_db_max_results: int = 3,
    vector_db_readonly: bool = True,
) -> Dict[str, Any]:
    """
    规则优先 + 向量兜底。RAG 栈不可用时返回 skipped 结果（不抛异常）。
    """
    base: Dict[str, Any] = {
        "success": True,
        "skipped": False,
        "skip_reason": None,
        "memory_context": "",
        "rule_hits": [],
        "pattern_hits": [],
        "evidence_map": {},
        "strategy_hits": [],
        "decision_trace": [],
        "vector_used": False,
        "vector_db_readonly": bool(vector_db_readonly),
    }
    analyzer_cls = get_ai_stability_analyzer_class()
    if not rag_stack_available() or analyzer_cls is None:
        base["skipped"] = True
        base["skip_reason"] = "rag_stack_unavailable"
        base["user_message"] = (
            f"未安装或无法加载 RAG 向量栈，已跳过经验检索。安装: {RAG_INSTALL_HINT}"
        )
        return base

    try:
        analyzer = analyzer_cls(vector_db_path=str(vector_db_path or "./vector_db"))
        features = extract_features(parse_result, resolved_stack, code_context)
        rule_hits = analyzer.match_rules(features, min_confidence=float(rule_confidence_threshold))
        high_conf_hits = [h for h in rule_hits if h.get("is_high_confidence")]
        decision_trace: List[Dict[str, Any]] = []
        pattern_hits: List[Dict[str, Any]] = []
        evidence_map: Dict[str, Any] = {}
        strategy_hits: List[Dict[str, Any]] = []
        vector_used = False

        if high_conf_hits:
            decision_trace.append(
                {
                    "stage": "rule",
                    "result": "hit",
                    "rule_ids": [h.get("rule_id") for h in high_conf_hits if h.get("rule_id")],
                }
            )
        else:
            query_text, _signature = build_pattern_query(parse_result, resolved_stack, code_context)
            pattern_hits = analyzer.retrieve_patterns(
                query_text,
                n_results=int(vector_db_max_results),
                record_usage=not vector_db_readonly,
            )
            vector_used = bool(pattern_hits)
            pattern_ids = [p.get("pattern_id") for p in pattern_hits if p.get("pattern_id")]
            for pid in pattern_ids:
                evidence_map[pid] = analyzer.get_evidence(pid)
            strategy_hits = analyzer.get_fix_strategies(pattern_ids)
            decision_trace.append(
                {
                    "stage": "vector",
                    "result": "hit" if pattern_hits else "miss",
                    "pattern_ids": pattern_ids,
                }
            )

        memory_context = render_memory_context(
            rule_hits, pattern_hits, evidence_map, strategy_hits
        )
        base.update(
            {
                "memory_context": memory_context,
                "rule_hits": rule_hits,
                "pattern_hits": pattern_hits,
                "evidence_map": evidence_map,
                "strategy_hits": strategy_hits,
                "decision_trace": decision_trace,
                "vector_used": vector_used,
                "user_message": (
                    "已从向量库只读检索规则/经验参考"
                    if memory_context and vector_db_readonly
                    else (
                        "已检索规则/经验参考"
                        if memory_context
                        else "规则与向量检索均无命中"
                    )
                ),
            }
        )
        return base
    except Exception as exc:
        logger.warning("vector memory retrieval failed: %s", exc)
        base["skipped"] = True
        base["skip_reason"] = "retrieval_error"
        base["error"] = str(exc)
        base["user_message"] = f"经验检索失败，已跳过: {exc}"
        return base
