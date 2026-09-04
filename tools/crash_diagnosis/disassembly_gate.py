#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""反汇编触发判定与执行。

在 04a 诊断内按「硬前置 → 跳过已确定 → 软触发」决定是否调用
``tools.disassembly_tool.DisassemblyTool``。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_AMBIGUOUS_FAULT_PATTERNS = frozenset({
    "base_plus_offset",
    "unmapped_access",
    "pc_address_as_crash_addr",
    "unknown",
})

_TRIGGER_CLASSIFICATIONS = frozenset({
    "code_corruption",
    "jit_failure",
    "wild_pointer",
    "invalid_object_access",
})

_SKIP_DETERMINISTIC_TYPES = frozenset({
    "null_pointer",
    "detector_report",
    "abort",
    "divide_by_zero",
})


def maybe_run_disassembly(
    *,
    parse_result: Dict[str, Any],
    resolved_stack: Dict[str, Any],
    stack_summary: Dict[str, Any],
    classification: Dict[str, Any],
    deterministic_facts: List[Dict[str, Any]],
    fault_pattern: str = "",
    data_availability: Optional[Dict[str, Any]] = None,
    library_dir: str = "",
    force: bool = False,
    trace: Any = None,
) -> Dict[str, Any]:
    """评估是否反汇编；需要时执行并返回可写入 04a 的结构。"""
    data_availability = data_availability or {}
    trigger_reasons: List[str] = []

    binary_path, pc_for_tool, resolve_notes = _resolve_binary_and_pc(
        parse_result, resolved_stack, stack_summary, library_dir
    )

    # --- 硬前置 ---
    try:
        from tools.disassembly_tool import DisassemblyTool
        tool = DisassemblyTool()
    except Exception as exc:
        return _skip("tool_import_failed", str(exc), trigger_reasons)

    if not tool.available:
        return _skip("objdump_unavailable", "未找到 llvm-objdump/objdump/otool", trigger_reasons)

    if not library_dir or not os.path.isdir(str(library_dir)):
        return _skip("no_library_dir", "未提供有效 library_dir", trigger_reasons)

    if not binary_path:
        return _skip(
            "binary_not_found",
            "library_dir 中未匹配到崩溃模块二进制",
            trigger_reasons,
            resolve_notes=resolve_notes,
        )

    if not pc_for_tool:
        return _skip(
            "no_pc_offset",
            "无法得到可用于反汇编的 PC/文件偏移",
            trigger_reasons,
            resolve_notes=resolve_notes,
        )

    # --- 已确定则跳过（force 除外）---
    if not force:
        skip_certain = _skip_if_already_certain(
            classification, deterministic_facts, fault_pattern, stack_summary
        )
        if skip_certain:
            return _skip(
                skip_certain,
                "已有高置信确定性/空指针结论，反汇编收益低",
                trigger_reasons,
                resolve_notes=resolve_notes,
                binary_path=binary_path,
                crash_pc=pc_for_tool,
            )

        # --- 软触发 ---
        trigger_reasons = _collect_trigger_reasons(
            parse_result=parse_result,
            resolved_stack=resolved_stack,
            stack_summary=stack_summary,
            classification=classification,
            fault_pattern=fault_pattern,
            data_availability=data_availability,
        )
        if not trigger_reasons:
            return _skip(
                "not_needed",
                "源码/寄存器结论已够用，未满足软触发条件",
                [],
                resolve_notes=resolve_notes,
                binary_path=binary_path,
                crash_pc=pc_for_tool,
            )
    else:
        trigger_reasons = ["force_disassembly"]

    # --- 执行 ---
    try:
        from services.tool_invoke import invoke_tool

        def _disassemble(_name: str, _payload: Dict[str, Any]) -> Dict[str, Any]:
            return {"_disassembly_result": tool.disassemble_around_pc(binary_path, pc_for_tool)}

        out = invoke_tool("disassembly", {}, trace=trace, tool_executor=_disassemble)
        ctx = out.get("_disassembly_result")
        if ctx is None:
            raise TypeError("disassembly result missing")
        result_dict = ctx.to_dict()
        markdown = ctx.render_markdown() if not ctx.error else f"反汇编失败: {ctx.error}"
        return {
            "triggered": True,
            "skip_reason": None,
            "trigger_reasons": trigger_reasons,
            "binary_path": binary_path,
            "crash_pc": pc_for_tool,
            "resolve_notes": resolve_notes,
            "result": result_dict,
            "markdown": markdown,
            "error": ctx.error or None,
        }
    except Exception as exc:
        logger.warning("disassembly execution failed: %s", exc)
        return {
            "triggered": True,
            "skip_reason": None,
            "trigger_reasons": trigger_reasons,
            "binary_path": binary_path,
            "crash_pc": pc_for_tool,
            "resolve_notes": resolve_notes,
            "result": None,
            "markdown": "",
            "error": str(exc),
        }


