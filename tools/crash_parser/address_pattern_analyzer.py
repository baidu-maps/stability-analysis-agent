#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""崩溃地址特征模式分析器。

基于崩溃地址的数值特征推断可能的故障类型。
参考华为 DFX Skills 的地址分析逻辑。
"""

from __future__ import annotations
import re
from typing import Any, Dict, Optional


# Known debug/poison fill values used by various allocators
POISON_VALUES = {
    0xDEADBEEF: ("debug_poison", "Windows CRT debug 填充值（未初始化堆内存）"),
    0xBAADF00D: ("debug_poison", "Windows LocalAlloc debug 填充值"),
    0xFEEDFACE: ("debug_poison", "Apple Mach-O magic / debug 填充"),
    0xABABABAB: ("debug_poison", "Windows heap guard 字节（HeapAlloc 后的 guard 区域）"),
    0xFDFDFDFD: ("debug_poison", "Windows CRT no-man's-land（堆块边界填充）"),
    0xCDCDCDCD: ("debug_poison", "Windows CRT 未初始化堆内存"),
    0xDDDDDDDD: ("debug_poison", "Windows CRT 已释放堆内存"),
    0xCCCCCCCC: ("debug_poison", "MSVC 未初始化栈内存"),
    0xA5A5A5A5: ("debug_poison", "嵌入式/RTOS 调试填充值"),
}

# 0x6b fill pattern used by many allocators (jemalloc, scudo) for freed memory
UAF_FILL_BYTE = 0x6B  # 'k'


def analyze_crash_address(address_str: str) -> Dict[str, Any]:
    """基于崩溃地址特征推断崩溃类型。

    Args:
        address_str: 崩溃地址字符串（如 "0x0000001c"、"0x6b6b6b6b"）

    Returns:
        分析结果字典:
        - pattern: 模式名称
        - confidence: 置信度 (0.0-1.0)
        - hint: 人类可读提示
        - address_value: 解析后的数值
        - address_region: 地址区域推断
    """
    if not address_str:
        return {"pattern": "unknown", "confidence": 0.0, "hint": "无崩溃地址信息"}

    # Parse address
    address_str = str(address_str).strip()
    try:
        if address_str.startswith("0x") or address_str.startswith("0X"):
            addr = int(address_str, 16)
        elif re.match(r'^[0-9a-fA-F]+$', address_str) and len(address_str) >= 8:
            addr = int(address_str, 16)
        else:
            addr = int(address_str, 0)
    except (ValueError, TypeError):
        return {"pattern": "unparseable", "confidence": 0.0, "hint": f"无法解析地址: {address_str}"}

    result: Dict[str, Any] = {
        "address_value": addr,
        "address_hex": f"0x{addr:016x}" if addr > 0xFFFFFFFF else f"0x{addr:08x}",
    }

    # --- Pattern 1: Null pointer / near-zero ---
    if addr == 0:
        result.update({
            "pattern": "null_pointer",
            "confidence": 0.99,
            "hint": "空指针解引用（地址为 0x0）",
            "address_region": "null",
        })
        return result

    if addr < 0x1000:
        result.update({
            "pattern": "null_pointer_offset",
            "confidence": 0.95,
            "hint": f"空指针+成员偏移访问（地址 {result['address_hex']}，偏移 {addr} 字节，"
                    f"疑似 ((T*)nullptr)->member 模式）",
            "address_region": "null_page",
            "member_offset": addr,
        })
        return result

    if addr < 0x10000:
        result.update({
            "pattern": "low_address",
            "confidence": 0.75,
            "hint": f"低地址访问（{result['address_hex']}），可能是空指针+大偏移或小整数误用作地址",
            "address_region": "low",
        })
        return result

    # --- Pattern 2: 0x6b6b... UAF fill pattern ---
    addr_bytes = addr.to_bytes(8, 'big') if addr <= 0xFFFFFFFFFFFFFFFF else addr.to_bytes(8, 'big')
    non_zero_bytes = [b for b in addr_bytes if b != 0]
    if non_zero_bytes and all(b == UAF_FILL_BYTE for b in non_zero_bytes):
        result.update({
            "pattern": "use_after_free_fill",
            "confidence": 0.90,
            "hint": f"UAF 特征地址（{result['address_hex']} 全为 0x6b 填充，"
                    "jemalloc/scudo 等分配器释放后填充值）",
            "address_region": "freed_heap",
        })
        return result

    # Check for partial 0x6b pattern (e.g., upper bits are 0x6b)
    hex_str = f"{addr:016x}"
    if hex_str.count("6b") >= 3:
        result.update({
            "pattern": "use_after_free_partial",
            "confidence": 0.80,
            "hint": f"疑似 UAF（{result['address_hex']} 含多处 0x6b 字节，"
                    "可能为释放后内存的部分覆写）",
            "address_region": "freed_heap",
        })
        return result

    # --- Pattern 3: Known poison/debug fill values ---
    # Check 32-bit truncation for poison values
    addr_32 = addr & 0xFFFFFFFF
    if addr_32 in POISON_VALUES:
        pattern_name, desc = POISON_VALUES[addr_32]
        result.update({
            "pattern": pattern_name,
            "confidence": 0.90,
            "hint": f"调试填充值（{result['address_hex']}）: {desc}",
            "address_region": "debug_fill",
        })
        return result

    # --- Pattern 4: Repeating byte patterns (potential corruption) ---
    if len(set(non_zero_bytes)) == 1 and len(non_zero_bytes) >= 4:
        fill_byte = non_zero_bytes[0]
        result.update({
            "pattern": "repeating_fill",
            "confidence": 0.70,
            "hint": f"重复字节模式（{result['address_hex']}，全为 0x{fill_byte:02x}），"
                    "疑似内存填充或损坏",
            "address_region": "corrupted",
        })
        return result

    # --- Pattern 5: Stack region heuristic ---
    # Typical stack addresses: 0x7fff... (Linux x86_64), 0x16f... (macOS arm64)
    if (0x7FFE00000000 <= addr <= 0x7FFF00000000 or  # Linux x86_64 stack
        0x16F000000000 <= addr <= 0x170000000000 or  # macOS arm64 stack
        0xBE000000 <= addr <= 0xC0000000):            # Linux arm32 stack
        result.update({
            "pattern": "stack_region",
            "confidence": 0.60,
            "hint": f"地址 {result['address_hex']} 位于典型栈区域，"
                    "可能与栈损坏、局部变量越界或栈溢出相关",
            "address_region": "stack",
        })
        return result

    # --- Pattern 6: Heap region heuristic (broad) ---
    # Just classify as normal heap/code if nothing else matches
    result.update({
        "pattern": "normal_address",
        "confidence": 0.0,
        "hint": f"地址 {result['address_hex']} 无特殊模式，需结合其他证据分析",
        "address_region": "heap_or_code",
    })
    return result


def analyze_crash_address_from_crash_info(crash_info: Dict[str, Any]) -> Dict[str, Any]:
    """从 crash_info 中获取崩溃地址并分析。

    Args:
        crash_info: parse_result["crash_info"] 字典

    Returns:
        address_analysis 字典，可直接写入报告
    """
    addr = crash_info.get("crash_address") or crash_info.get("fault_addr")
    if not addr:
        return {}

    analysis = analyze_crash_address(str(addr))
    if analysis.get("pattern") == "unparseable":
        return {}

    return {"address_analysis": analysis}
