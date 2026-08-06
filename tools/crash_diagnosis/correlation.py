#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""寄存器间关联分析：UAF 模式、参数寄存器、callee-saved 状态。"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from tools.crash_diagnosis.types import RegisterCorrelation

# UAF 特征: freed memory 被 jemalloc/scudo 等填充的模式
_UAF_PATTERNS = [
    0x6b6b6b6b6b6b6b6b,  # jemalloc freed fill (Linux/HarmonyOS)
    0xdededededededede,  # scudo quarantine
    0xcdcdcdcdcdcdcdcd,  # MSVC uninitialized heap
    0xabababababababab,  # some allocators' guard
    0xfefefefefefefefe,  # freed fill variant
]

_UAF_BYTE_PATTERNS = [0x6b, 0xde, 0xcd, 0xab, 0xfe]

_ARG_REGISTERS_ARM64 = ["x0", "x1", "x2", "x3", "x4", "x5", "x6", "x7"]
_CALLEE_SAVED_ARM64 = [f"x{i}" for i in range(19, 29)]  # x19-x28


def analyze_register_correlation(
    registers: Dict[str, Any],
    crash_info: Dict[str, Any],
) -> RegisterCorrelation:
    """扫描所有寄存器值，识别 UAF 特征、NULL 分布、参数/callee-saved 状态。"""
    result = RegisterCorrelation()

    values = registers.get("values", {}) if isinstance(registers, dict) else {}
    if not values:
        result.notes.append("无寄存器值可分析")
        return result

    for name, hex_val in values.items():
        val = _parse_hex(hex_val)
        if val is None:
            continue

        # NULL 检测
        if val == 0:
            result.null_registers.append(name)

        # UAF 模式检测
        if _is_uaf_pattern(val):
            result.uaf_pattern_registers.append(name)
            result.freed_memory_indicators.append(
                f"{name}=0x{val:x} 含释放内存特征值"
            )

        # 记录参数寄存器（ARM64）
        if name in _ARG_REGISTERS_ARM64:
            result.arg_registers[name] = hex_val

        # 记录 callee-saved 寄存器
        if name in _CALLEE_SAVED_ARM64:
            result.callee_saved[name] = hex_val

    # 汇总
    if result.uaf_pattern_registers:
        result.notes.append(
            f"寄存器 {', '.join(result.uaf_pattern_registers)} 含 UAF/freed 特征 → 疑似使用已释放内存"
        )
    if len(result.null_registers) > 3:
        result.notes.append(
            f"{len(result.null_registers)} 个寄存器为 NULL ({', '.join(result.null_registers[:5])}) "
            "→ 对象可能未初始化或已销毁"
        )

    return result


def _is_uaf_pattern(val: int) -> bool:
    """检测整数值是否含 UAF/freed memory 填充模式。"""
    if val == 0:
        return False
    # 完整 64 位匹配
    for pattern in _UAF_PATTERNS:
        if val == pattern:
            return True
    # 检测重复字节模式（如 0x6b6b6b6b...）
    hex_str = f"{val:016x}"
    if len(hex_str) >= 8:
        byte_val = int(hex_str[:2], 16)
        if byte_val in _UAF_BYTE_PATTERNS:
            # 检查是否全为同一字节
            if all(hex_str[i:i+2] == hex_str[:2] for i in range(0, len(hex_str), 2)):
                return True
    return False


def _parse_hex(val: Any) -> Optional[int]:
    if val is None:
        return None
    s = str(val).strip()
    try:
        return int(s, 16) if s.startswith("0x") or s.startswith("0X") else int(s, 16)
    except (ValueError, TypeError):
        return None
