#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""崩溃诊断数据结构定义。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SpAnalysis:
    """SP（栈指针）诊断结果。"""
    sp_value: Optional[str] = None
    in_stack_region: Optional[bool] = None
    stack_region_start: Optional[str] = None
    stack_region_end: Optional[str] = None
    distance_to_boundary_bytes: Optional[int] = None
    alignment_ok: Optional[bool] = None
    alignment_bytes: int = 16
    stack_overflow_risk: str = "unknown"  # none / low / high / overflow
    notes: List[str] = field(default_factory=list)


@dataclass
class FpAnalysis:
    """FP/x29（帧指针）诊断结果。"""
    fp_value: Optional[str] = None
    in_stack_region: Optional[bool] = None
    above_sp: Optional[bool] = None
    frame_chain_plausible: Optional[bool] = None
    notes: List[str] = field(default_factory=list)


@dataclass
class PcLrAnalysis:
    """PC/LR 代码指针诊断结果。"""
    pc_value: Optional[str] = None
    lr_value: Optional[str] = None
    pc_in_code_region: Optional[bool] = None
    lr_in_code_region: Optional[bool] = None
    pc_resolved: Optional[Dict[str, Any]] = None  # {module, offset, function}
    lr_resolved: Optional[Dict[str, Any]] = None
    jit_execution: bool = False
    notes: List[str] = field(default_factory=list)


@dataclass
class FaultAnalysis:
    """崩溃地址 + 寄存器关联分析。"""
    crash_address: Optional[str] = None
    crash_address_int: Optional[int] = None
    source_register: Optional[str] = None
    source_register_value: Optional[str] = None
    base_register: Optional[str] = None
    base_register_value: Optional[str] = None
    member_offset: Optional[int] = None
    member_index_estimate: Optional[int] = None
    pointer_size: int = 8
    pattern: str = "unknown"  # null_deref / null_base_plus_offset / unmapped / ...
    notes: List[str] = field(default_factory=list)


@dataclass
class RegisterCorrelation:
    """寄存器间关联分析（UAF、double-free 等）。"""
    uaf_pattern_registers: List[str] = field(default_factory=list)
    null_registers: List[str] = field(default_factory=list)
    freed_memory_indicators: List[str] = field(default_factory=list)
    arg_registers: Dict[str, str] = field(default_factory=dict)  # x0-x7 values
    callee_saved: Dict[str, str] = field(default_factory=dict)  # x19-x28 values
    notes: List[str] = field(default_factory=list)


@dataclass
class CrashClassification:
    """综合崩溃分类。"""
    primary_pattern: str = "unknown"
    confidence: float = 0.0
    secondary_patterns: List[str] = field(default_factory=list)
    summary_zh: str = ""


@dataclass
class CrashDiagnosis:
    """完整诊断结果（对应 04a_crash_diagnosis.json）。"""
    crash_classification: CrashClassification = field(default_factory=CrashClassification)
    register_diagnosis: Optional[Dict[str, Any]] = None
    evidence_chain: List[Dict[str, Any]] = field(default_factory=list)
    prompt_section_zh: str = ""