def _skip(
    reason: str,
    detail: str,
    trigger_reasons: List[str],
    *,
    resolve_notes: Optional[List[str]] = None,
    binary_path: str = "",
    crash_pc: str = "",
) -> Dict[str, Any]:
    return {
        "triggered": False,
        "skip_reason": reason,
        "skip_detail": detail,
        "trigger_reasons": trigger_reasons,
        "binary_path": binary_path or None,
        "crash_pc": crash_pc or None,
        "resolve_notes": resolve_notes or [],
        "result": None,
        "markdown": "",
        "error": None,
    }


def _skip_if_already_certain(
    classification: Dict[str, Any],
    deterministic_facts: List[Dict[str, Any]],
    fault_pattern: str,
    stack_summary: Dict[str, Any],
) -> Optional[str]:
    for fact in deterministic_facts or []:
        if fact.get("fact_type") in _SKIP_DETERMINISTIC_TYPES:
            if float(fact.get("confidence") or 0.0) >= 0.95:
                return f"deterministic_{fact.get('fact_type')}"

    pattern = str(classification.get("primary_pattern") or "")
    conf = float(classification.get("confidence") or 0.0)
    crash_fn = str(stack_summary.get("crash_function") or "").lower()
    null_hint = any(h in crash_fn for h in ("nullptr", "null_ptr", "nullpointer", "null_deref"))

    if fault_pattern == "null_deref" and conf >= 0.90:
        return "fault_null_deref"
    if pattern == "null_pointer_dereference" and conf >= 0.70 and null_hint:
        return "stack_null_symbol"
    if pattern == "null_pointer_dereference" and fault_pattern == "null_deref":
        return "null_pointer_confirmed"
    return None


def _collect_trigger_reasons(
    *,
    parse_result: Dict[str, Any],
    resolved_stack: Dict[str, Any],
    stack_summary: Dict[str, Any],
    classification: Dict[str, Any],
    fault_pattern: str,
    data_availability: Dict[str, Any],
) -> List[str]:
    reasons: List[str] = []

    if not _crash_frame_has_usable_source(resolved_stack, stack_summary):
        reasons.append("no_usable_source_line")

    has_regs = bool(data_availability.get("has_registers"))
    if has_regs and fault_pattern in _AMBIGUOUS_FAULT_PATTERNS:
        reasons.append(f"ambiguous_fault:{fault_pattern or 'unknown'}")

    primary = str(classification.get("primary_pattern") or "")
    if primary in _TRIGGER_CLASSIFICATIONS:
        reasons.append(f"classification:{primary}")

    # 有寄存器但 crash_addr_source 未定
    registers = parse_result.get("registers") or {}
    if isinstance(registers, dict):
        analysis = registers.get("analysis") or {}
        if has_regs and not analysis.get("crash_addr_source"):
            signal = str((parse_result.get("crash_info") or {}).get("signal") or "").upper()
            if "SIGSEGV" in signal or "SIGBUS" in signal or "EXC_BAD_ACCESS" in signal:
                reasons.append("missing_crash_addr_source")

    return reasons


def _crash_frame_has_usable_source(
    resolved_stack: Dict[str, Any],
    stack_summary: Dict[str, Any],
) -> bool:
    frames = stack_summary.get("top_frames") or []
    if frames:
        f0 = frames[0] or {}
        if f0.get("file") and f0.get("line"):
            return True
    # 再扫 resolved_threads #00
    for t in resolved_stack.get("resolved_threads") or []:
        if not isinstance(t, dict):
            continue
        if not t.get("is_crash_thread"):
            continue
        frs = t.get("frames") or []
        if not frs:
            continue
        f0 = frs[0] if isinstance(frs[0], dict) else {}
        if f0.get("resolved_file") and f0.get("resolved_line"):
            return True
        if f0.get("file") and f0.get("line"):
            return True
        break
    return False


