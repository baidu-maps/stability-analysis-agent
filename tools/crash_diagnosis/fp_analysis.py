#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FP/x29（帧指针）诊断：验证帧链完整性、检测栈帧损坏。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from tools.crash_diagnosis.types import FpAnalysis


def analyze_fp(
    registers: Dict[str, Any],
    stack_regions: List[Dict[str, Any]],
    sp_value_int: Optional[int] = None,
    arch: str = "arm64",
) -> FpAnalysis:
    """分析 FP 是否在栈区域内、是否高于 SP、帧链是否可信。"""
    result = FpAnalysis()

    values = registers.get("values", {}) if isinstance(registers, dict) else {}
    fp_hex = values.get("x29") or values.get("fp") or values.get("rbp") or values.get("ebp")
    if not fp_hex:
        result.notes.append("日志中无 FP 寄存器值")
        return result

    fp_val = _parse_hex(fp_hex)
    if fp_val is None:
        result.notes.append(f"FP 值无法解析: {fp_hex}")
        return result

    result.fp_value = f"0x{fp_val:x}"

    # FP vs SP 关系（ARM64 栈向下生长：FP 应 >= SP）
    if sp_value_int is not None:
        result.above_sp = fp_val >= sp_value_int
        if not result.above_sp:
            result.notes.append(
                f"FP (0x{fp_val:x}) < SP (0x{sp_value_int:x}) → 帧指针异常，栈帧可能被破坏"
            )
        else:
            diff = fp_val - sp_value_int
            if diff > 1024 * 1024:  # > 1MB
                result.notes.append(
                    f"FP - SP = {diff} 字节（>1MB），当前帧异常大或 FP 指向错误位置"
                )

    # 栈区域检查
    if not stack_regions:
        result.notes.append("无 Maps 数据，无法验证 FP 所在区域")
        return result

    for region in stack_regions:
        start = _parse_hex(region.get("start"))
        end = _parse_hex(region.get("end"))
        if start is None or end is None:
            continue
        if start <= fp_val < end:
            result.in_stack_region = True
            result.frame_chain_plausible = True
            return result

    result.in_stack_region = False
    result.frame_chain_plausible = False
    result.notes.append(
        f"FP (0x{fp_val:x}) 不在任何 [stack] 区域 → 帧链已损坏，backtrace 可能不完整"
    )
    return result


def _parse_hex(val: Any) -> Optional[int]:
    if val is None:
        return None
    s = str(val).strip()
    try:
        return int(s, 16) if s.startswith("0x") or s.startswith("0X") else int(s, 16)
    except (ValueError, TypeError):
        return None
