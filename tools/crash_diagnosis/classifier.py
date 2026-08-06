#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""综合崩溃分类器：汇总所有诊断证据产出最终分类和置信度。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from tools.crash_diagnosis.types import (
    CrashClassification,
    FaultAnalysis,
    FpAnalysis,
    PcLrAnalysis,
    RegisterCorrelation,
    SpAnalysis,
)


def classify_crash(
    sp: SpAnalysis,
    fp: FpAnalysis,
    pc_lr: PcLrAnalysis,
    fault: FaultAnalysis,
    correlation: RegisterCorrelation,
    crash_info: Dict[str, Any],
) -> CrashClassification:
    """综合所有证据进行分类打分。"""
    result = CrashClassification()

    signal = str(crash_info.get("signal") or "").upper()
    crash_reason = str(crash_info.get("crash_reason") or "").lower()

    candidates: List[tuple] = []  # (pattern, confidence, summary)

    # --- 规则优先级从高到低 ---

    # 1. 栈溢出/损坏（SP 级别异常最严重）
    if sp.stack_overflow_risk == "overflow":
        candidates.append(("stack_overflow", 0.95, "SP 超出栈区域，栈已溢出"))
    elif sp.stack_overflow_risk == "high":
        candidates.append(("stack_overflow", 0.85, f"SP 距栈底仅 {sp.distance_to_boundary_bytes} 字节"))
    if not sp.alignment_ok and sp.alignment_ok is not None:
        candidates.append(("stack_corruption", 0.80, f"SP 未对齐（{sp.alignment_bytes}B），栈帧可能损坏"))

    # 2. FP 损坏
    if fp.in_stack_region is False:
        candidates.append(("stack_corruption", 0.85, "FP 不在栈区域，帧链已损坏"))
    if fp.above_sp is False:
        candidates.append(("stack_corruption", 0.80, "FP < SP，帧指针异常"))

    # 3. UAF (寄存器含释放模式)
    if correlation.uaf_pattern_registers:
        conf = min(0.90, 0.70 + 0.10 * len(correlation.uaf_pattern_registers))
        regs = ", ".join(correlation.uaf_pattern_registers[:3])
        candidates.append(("use_after_free", conf, f"寄存器 {regs} 含 UAF 特征值"))

    # 4. PC 在非代码区域
    if pc_lr.pc_in_code_region is False and not pc_lr.jit_execution:
        candidates.append(("code_corruption", 0.85, "PC 不在代码段，可能执行了损坏的内存"))
    if pc_lr.jit_execution:
        candidates.append(("jit_failure", 0.70, "PC 在 JIT 匿名区域"))

    # 5. 空指针模式（最常见）
    if fault.pattern == "null_deref":
        candidates.append(("null_pointer_dereference", 0.98, "崩溃地址为 0x0，纯空指针解引用"))
    elif fault.pattern == "null_base_plus_offset":
        base_info = f"，{fault.base_register}=NULL" if fault.base_register else ""
        candidates.append((
            "null_pointer_member_access",
            0.95,
            f"空指针成员访问（offset=0x{fault.member_offset:x}"
            f"，约第{fault.member_index_estimate}个{fault.pointer_size}B成员{base_info}）"
        ))
    elif fault.pattern == "base_plus_offset":
        candidates.append((
            "invalid_object_access",
            0.70,
            f"对象成员访问（{fault.base_register}+0x{fault.member_offset:x}），对象可能已释放或损坏"
        ))
    elif fault.pattern == "unmapped_access":
        candidates.append(("wild_pointer", 0.60, f"崩溃地址 {fault.crash_address} 无法关联到已知模式"))

    # 6. SIGABRT 特殊处理
    if "SIGABRT" in signal or "abort" in crash_reason:
        candidates.append(("explicit_abort", 0.95, "进程主动 abort（SIGABRT）"))

    # --- 选择最高置信度的分类 ---
    if not candidates:
        result.primary_pattern = "unknown"
        result.confidence = 0.0
        result.summary_zh = "证据不足，无法确定崩溃模式"
        return result

    candidates.sort(key=lambda x: x[1], reverse=True)
    best = candidates[0]
    result.primary_pattern = best[0]
    result.confidence = best[1]
    result.summary_zh = best[2]
    result.secondary_patterns = [c[0] for c in candidates[1:4] if c[1] > 0.5]

    return result