def _resolve_binary_and_pc(
    parse_result: Dict[str, Any],
    resolved_stack: Dict[str, Any],
    stack_summary: Dict[str, Any],
    library_dir: str,
) -> Tuple[str, str, List[str]]:
    """解析二进制路径与反汇编用的 PC（优先文件内偏移）。"""
    notes: List[str] = []
    module = ""
    abs_pc: Optional[int] = None
    file_offset: Optional[int] = None

    registers = parse_result.get("registers") or {}
    if isinstance(registers, dict):
        values = registers.get("values") or {}
        address_map = registers.get("address_map") or {}
        for name in ("pc", "rip", "eip"):
            am = address_map.get(name) if isinstance(address_map, dict) else None
            if isinstance(am, dict):
                if am.get("module"):
                    module = str(am.get("module"))
                off = _parse_hex(am.get("offset") or am.get("file_offset"))
                if off is not None:
                    file_offset = off
                    notes.append(f"pc_offset_from_address_map:{name}")
                break
        if abs_pc is None:
            for name in ("pc", "rip", "eip"):
                abs_pc = _parse_hex(values.get(name)) if isinstance(values, dict) else None
                if abs_pc is not None:
                    notes.append(f"pc_va_from_registers:{name}")
                    break

    # resolved_registers
    rr = resolved_stack.get("resolved_registers")
    if isinstance(rr, dict) and not module:
        for name in ("pc", "rip"):
            info = rr.get(name)
            if isinstance(info, dict) and info.get("module"):
                module = str(info.get("module"))
                off = _parse_hex(info.get("offset"))
                if off is not None and file_offset is None:
                    file_offset = off
                    notes.append("pc_offset_from_resolved_registers")
                break

    # stack #00
    frames = stack_summary.get("top_frames") or []
    if frames:
        f0 = frames[0] or {}
        if not module and f0.get("module"):
            module = str(f0.get("module"))
        if abs_pc is None:
            abs_pc = _parse_hex(f0.get("address"))
            if abs_pc is not None:
                notes.append("pc_va_from_stack_top")

    # 从 meta 基址推 offset
    if file_offset is None and abs_pc is not None and module:
        meta = parse_result.get("meta_info") or {}
        bases = meta.get("module_base_addresses") or {}
        if isinstance(bases, dict):
            base = _parse_hex(bases.get(module) or bases.get(Path(module).name))
            if base is not None and abs_pc >= base:
                file_offset = abs_pc - base
                notes.append("pc_offset_from_module_base")

    os_type = str((parse_result.get("meta_info") or {}).get("os_type") or "unknown").lower()
    if os_type in ("mac", "macos", "darwin"):
        os_type = "macos"
    elif os_type in ("harmony", "ohos"):
        os_type = "harmonyos"

    binary_path = ""
    if module and library_dir and os.path.isdir(library_dir):
        try:
            from tools._library_frame_whitelist import (
                find_library_files_in_dir,
                match_libraries_for_module,
            )
            libs = find_library_files_in_dir(library_dir, os_type)
            matches = match_libraries_for_module(module, libs)
            if matches:
                binary_path = str(matches[0])
                notes.append(f"binary_matched:{matches[0].name}")
        except Exception as exc:
            notes.append(f"library_match_error:{exc}")

    pc_for_tool = ""
    if file_offset is not None:
        pc_for_tool = f"0x{file_offset:x}"
    elif abs_pc is not None:
        # 兜底：部分 dump 地址与 objdump 显示一致
        pc_for_tool = f"0x{abs_pc:x}"
        notes.append("using_absolute_pc_fallback")

    return binary_path, pc_for_tool, notes


def _parse_hex(val: Any) -> Optional[int]:
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    try:
        return int(s, 16) if s.lower().startswith("0x") else int(s, 16)
    except (ValueError, TypeError):
        try:
            return int(s, 0)
        except (ValueError, TypeError):
            return None
