#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""证据罗盘：把 PC / 符号 / 反汇编 / 寄存器完备度收成结构化字段。

用于 04a_crash_diagnosis.json 与 prompt「证据罗盘」小节，
让缺证可见、置信度有上限。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def build_evidence_compass(
    *,
    crash_info: Dict[str, Any],
    stack_summary: Dict[str, Any],
    data_availability: Dict[str, Any],
    fault_notes: Optional[List[str]] = None,
    fault_address: Optional[str] = None,
    fault_pattern: Optional[str] = None,
    pc_value: Optional[str] = None,
    classification: Optional[Dict[str, Any]] = None,
    deterministic_facts: Optional[List[Dict[str, Any]]] = None,
    disassembly: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构建证据罗盘字典。"""
    classification = classification or {}
    deterministic_facts = deterministic_facts or []
    disassembly = disassembly or {}
    fault_notes = fault_notes or []
    stack_summary = stack_summary or {}
    top = (stack_summary.get("top_frames") or [{}])[0] if stack_summary.get("top_frames") else {}

    pc_vs_fault = "unknown"
    if any("与 #00" in n or "更可能是 PC" in n for n in fault_notes):
        pc_vs_fault = "crash_address_looks_like_pc"
    elif fault_pattern == "pc_address_as_crash_addr":
        pc_vs_fault = "crash_address_looks_like_pc"
    elif fault_address and _is_near_null(fault_address):
        pc_vs_fault = "fault_addr_near_null"
    elif fault_address and pc_value and _addr_eq(fault_address, pc_value):
        pc_vs_fault = "crash_address_equals_pc"
    elif fault_address:
        pc_vs_fault = "fault_addr_present"

    has_symbol_line = bool(top.get("file") and top.get("line"))
    has_symbol_func = bool(top.get("function") or stack_summary.get("crash_function"))
    disasm_triggered = bool(disassembly.get("triggered"))
    disasm_skip = disassembly.get("skip_reason")

    layers = {
        "location_pc": {
            "available": bool(pc_value) or bool(top.get("address")) or bool(fault_address),
            "pc": pc_value or top.get("address"),
            "fault_address": fault_address,
            "pc_vs_fault": pc_vs_fault,
            "role_zh": "告诉你在哪里崩溃",
        },
        "symbol_source": {
            "available": bool(has_symbol_func),
            "has_file_line": has_symbol_line,
            "function": top.get("function") or stack_summary.get("crash_function"),
            "module": top.get("module") or stack_summary.get("crash_module"),
            "file": top.get("file"),
            "line": top.get("line"),
            "role_zh": "告诉你对应什么符号/源码位置",
        },
        "disassembly": {
            "available": disasm_triggered and not disassembly.get("error"),
            "triggered": disasm_triggered,
            "skip_reason": disasm_skip,
            "skip_detail": disassembly.get("skip_detail"),
            "access_direction": ((disassembly.get("result") or {}) if isinstance(disassembly.get("result"), dict) else {}).get("access_direction"),
            "role_zh": "告诉你 CPU 正在执行什么指令",
        },
        "registers": {
            "available": bool(data_availability.get("has_registers")),
            "has_resolved_registers": bool(data_availability.get("has_resolved_registers")),
            "has_memory_maps": bool(data_availability.get("has_memory_maps")),
            "role_zh": "告诉你 CPU 在操作什么数据",
        },
    }

    missing: List[str] = []
    if not layers["registers"]["available"]:
        missing.append("寄存器转储")
    if not layers["registers"]["has_memory_maps"]:
        missing.append("内存映射(Maps)")
    if not layers["symbol_source"]["available"]:
        missing.append("符号化函数名")
    elif not layers["symbol_source"]["has_file_line"]:
        missing.append("源码文件:行号（仅有符号名）")
    if not layers["disassembly"]["available"]:
        if disasm_skip:
            missing.append(f"反汇编（跳过: {disasm_skip}）")
        else:
            missing.append("反汇编（未触发）")
    if pc_vs_fault == "crash_address_looks_like_pc":
        missing.append("真实 fault address（当前崩溃地址疑似 PC）")

    raw_conf = float(classification.get("confidence") or 0.0)
    ceiling = _confidence_ceiling(
        raw_conf,
        has_registers=bool(layers["registers"]["available"]),
        has_maps=bool(layers["registers"]["has_memory_maps"]),
        has_symbol=bool(layers["symbol_source"]["available"]),
        has_file_line=has_symbol_line,
        has_disasm=bool(layers["disassembly"]["available"]),
        deterministic_facts=deterministic_facts,
        pc_vs_fault=pc_vs_fault,
    )

    return {
        "schema_version": 1,
        "layers": layers,
        "missing_evidence": missing,
        "confidence_raw": round(raw_conf, 3),
        "confidence_ceiling": ceiling,
        "confidence_note_zh": _ceiling_note(ceiling, raw_conf, missing, deterministic_facts),
        "signal": crash_info.get("signal"),
        "analysis_order_zh": [
            "1. 位置(PC/fault)",
            "2. 符号/源码(addr2line)",
            "3. 指令(反汇编，按需)",
            "4. 数据(寄存器)",
            "5. AI 串联根因",
        ],
    }


def enrich_data_availability(
    data_availability: Dict[str, Any],
    *,
    stack_summary: Dict[str, Any],
    disassembly: Dict[str, Any],
    pc_vs_fault: str,
) -> Dict[str, Any]:
    """扩展 data_availability，保持旧字段兼容。"""
    out = dict(data_availability or {})
    top = (stack_summary.get("top_frames") or [{}])[0] if stack_summary.get("top_frames") else {}
    out["has_symbolized_function"] = bool(
        top.get("function") or stack_summary.get("crash_function")
    )
    out["has_source_file_line"] = bool(top.get("file") and top.get("line"))
    out["has_disassembly"] = bool(disassembly.get("triggered")) and not disassembly.get("error")
    out["disassembly_skip_reason"] = disassembly.get("skip_reason")
    out["pc_vs_fault"] = pc_vs_fault
    return out


def _confidence_ceiling(
    raw: float,
    *,
    has_registers: bool,
    has_maps: bool,
    has_symbol: bool,
    has_file_line: bool,
    has_disasm: bool,
    deterministic_facts: List[Dict[str, Any]],
    pc_vs_fault: str,
) -> float:
    """根据证据完备度给出置信度上限。"""
    _ = (raw, has_disasm)  # raw 用于展示侧 note；反汇编按需不单独压低
    best_det = 0.0
    for f in deterministic_facts:
        if str(f.get("fact_type") or "") in {
            "null_pointer", "abort", "detector_report", "stack_overflow", "divide_by_zero"
        }:
            best_det = max(best_det, float(f.get("confidence") or 0.0))

    # 故障地址级确定性空指针（conf≈1.0）可接近满分
    if best_det >= 0.99:
        return 1.0
    if best_det >= 0.95:
        ceiling = 0.95
    else:
        ceiling = 0.95
        if not has_registers:
            ceiling = min(ceiling, 0.75)
        if not has_maps and not has_registers:
            ceiling = min(ceiling, 0.72)
        if not has_symbol:
            ceiling = min(ceiling, 0.55)
        elif not has_file_line:
            ceiling = min(ceiling, 0.85 if has_registers else 0.72)
        if pc_vs_fault in ("crash_address_looks_like_pc", "crash_address_equals_pc"):
            ceiling = min(ceiling, 0.70)

    # 上限不低于已有高置信确定性（符号启发 0.85 等），但不超过 1
    ceiling = max(ceiling, min(best_det, 0.95)) if best_det >= 0.85 else ceiling
    # 最终取 min(ceiling, max(raw, best_det)) 的语义：报告侧用 ceiling 约束展示
    return round(min(1.0, max(ceiling, 0.0)), 3)


def _ceiling_note(
    ceiling: float,
    raw: float,
    missing: List[str],
    deterministic_facts: List[Dict[str, Any]],
) -> str:
    if deterministic_facts and any(float(f.get("confidence") or 0) >= 0.95 for f in deterministic_facts):
        return f"存在高置信确定性事实；报告置信度上限约 {ceiling:.0%}"
    if missing:
        return (
            f"缺证: {', '.join(missing[:4])}"
            f"{'…' if len(missing) > 4 else ''}；"
            f"原始分类置信 {raw:.0%}，建议不超过 {ceiling:.0%}"
        )
    return f"证据较全；原始分类置信 {raw:.0%}，上限约 {ceiling:.0%}"


def _is_near_null(addr: str) -> bool:
    try:
        s = str(addr).strip()
        v = int(s, 16) if s.lower().startswith("0x") else int(s, 0)
        return 0 <= v < 0x1000
    except (ValueError, TypeError):
        return False


def _addr_eq(a: str, b: str) -> bool:
    try:
        def _n(x: str) -> int:
            s = str(x).strip().lower().replace("0x", "")
            return int(s, 16)
        return _n(a) == _n(b)
    except (ValueError, TypeError):
        return False
