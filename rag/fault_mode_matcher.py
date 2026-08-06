#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三级故障模式匹配引擎。

基于 RuleStore 中 conclusion_type == 'fault_mode' 的规则，
将崩溃特征映射到三级根因（L1/L2/L3）。
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class FaultModeMatch:
    """三级故障模式匹配结果。"""
    rule_id: str
    rule_name: str
    root_cause_l1: str
    root_cause_l2: str
    root_cause_l3: str
    pattern: str
    evidence_tier: int  # 1-5
    fix_direction: str
    responsibility: str  # application/system/third_party/undetermined
    confidence_score: float
    matched_features: Dict[str, Any] = field(default_factory=dict)


class FaultModeMatcher:
    """从 RuleStore 加载 fault_mode 规则并执行匹配。"""

    def __init__(self, analyzer):
        """
        Args:
            analyzer: AIStabilityAnalyzerWithVectorDB or StabilityMemorySystem instance
        """
        self._analyzer = analyzer

    def match(self, features: Dict[str, Any], max_results: int = 3) -> List[FaultModeMatch]:
        """对提取的特征执行三级故障模式匹配。

        Args:
            features: extract_features() 产出的特征字典
            max_results: 最多返回结果数

        Returns:
            按 confidence_score 降序排列的匹配结果
        """
        all_hits = self._analyzer.match_rules(features, min_confidence=0.0)
        # Filter to fault_mode type only
        fault_mode_hits = [
            h for h in all_hits
            if h.get("conclusion_type") == "fault_mode"
        ]
        # Sort by confidence
        fault_mode_hits.sort(key=lambda x: x.get("confidence_score", 0.0), reverse=True)

        results: List[FaultModeMatch] = []
        for hit in fault_mode_hits[:max_results]:
            payload = hit.get("conclusion_payload") or {}
            results.append(FaultModeMatch(
                rule_id=hit.get("rule_id", ""),
                rule_name=hit.get("rule_name", ""),
                root_cause_l1=payload.get("root_cause_l1", ""),
                root_cause_l2=payload.get("root_cause_l2", ""),
                root_cause_l3=payload.get("root_cause_l3", ""),
                pattern=payload.get("pattern", ""),
                evidence_tier=int(payload.get("evidence_tier", 5)),
                fix_direction=payload.get("fix_direction", ""),
                responsibility=payload.get("responsibility", "undetermined"),
                confidence_score=hit.get("confidence_score", 0.0),
                matched_features=hit.get("matched_features", {}),
            ))
        return results

    def render_fault_mode_context(self, matches: List[FaultModeMatch]) -> str:
        """将匹配结果渲染为可拼入 prompt 的 Markdown。"""
        if not matches:
            return ""
        lines = ["故障模式匹配结果:"]
        for i, m in enumerate(matches, 1):
            lines.append(f"\n### 匹配 {i} (置信度: {m.confidence_score:.0%})")
            lines.append(f"| 一级根因 | 二级根因 | 三级根因 |")
            lines.append(f"|----------|----------|----------|")
            lines.append(f"| {m.root_cause_l1} | {m.root_cause_l2} | {m.root_cause_l3} |")
            try:
                from rag.evidence_grader import TIER_DEFINITIONS
                tier_desc = TIER_DEFINITIONS.get(m.evidence_tier, "推测性结论（证据不足）")
            except Exception:
                tier_desc = "推测性结论（证据不足）"
            lines.append(f"- 证据等级: {tier_desc}")
            lines.append(f"- 责任归属: {m.responsibility}")
            lines.append(f"- 修复方向: {m.fix_direction}")
        return "\n".join(lines)
