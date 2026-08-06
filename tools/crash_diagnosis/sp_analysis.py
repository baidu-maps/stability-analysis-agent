#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SP（栈指针）诊断：验证栈状态、检测溢出风险。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from tools.crash_diagnosis.types import SpAnalysis


def analyze_sp(
    registers: Dict[str, Any],
    stack_regions: List[Dict[str, Any]],
    arch: str = "arm64",
) -> SpAnalysis:
    """分析 SP 是否在合法栈区域内、对齐情况、溢出风险。"""
    result = SpAnalysis()

    values = registers.get("values", {}) if isinstance(registers, dict) else {}
    sp_hex = values.get("sp") or values.get("rsp") or values.get("esp")
    if not sp_hex:
        result.notes.append("日志中无 SP 寄存器值")
        return result

    sp_val = _parse_hex(sp_hex)
    if sp_val is None:
        result.notes.append(f"SP 值无法解析: {sp_hex}")
        return result

    result.sp_value = f"0x{sp_val:x}"

    # 对齐检查
    align = 16 if arch in ("arm64", "aarch64") else 8 if arch == "x86_64" else 4
    result.alignment_bytes = align
    result.alignment_ok = (sp_val % align) == 0
    if not result.alignment_ok:
        result.notes.append(f"SP 未 {align} 字节对齐（0x{sp_val:x} % {align} = {sp_val % align}）→ 栈可能已损坏")

    # 栈区域检查
    if not stack_regions:
        result.notes.append("无 Maps 数据，无法验证 SP 所在区域")
        return result

    for region in stack_regions:
        start = _parse_hex(region.get("start"))
        end = _parse_hex(region.get("end"))
        if start is None or end is None:
            continue
        if start <= sp_val < end:
            result.in_stack_region = True
            result.stack_region_start = f"0x{start:x}"
            result.stack_region_end = f"0x{end:x}"
            # 距栈底的距离（ARM64 栈向下生长，start 是栈底/低地址）
            result.distance_to_boundary_bytes = sp_val - start
            stack_size = end - start
            # 溢出风险评估
            if result.distance_to_boundary_bytes < 4096:
                result.stack_overflow_risk = "high"
                result.notes.append(
                    f"SP 距栈底仅 {result.distance_to_boundary_bytes} 字节 → 高栈溢出风险"
                )
            elif result.distance_to_boundary_bytes < stack_size * 0.1:
                result.stack_overflow_risk = "low"
                result.notes.append("SP 接近栈底区域（<10%），有一定溢出风险")
            else:
                result.stack_overflow_risk = "none"
            return result

    # SP 不在任何栈区域
    result.in_stack_region = False
    result.stack_overflow_risk = "overflow"
    result.notes.append(
        f"SP (0x{sp_val:x}) 不在任何 [stack] 区域内 → 栈已溢出或严重损坏"
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
