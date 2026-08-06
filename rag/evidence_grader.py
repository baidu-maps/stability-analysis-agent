#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""证据分级评估器。

将崩溃分析的证据强度分为5个等级，并输出置信度标签。
"""

from __future__ import annotations
import re
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Sanitizer/detector keywords
DETECTOR_KEYWORDS = [
    "AddressSanitizer", "ASan", "ThreadSanitizer", "TSan",
    "MemorySanitizer", "MSan", "UBSan", "LeakSanitizer",
    "GWP-ASan", "Valgrind", "MallocStackLogging",
    "guard_malloc", "scudo", "HWASan",
]

# Register/instruction evidence keywords
REGISTER_EVIDENCE_KEYWORDS = [
    "fault_addr", "fault address", "registers:", "pc=", "lr=", "sp=",
    "x0=", "x1=", "r0=", "r1=", "rip=", "rbp=",
]


@dataclass
class EvidenceItem:
    """单条证据。"""
    source: str       # "detector"/"register"/"stack"/"log"/"single_feature"
    description: str  # 证据描述
    raw_excerpt: str  # 原始日志片段
    tier: int         # 该条证据对应的等级


@dataclass
class EvidenceGrade:
    """证据分级评估结果。"""
    tier: int                        # 1-5（内部编号，面向开发者展示时用中文描述）
    confidence_label: str            # 高 / 中 / 低
    tier_description: str            # 等级含义说明（中文）
    evidence_chain: List[EvidenceItem] = field(default_factory=list)
    reasoning: str = ""              # 分级推理说明


TIER_DEFINITIONS = {
    1: "检测器明确报告（ASan、TSan、Valgrind、GWP-ASan 等）",
    2: "指令+寄存器+地址联合证据",
    3: "多项栈特征一致（多帧模式匹配）",
    4: "单一模块/函数特征",
    5: "推测性结论（证据不足）",
}

CONFIDENCE_MAP = {
    1: "高",
    2: "高",
    3: "中",
    4: "低",
    5: "低",
}


class EvidenceGrader:
    """评估崩溃分析的证据强度等级。"""

    def grade(
        self,
        features: Dict[str, Any],
        rule_hits: List[Dict[str, Any]],
        pattern_hits: List[Dict[str, Any]],
        crash_log_content: str = "",
    ) -> EvidenceGrade:
        """评估证据等级。

        Args:
            features: 提取的崩溃特征
            rule_hits: 规则匹配结果
            pattern_hits: 向量模式匹配结果
            crash_log_content: 原始日志内容（用于关键词检测）

        Returns:
            EvidenceGrade 包含分级和证据链
        """
        evidence_chain: List[EvidenceItem] = []

        # --- Tier 1: Detector report ---
        detector_found = self._check_detector(features, crash_log_content)
        if detector_found:
            evidence_chain.append(EvidenceItem(
                source="detector",
                description=f"检测到内存检测器报告: {detector_found}",
                raw_excerpt=detector_found,
                tier=1,
            ))
            return EvidenceGrade(
                tier=1,
                confidence_label="高",
                tier_description=TIER_DEFINITIONS[1],
                evidence_chain=evidence_chain,
                reasoning=f"存在 {detector_found} 检测器的明确报告，可直接作为根因依据",
            )

        # --- Tier 2: Register + address + instruction ---
        register_evidence = self._check_register_evidence(features, crash_log_content)
        if register_evidence:
            evidence_chain.extend(register_evidence)
            if len(register_evidence) >= 2:
                return EvidenceGrade(
                    tier=2,
                    confidence_label="高",
                    tier_description=TIER_DEFINITIONS[2],
                    evidence_chain=evidence_chain,
                    reasoning="存在寄存器+地址+信号的联合证据，可高置信度判定",
                )

        # --- Tier 3: Multiple stack features ---
        stack_evidence = self._check_stack_features(features, rule_hits, pattern_hits)
        if stack_evidence:
            evidence_chain.extend(stack_evidence)
            if len(stack_evidence) >= 2:
                return EvidenceGrade(
                    tier=3,
                    confidence_label="中",
                    tier_description=TIER_DEFINITIONS[3],
                    evidence_chain=evidence_chain,
                    reasoning="多项栈特征一致指向同一根因，中等置信度",
                )

        # --- Tier 4: Single feature ---
        if rule_hits or pattern_hits:
            single_source = (rule_hits[0] if rule_hits else pattern_hits[0]) if (rule_hits or pattern_hits) else {}
            evidence_chain.append(EvidenceItem(
                source="single_feature",
                description=f"单一特征匹配: {single_source.get('rule_name') or single_source.get('pattern_summary', 'unknown')}",
                raw_excerpt="",
                tier=4,
            ))
            return EvidenceGrade(
                tier=4,
                confidence_label="低",
                tier_description=TIER_DEFINITIONS[4],
                evidence_chain=evidence_chain,
                reasoning="仅有单一特征匹配，建议补充更多证据",
            )

        # --- Tier 5: Insufficient evidence ---
        return EvidenceGrade(
            tier=5,
            confidence_label="低",
            tier_description=TIER_DEFINITIONS[5],
            evidence_chain=evidence_chain,
            reasoning="证据不足，无法确定性判断，需要更多日志或符号信息",
        )

    def _check_detector(self, features: Dict[str, Any], log_content: str) -> Optional[str]:
        """Check for sanitizer/detector report."""
        search_text = " ".join([
            str(features.get("crash_reason", "")),
            str(features.get("stack_functions", "")),
            log_content[:5000],  # First 5K chars only for performance
        ])
        for kw in DETECTOR_KEYWORDS:
            if kw.lower() in search_text.lower():
                return kw
        return None

    def _check_register_evidence(self, features: Dict[str, Any], log_content: str) -> List[EvidenceItem]:
        """Check for register + address evidence."""
        items: List[EvidenceItem] = []
        search_text = log_content[:10000]

        # Check fault address
        signal = str(features.get("signal", ""))

        if signal and "SIGSEGV" in signal.upper():
            items.append(EvidenceItem(
                source="register",
                description=f"信号: {signal}",
                raw_excerpt=signal,
                tier=2,
            ))

        # Check for register dumps in log
        has_registers = any(kw.lower() in search_text.lower() for kw in REGISTER_EVIDENCE_KEYWORDS)
        if has_registers:
            items.append(EvidenceItem(
                source="register",
                description="日志中包含寄存器转储信息",
                raw_excerpt="(registers present in log)",
                tier=2,
            ))

        # Check fault address range (near-zero = null pointer)
        crash_address = str(features.get("crash_address", ""))
        if crash_address:
            try:
                addr_val = int(crash_address, 16) if crash_address.startswith("0x") else int(crash_address)
                if addr_val < 0x1000:
                    items.append(EvidenceItem(
                        source="register",
                        description=f"故障地址 {crash_address} 接近零值，指向空指针解引用",
                        raw_excerpt=crash_address,
                        tier=2,
                    ))
            except (ValueError, TypeError):
                pass

        return items

    def _check_stack_features(
        self,
        features: Dict[str, Any],
        rule_hits: List[Dict[str, Any]],
        pattern_hits: List[Dict[str, Any]],
    ) -> List[EvidenceItem]:
        """Check for multiple consistent stack features."""
        items: List[EvidenceItem] = []

        # Multiple rule hits pointing to same pattern
        if len(rule_hits) >= 2:
            patterns = set()
            for hit in rule_hits:
                payload = hit.get("conclusion_payload") or {}
                p = payload.get("pattern") or payload.get("root_cause_l1", "")
                if p:
                    patterns.add(p)
            if len(patterns) == 1:
                items.append(EvidenceItem(
                    source="stack",
                    description=f"多条规则一致指向: {patterns.pop()}",
                    raw_excerpt="",
                    tier=3,
                ))

        # Stack functions contain multiple related keywords
        stack_funcs = str(features.get("stack_functions", ""))
        concurrency_keywords = ["mutex", "lock", "thread", "pthread", "dispatch", "async"]
        memory_keywords = ["malloc", "free", "delete", "new", "alloc", "dealloc", "release"]

        concurrency_count = sum(1 for kw in concurrency_keywords if kw in stack_funcs.lower())
        memory_count = sum(1 for kw in memory_keywords if kw in stack_funcs.lower())

        if concurrency_count >= 2:
            items.append(EvidenceItem(
                source="stack",
                description=f"栈中包含 {concurrency_count} 个并发相关符号",
                raw_excerpt=stack_funcs[:200],
                tier=3,
            ))
        if memory_count >= 2:
            items.append(EvidenceItem(
                source="stack",
                description=f"栈中包含 {memory_count} 个内存管理相关符号",
                raw_excerpt=stack_funcs[:200],
                tier=3,
            ))

        return items

    def render_evidence_grade(self, grade: EvidenceGrade) -> str:
        """渲染证据分级结果为 Markdown（面向开发者，不暴露 Tier 编号）。"""
        lines = [
            f"证据评级: {grade.tier_description}",
            f"置信度: {grade.confidence_label}",
            f"评估理由: {grade.reasoning}",
        ]
        if grade.evidence_chain:
            lines.append("\n证据链:")
            for i, e in enumerate(grade.evidence_chain, 1):
                lines.append(f"  {i}. [{e.source}] {e.description}")
        return "\n".join(lines)
