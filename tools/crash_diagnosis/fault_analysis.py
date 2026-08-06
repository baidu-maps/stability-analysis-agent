#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""崩溃地址 + 寄存器关联分析：识别 base+offset、成员偏移等模式。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from tools.crash_diagnosis.types import FaultAnalysis

# ARM64 ABI: x0-x7 为参数/返回值寄存器，x0 通常是 this 指针
_ARG_REGS = ("x0", "x1", "x2", "x3", "x4", "x5", "x6", "x7")
_PREFERRED_BASE_REGS = ("x0", "x19", "x20", "x1", "x8")


def analyze_fault_register(
    crash_info: Dict[str, Any],
    registers: Dict[str, Any],
    arch: str = "arm64",
) -> FaultAnalysis:
    """识别崩溃地址来源寄存器、推断 base+offset 成员访问模式。"""
    result = FaultAnalysis()
    result.pointer_size = 8 if arch in ("arm64", "aarch64", "x86_64") else 4

    # 提取崩溃地址
    crash_addr_str = crash_info.get("crash_address")
    if not crash_addr_str:
        result.notes.append("无崩溃地址")
        return result

    crash_addr = _parse_hex(crash_addr_str)
    if crash_addr is None:
        result.notes.append(f"崩溃地址无法解析: {crash_addr_str}")
        return result

    result.crash_address = f"0x{crash_addr:x}"
    result.crash_address_int = crash_addr

    values = registers.get("values", {}) if isinstance(registers, dict) else {}
    analysis = registers.get("analysis", {}) if isinstance(registers, dict) else {}

    # 从 01 的 analysis 获取已识别的来源寄存器
    source_reg = analysis.get("crash_addr_source")
    if source_reg and source_reg in values:
        result.source_register = source_reg
        result.source_register_value = values[source_reg]

    # --- 模式识别 ---

    # 1. 纯空指针 (fault_addr == 0)
    if crash_addr == 0:
        result.pattern = "null_deref"
        result.notes.append("崩溃地址为 0x0 → 纯空指针解引用")
        # 找到值为 0 的寄存器作为来源
        if not result.source_register:
            for reg in _ARG_REGS:
                if _parse_hex(values.get(reg)) == 0:
                    result.source_register = reg
                    result.source_register_value = values[reg]
                    break
        return result

    # 2. 小偏移 (fault_addr < 0x10000) → null ptr + member offset
    if crash_addr < 0x10000:
        result.pattern = "null_base_plus_offset"
        result.member_offset = crash_addr
        result.member_index_estimate = crash_addr // result.pointer_size

        # 寻找为 NULL 的基址寄存器
        for reg in _PREFERRED_BASE_REGS:
            val = _parse_hex(values.get(reg))
            if val == 0:
                result.base_register = reg
                result.base_register_value = values.get(reg)
                break

        # 寻找值等于 crash_addr 的寄存器
        if not result.source_register:
            for name, hex_val in values.items():
                val = _parse_hex(hex_val)
                if val == crash_addr:
                    result.source_register = name
                    result.source_register_value = hex_val
                    break

        if result.base_register:
            result.notes.append(
                f"{result.base_register}=NULL + offset 0x{crash_addr:x} "
                f"→ 空指针对象第 {result.member_index_estimate} 个成员访问"
            )
        else:
            result.notes.append(
                f"故障地址 0x{crash_addr:x}（小偏移）→ 可能的空指针成员访问，"
                f"offset={crash_addr}, 约第 {result.member_index_estimate} 个 {result.pointer_size}B 成员"
            )
        return result

    # 3. 地址在某个寄存器值附近 → 可能是 base+offset
    for reg in _PREFERRED_BASE_REGS + tuple(f"x{i}" for i in range(8, 29)):
        reg_val = _parse_hex(values.get(reg))
        if reg_val is None or reg_val == 0:
            continue
        offset = crash_addr - reg_val
        if 0 < offset < 0x10000:
            # crash_addr = reg + small_offset
            result.pattern = "base_plus_offset"
            result.base_register = reg
            result.base_register_value = values.get(reg)
            result.member_offset = offset
            result.member_index_estimate = offset // result.pointer_size
            result.notes.append(
                f"crash_addr = {reg}(0x{reg_val:x}) + 0x{offset:x} "
                f"→ 对象成员访问（第 {result.member_index_estimate} 个成员）"
            )
            return result

    # 4. 大地址但不在映射区域 → wild pointer
    result.pattern = "unmapped_access"
    result.notes.append(f"崩溃地址 0x{crash_addr:x} 未匹配到寄存器 base+offset 模式")
    return result


def _parse_hex(val: Any) -> Optional[int]:
    if val is None:
        return None
    s = str(val).strip()
    try:
        return int(s, 16) if s.startswith("0x") or s.startswith("0X") else int(s, 16)
    except (ValueError, TypeError):
        return None
