#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PC/LR 代码指针诊断：合并 02 的 resolved_registers，检测执行位置异常。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from tools.crash_diagnosis.types import PcLrAnalysis


def analyze_pc_lr(
    registers: Dict[str, Any],
    resolved_registers: Optional[Dict[str, Any]] = None,
    memory_maps_entries: Optional[List[Dict[str, Any]]] = None,
) -> PcLrAnalysis:
    """分析 PC/LR 是否在合法代码区域、是否 JIT 执行。"""
    result = PcLrAnalysis()

    values = registers.get("values", {}) if isinstance(registers, dict) else {}
    address_map = registers.get("address_map", {}) if isinstance(registers, dict) else {}
    pc_info: Dict[str, Any] = {}
    lr_info: Dict[str, Any] = {}

    if not values and not isinstance(resolved_registers, dict):
        result.notes.append("日志中无 PC/LR 寄存器值，且无符号化寄存器结果")

    # PC
    pc_hex = values.get("pc") or values.get("rip") or values.get("eip")
    if pc_hex:
        result.pc_value = pc_hex
        raw_pc = address_map.get("pc") or address_map.get("rip") or {}
        pc_info = raw_pc if isinstance(raw_pc, dict) else {}
        kind = pc_info.get("kind")
        if kind is not None:
            result.pc_in_code_region = kind == "code"
            if kind != "code":
                result.notes.append(f"PC 所在区域类型为 '{kind}'（非代码段）→ 可能执行了非法地址")
    else:
        result.notes.append("日志中无 PC 寄存器值")

    # LR
    lr_hex = values.get("lr") or values.get("x30")
    if lr_hex:
        result.lr_value = lr_hex
        raw_lr = address_map.get("lr") or {}
        lr_info = raw_lr if isinstance(raw_lr, dict) else {}
        kind = lr_info.get("kind")
        if kind is not None:
            result.lr_in_code_region = kind == "code"
    else:
        result.notes.append("日志中无 LR 寄存器值")

    # 合并 03 的 resolved_registers（已符号化的代码指针）
    if isinstance(resolved_registers, dict):
        for reg_name in ("pc", "rip"):
            reg_res = resolved_registers.get(reg_name)
            if isinstance(reg_res, dict) and reg_res.get("resolved_function"):
                result.pc_resolved = {
                    "module": reg_res.get("module"),
                    "offset": reg_res.get("offset"),
                    "function": reg_res.get("resolved_function"),
                }
                break
        for reg_name in ("lr", "x30"):
            reg_res = resolved_registers.get(reg_name)
            if isinstance(reg_res, dict) and reg_res.get("resolved_function"):
                result.lr_resolved = {
                    "module": reg_res.get("module"),
                    "offset": reg_res.get("offset"),
                    "function": reg_res.get("resolved_function"),
                }
                break

    # 从 address_map 补充模块信息
    if not result.pc_resolved and pc_info.get("module"):
        result.pc_resolved = {
            "module": pc_info.get("module"),
            "offset": pc_info.get("offset"),
            "function": None,
        }
    if not result.lr_resolved and lr_info.get("module"):
        result.lr_resolved = {
            "module": lr_info.get("module"),
            "offset": lr_info.get("offset"),
            "function": None,
        }

    # JIT 检测启发式
    if result.pc_in_code_region is False and pc_hex:
        # PC 不在标准代码段 → 可能是 JIT 或代码损坏
        pc_val = _parse_hex(pc_hex)
        if pc_val and memory_maps_entries:
            for entry in memory_maps_entries:
                start = _parse_hex(entry.get("start"))
                end = _parse_hex(entry.get("end"))
                if start and end and start <= pc_val < end:
                    perms = entry.get("perms", "")
                    path = entry.get("path", "")
                    if "x" in perms and ("[anon" in path or not path.strip()):
                        result.jit_execution = True
                        result.notes.append("PC 在匿名可执行区域 → JIT 代码执行")
                    break

    return result


def _parse_hex(val: Any) -> Optional[int]:
    if val is None:
        return None
    s = str(val).strip()
    try:
        return int(s, 16) if s.startswith("0x") or s.startswith("0X") else int(s, 16)
    except (ValueError, TypeError):
        return None
