#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""寄存器状态关联分析。

从 crash log 中提取寄存器 dump，并与崩溃地址做关联分析，
写入 ``01_crash_log_parser.json`` 的 ``registers`` 字段。

可选：结合 Maps 生成 ``address_map``（仅关键寄存器的 VA 分类，非全文 maps）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from tools.crash_parser.address_pattern_analyzer import analyze_crash_address
from tools.crash_parser.memory_maps import (
    MapEntry,
    classify_mapped_kind,
    lookup_va,
    parse_memory_maps,
    so_load_base,
    so_relative_offset,
)

# 始终纳入 address_map 的控制类寄存器（选择逻辑见 _select_address_map_names）
_MAX_EXTRA_CODE_REGS = 4


@dataclass
class RegisterValue:
    """单个寄存器值。"""

    name: str
    value: int
    hex_str: str
    is_null: bool = False
    is_uaf_pattern: bool = False
    matches_crash_addr: bool = False
    note: str = ""


@dataclass
class RegisterAnalysis:
    """寄存器分析结果。"""

    registers: List[RegisterValue] = field(default_factory=list)
    arch: str = ""
    has_null: bool = False
    has_uaf_pattern: bool = False
    crash_addr_source: str = ""
    evidence_notes: List[str] = field(default_factory=list)
    address_map: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # basename → load-base hex，供 meta_info.module_base_addresses 合并
    referenced_module_bases: Dict[str, str] = field(default_factory=dict)

    def to_report_dict(self) -> Optional[Dict[str, Any]]:
        """供 01 JSON 使用的精炼结构；无寄存器时返回 None。"""
        if not self.registers:
            return None
        values = {r.name: r.hex_str for r in self.registers}
        notable = [
            {"name": r.name, "value": r.hex_str, "note": r.note}
            for r in self.registers
            if r.note
        ]
        out: Dict[str, Any] = {
            "arch": self.arch or _infer_arch(list(values.keys())),
            "values": values,
            "notable": notable,
            "analysis": {
                "has_null": self.has_null,
                "has_uaf_pattern": self.has_uaf_pattern,
                "crash_addr_source": self.crash_addr_source or None,
                "evidence_notes": list(self.evidence_notes),
            },
        }
        if self.address_map:
            cleaned_map: Dict[str, Dict[str, Any]] = {}
            for name, info in self.address_map.items():
                cleaned_map[name] = {k: v for k, v in info.items() if v is not None}
            out["address_map"] = cleaned_map
        return out


# Harmony / Android tombstone: "Registers:" … until next known section
_REGISTERS_SECTION_RE = re.compile(
    r"(?ims)^Registers:\s*\n(.*?)(?=^"
    r"(?:Other thread info:|Memory near registers:|FaultStack:|Maps:|OpenFiles:|HiLog:"
    r"|Binary Images:|=======|-----)"
    r"|\Z)",
)

# Apple .crash: "Thread State" / "ARM Thread State"
_THREAD_STATE_SECTION_RE = re.compile(
    r"(?ims)^(?:ARM |x86_64 )?Thread State[^\n]*:\s*\n(.*?)(?=^"
    r"(?:Binary Images:|Thread \d+|Crashing Thread:|=======|-----)"
    r"|\Z)",
)

# ARM64 dump styles:
#   "x0:0000000000000000 x1:0000007f..."   (Harmony)
#   "x0: 0x0000000000000000   x1: 0x..."  (iOS)
#   "x0  0000000000000000  x1  0000007b..." (Android)
_ARM64_PAIR_RE = re.compile(
    r"\b(x\d{1,2}|fp|lr|sp|pc|cpsr)\s*[:=]\s*(?:0x)?([0-9a-fA-F]{8,16})\b",
    re.IGNORECASE,
)
_ARM64_SPACE_RE = re.compile(
    r"\b(x\d{1,2}|fp|lr|sp|pc|cpsr)\s+([0-9a-fA-F]{8,16})\b",
    re.IGNORECASE,
)

_ARM32_PAIR_RE = re.compile(
    r"\b(r\d{1,2}|fp|ip|sp|lr|pc|cpsr)\s*[:=]\s*(?:0x)?([0-9a-fA-F]{8})\b",
    re.IGNORECASE,
)
_ARM32_SPACE_RE = re.compile(
    r"\b(r\d{1,2}|fp|ip|sp|lr|pc|cpsr)\s+([0-9a-fA-F]{8})\b",
    re.IGNORECASE,
)

_X86_64_RE = re.compile(
    r"\b(rax|rbx|rcx|rdx|rsi|rdi|rbp|rsp|r\d{1,2}|rip|rflags)\s*[:=]\s*(?:0x)?([0-9a-fA-F]{8,16})\b",
    re.IGNORECASE,
)

# Stack lines like "#00 pc 000000000038620c /path/lib.so" must not feed register dump
_STACK_PC_LINE_RE = re.compile(r"^\s*#?\d+\s+pc\s+", re.IGNORECASE | re.MULTILINE)


def _infer_arch(names: List[str]) -> str:
    lowered = {n.lower() for n in names}
    if any(n.startswith("x") and n[1:].isdigit() for n in lowered) or {"lr", "pc", "sp"} <= lowered:
        if any(n.startswith("r") and n[1:].isdigit() and int(n[1:]) <= 15 for n in lowered) and not any(
            n.startswith("x") and n[1:].isdigit() for n in lowered
        ):
            return "arm32"
        return "arm64"
    if {"rax", "rbx", "rip", "rsp"} & lowered or any(n.startswith("r") and n[1:].isdigit() for n in lowered):
        return "x86_64"
    return "unknown"


def _parse_pairs(text: str, patterns: List[re.Pattern]) -> Dict[str, RegisterValue]:
    registers: Dict[str, RegisterValue] = {}
    for pattern in patterns:
        for name, hex_val in pattern.findall(text):
            name_lower = name.lower()
            if name_lower in registers:
                continue
            try:
                val = int(hex_val, 16)
            except ValueError:
                continue
            registers[name_lower] = RegisterValue(
                name=name_lower,
                value=val,
                hex_str=f"0x{hex_val.lower()}",
                is_null=(val == 0),
                is_uaf_pattern=_is_uaf_pattern(val),
            )
        # Prefer first pattern family that yields a real dump (>=4 regs)
        if len(registers) >= 4:
            break
        registers.clear()
    return registers


def _extract_register_blob(crash_log_content: str) -> str:
    """Prefer dedicated register sections; fall back to filtered full text."""
    for pattern in (_REGISTERS_SECTION_RE, _THREAD_STATE_SECTION_RE):
        m = pattern.search(crash_log_content)
        if m and m.group(1).strip():
            return m.group(1)
    # Drop stack backtrace lines that contain "pc <offset>"
    return _STACK_PC_LINE_RE.sub("", crash_log_content)


def extract_registers(crash_log_content: str) -> List[RegisterValue]:
    """从 crash log 中提取寄存器值。"""
    if not crash_log_content:
        return []

    blob = _extract_register_blob(crash_log_content)
    if not blob.strip():
        return []

    for patterns in (
        [_ARM64_PAIR_RE, _ARM64_SPACE_RE],
        [_ARM32_PAIR_RE, _ARM32_SPACE_RE],
        [_X86_64_RE],
    ):
        registers = _parse_pairs(blob, patterns)
        if len(registers) >= 4:
            return list(registers.values())
    return []


def analyze_registers(
    crash_log_content: str,
    crash_address: Optional[str] = None,
    *,
    map_entries: Optional[List[MapEntry]] = None,
) -> RegisterAnalysis:
    """分析寄存器状态与崩溃地址的关联，并生成 address_map。"""
    result = RegisterAnalysis()
    regs = extract_registers(crash_log_content)
    if not regs:
        return result

    result.registers = regs
    result.arch = _infer_arch([r.name for r in regs])

    crash_addr_val: Optional[int] = None
    if crash_address:
        try:
            addr_str = str(crash_address).strip()
            crash_addr_val = int(addr_str, 16) if addr_str.lower().startswith("0x") else int(addr_str, 0)
        except (ValueError, TypeError):
            crash_addr_val = None

    null_regs: List[str] = []
    uaf_regs: List[str] = []
    crash_source_regs: List[str] = []

    for reg in regs:
        if reg.is_null:
            null_regs.append(reg.name)
            reg.note = "NULL (0x0)"
        if reg.is_uaf_pattern:
            uaf_regs.append(reg.name)
            reg.note = "UAF 特征值 (含 0x6b 模式)"
        if crash_addr_val is None:
            continue
        # Include fault addr 0x0: NULL registers are the access-source candidates
        if reg.value == crash_addr_val:
            crash_source_regs.append(reg.name)
            reg.matches_crash_addr = True
            reg.note = "与崩溃地址一致" if crash_addr_val != 0 else "与崩溃地址一致（均为 NULL）"
        elif (
            reg.value != 0
            and crash_addr_val != 0
            and (reg.value & ((1 << 48) - 1)) == (crash_addr_val & ((1 << 48) - 1))
        ):
            # Harmony may print fault as 0x005555... while reg is 5555...
            crash_source_regs.append(reg.name)
            reg.matches_crash_addr = True
            reg.note = "与崩溃地址一致（低位匹配）"

    if null_regs:
        result.has_null = True
        result.evidence_notes.append(
            f"寄存器 {', '.join(null_regs[:5])} 为 NULL → 空指针来源线索"
        )
    if uaf_regs:
        result.has_uaf_pattern = True
        result.evidence_notes.append(
            f"寄存器 {', '.join(uaf_regs[:3])} 含 0x6b 模式 → UAF 佐证"
        )
    if crash_source_regs:
        # Prefer common argument/result registers when multiple match (esp. fault=0)
        preferred = ("x0", "r0", "rax", "rdi", "x1", "r1")
        chosen = next((n for n in preferred if n in crash_source_regs), crash_source_regs[0])
        result.crash_addr_source = chosen
        if crash_addr_val == 0:
            result.evidence_notes.append(
                f"寄存器 {chosen} 为 NULL 且 fault addr 为 0x0 → 该寄存器为访问来源"
            )
        else:
            result.evidence_notes.append(
                f"寄存器 {chosen} 值等于崩溃地址 → 该寄存器为访问来源"
            )

    maps = map_entries if map_entries is not None else parse_memory_maps(crash_log_content or "")
    address_map, module_bases, map_notes = _build_address_map(
        regs,
        maps,
        crash_addr_source=result.crash_addr_source,
    )
    result.address_map = address_map
    result.referenced_module_bases = module_bases
    result.evidence_notes.extend(map_notes)

    return result


def build_registers_section(
    crash_log_content: str,
    crash_address: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, str]]:
    """构建 registers 字典，并返回应合并进 meta 的 module bases。

    Returns:
        (registers_dict_or_None, referenced_module_bases)
    """
    analysis = analyze_registers(crash_log_content, crash_address)
    return analysis.to_report_dict(), dict(analysis.referenced_module_bases)


def enrich_registers_from_backtrace(
    registers: Optional[Dict[str, Any]],
    threads: List[Any],
) -> Tuple[Optional[Dict[str, Any]], Dict[str, str]]:
    """无 Maps 时，用崩溃线程 #00 推装载基址，回填 pc/lr 的 module+offset。

    规则（Android tombstone 惯例）：
    - ``pc`` ← #00 的 module + 相对 offset（不覆盖已有 kind=code）
    - ``base = pc_va - #00_offset``
    - ``lr`` ← 同一 module，offset=``lr_va - base``（**不是** #01）
    - ``sp``/``x29`` 仅在仍为 unknown 时标为 stack（启发式）

    Returns:
        (updated_registers_or_same, extra_module_bases)
    """
    extra_bases: Dict[str, str] = {}
    if not isinstance(registers, dict):
        return registers, extra_bases

    address_map = registers.get("address_map")
    values = registers.get("values")
    if not isinstance(address_map, dict) or not isinstance(values, dict):
        return registers, extra_bases

    frame0 = _pick_crash_thread_frame0(threads)
    if not frame0:
        # 仍可给 sp 标 stack
        _mark_stack_regs_heuristic(address_map)
        registers["address_map"] = _clean_address_map(address_map)
        return registers, extra_bases

    module = str(frame0.get("module") or "").strip()
    module_path = None
    # frame.module 有时是 basename；保留 basename
    module_base_name = os.path.basename(module) if module else ""
    if module.startswith("/"):
        module_path = module
        module_base_name = os.path.basename(module)

    frame_off = _parse_hex_int(frame0.get("offset") or frame0.get("address"))
    pc_va = _parse_hex_int(values.get("pc") or values.get("rip") or values.get("eip"))
    if not module_base_name or frame_off is None or pc_va is None:
        _mark_stack_regs_heuristic(address_map)
        registers["address_map"] = _clean_address_map(address_map)
        return registers, extra_bases

    if not _looks_like_relative_offset(frame_off, pc_va):
        _mark_stack_regs_heuristic(address_map)
        registers["address_map"] = _clean_address_map(address_map)
        return registers, extra_bases

    load_base = pc_va - frame_off
    if load_base < 0:
        _mark_stack_regs_heuristic(address_map)
        registers["address_map"] = _clean_address_map(address_map)
        return registers, extra_bases

    notes: List[str] = []
    analysis = registers.get("analysis")
    if not isinstance(analysis, dict):
        analysis = {}
        registers["analysis"] = analysis
    evidence = list(analysis.get("evidence_notes") or [])
    applied = False

    # --- pc ---
    pc_info = address_map.get("pc") or address_map.get("rip") or address_map.get("eip")
    pc_key = "pc" if "pc" in address_map or values.get("pc") is not None else (
        "rip" if "rip" in address_map or values.get("rip") is not None else "eip"
    )
    if _needs_backtrace_code_fill(pc_info if isinstance(pc_info, dict) else None):
        address_map[pc_key] = {
            "va": _fmt_va(pc_va),
            "mapped": True,
            "kind": "code",
            "module": module_base_name,
            "offset": f"0x{frame_off:x}",
            "source": "backtrace_frame0",
        }
        if module_path:
            address_map[pc_key]["module_path"] = module_path
        notes.append(
            f"无 Maps：用崩溃线程 #00 回填 {pc_key} → {module_base_name}+0x{frame_off:x}"
        )
        applied = True

    # --- lr（同基址推算，非 #01）---
    lr_va = _parse_hex_int(values.get("lr"))
    if lr_va is not None and _needs_backtrace_code_fill(
        address_map.get("lr") if isinstance(address_map.get("lr"), dict) else None
    ):
        lr_off = lr_va - load_base
        if _offset_plausible_in_module(lr_off, frame_off, threads, module_base_name):
            address_map["lr"] = {
                "va": _fmt_va(lr_va),
                "mapped": True,
                "kind": "code",
                "module": module_base_name,
                "offset": f"0x{lr_off:x}",
                "source": "backtrace_load_base",
            }
            if module_path:
                address_map["lr"]["module_path"] = module_path
            notes.append(
                f"无 Maps：用 #00 推算装载基址回填 lr → {module_base_name}+0x{lr_off:x}"
            )
            applied = True

    if applied:
        extra_bases[module_base_name] = f"0x{load_base:x}"

    _mark_stack_regs_heuristic(address_map)

    for n in notes:
        if n not in evidence:
            evidence.append(n)
    code_bits = []
    for n in ("pc", "lr", "rip"):
        info = address_map.get(n)
        if isinstance(info, dict) and info.get("kind") == "code" and info.get("module"):
            code_bits.append(f"{n}∈{info['module']}")
    if code_bits:
        summary = "代码指针寄存器: " + ", ".join(code_bits[:3])
        evidence = [e for e in evidence if not str(e).startswith("代码指针寄存器:")]
        evidence.append(summary)
    analysis["evidence_notes"] = evidence
    registers["address_map"] = _clean_address_map(address_map)
    return registers, extra_bases


def _clean_address_map(address_map: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {
        name: {k: v for k, v in info.items() if v is not None}
        for name, info in address_map.items()
        if isinstance(info, dict)
    }


def _needs_backtrace_code_fill(info: Optional[Dict[str, Any]]) -> bool:
    if not info:
        return True
    if info.get("kind") == "code" and info.get("module") and info.get("offset"):
        return False
    return info.get("kind") in (None, "unknown", "unmapped")


def _mark_stack_regs_heuristic(address_map: Dict[str, Dict[str, Any]]) -> None:
    for name in ("sp", "rsp", "esp", "x29", "fp", "rbp", "ebp"):
        info = address_map.get(name)
        if not isinstance(info, dict):
            continue
        if info.get("kind") in (None, "unknown"):
            info["kind"] = "stack"
            info.setdefault("source", "register_name_heuristic")


def _pick_crash_thread_frame0(threads: List[Any]) -> Optional[Dict[str, Any]]:
    if not threads:
        return None
    ordered = []
    for t in threads:
        if hasattr(t, "frames"):
            is_crash = bool(getattr(t, "is_crash_thread", False))
            frames = getattr(t, "frames", None) or []
            frame_dicts = []
            for f in frames:
                if hasattr(f, "address"):
                    frame_dicts.append(
                        {
                            "address": getattr(f, "address", None),
                            "offset": getattr(f, "offset", None),
                            "module": getattr(f, "module", None),
                            "frame_number": getattr(f, "frame_number", None),
                        }
                    )
                elif isinstance(f, dict):
                    frame_dicts.append(f)
            ordered.append((is_crash, frame_dicts))
        elif isinstance(t, dict):
            ordered.append((bool(t.get("is_crash_thread")), t.get("frames") or []))
    # Prefer crash thread
    ordered.sort(key=lambda x: (not x[0],))
    for _is_crash, frames in ordered:
        if not frames:
            continue
        # Prefer frame_number==0 else first
        for f in frames:
            if isinstance(f, dict) and f.get("frame_number") == 0:
                return f
        f0 = frames[0]
        return f0 if isinstance(f0, dict) else None
    return None


def _parse_hex_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        if s.lower().startswith("0x"):
            return int(s, 16)
        if re.fullmatch(r"[0-9a-fA-F]+", s):
            return int(s, 16)
        return int(s, 0)
    except (ValueError, TypeError):
        return None


def _fmt_va(value: int) -> str:
    return f"0x{value:016x}" if value > 0xFFFFFFFF else f"0x{value:x}"


def _looks_like_relative_offset(frame_off: int, pc_va: int) -> bool:
    """判断栈帧 address 更像模块内相对偏移，而非绝对 VA。"""
    if frame_off < 0:
        return False
    # 典型 tombstone 相对 pc
    if frame_off <= 0x0FFFFFFF and frame_off < pc_va:
        return True
    # 若帧地址≈pc，则为绝对地址，无法单靠 #00 得 file offset
    if abs(frame_off - pc_va) <= 0x10:
        return False
    return frame_off < pc_va and (pc_va - frame_off) >= 0x10000


def _offset_plausible_in_module(
    lr_off: int,
    frame0_off: int,
    threads: List[Any],
    module_base_name: str,
) -> bool:
    if lr_off < 0:
        return False
    # 宽松上限：512MB 文本段
    if lr_off > 0x20000000:
        return False
    # 与 #00 同量级，或落在同模块其它帧偏移附近
    if abs(lr_off - frame0_off) <= 0x100000:  # 1MB
        return True
    same_mod_offs: List[int] = []
    for t in threads:
        frames = getattr(t, "frames", None) if hasattr(t, "frames") else (
            t.get("frames") if isinstance(t, dict) else None
        )
        if not frames:
            continue
        for f in frames:
            if hasattr(f, "module"):
                mod = getattr(f, "module", None)
                addr = getattr(f, "offset", None) or getattr(f, "address", None)
            elif isinstance(f, dict):
                mod = f.get("module")
                addr = f.get("offset") or f.get("address")
            else:
                continue
            if not mod:
                continue
            if os.path.basename(str(mod)) != module_base_name:
                continue
            off = _parse_hex_int(addr)
            if off is not None:
                same_mod_offs.append(off)
    if not same_mod_offs:
        return lr_off <= max(frame0_off * 2, 0x1000000)
    lo, hi = min(same_mod_offs), max(same_mod_offs)
    margin = max(0x100000, (hi - lo) // 2)
    return (lo - margin) <= lr_off <= (hi + margin)


def _is_uaf_pattern(value: int) -> bool:
    """检查值是否含有 0x6b (freed memory fill) 模式。"""
    if value == 0:
        return False
    hex_str = f"{value:016x}"
    return hex_str.count("6b") >= 3


def _value_kind_without_maps(value: int) -> Tuple[str, Optional[str]]:
    """无 maps 时的粗分类：(kind, pattern_or_None)。"""
    if value == 0:
        return "null", None
    pat = analyze_crash_address(f"0x{value:x}")
    pattern = str(pat.get("pattern") or "")
    if pattern in ("null_pointer", "null_pointer_offset"):
        return "null", pattern
    if pattern in (
        "repeating_fill",
        "use_after_free_fill",
        "use_after_free_partial",
        "debug_poison",
    ) or pattern.endswith("_poison"):
        return "poison_or_fill", pattern
    if pattern == "stack_region":
        return "stack", pattern
    return "unknown", None


def _classify_register_va(
    name: str,
    value: int,
    maps: List[MapEntry],
) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "va": f"0x{value:016x}" if value > 0xFFFFFFFF else f"0x{value:x}",
    }
    if value == 0:
        entry.update({"mapped": False, "kind": "null"})
        return entry

    kind_wo, pattern = _value_kind_without_maps(value)
    if kind_wo == "poison_or_fill":
        entry.update(
            {
                "mapped": False,
                "kind": "poison_or_fill",
                "pattern": pattern,
            }
        )
        # Still try maps — poison usually unmapped
        hit = lookup_va(maps, value) if maps else None
        if hit is not None:
            entry["mapped"] = True
            entry["perm"] = hit.perms
            entry["module"] = hit.basename or None
            entry["module_path"] = hit.path or None
        return entry

    if kind_wo == "null":
        entry.update({"mapped": False, "kind": "null", "pattern": pattern})
        return entry

    if not maps:
        entry.update({"mapped": None, "kind": kind_wo if kind_wo != "unknown" else "unknown"})
        if pattern:
            entry["pattern"] = pattern
        return entry

    hit = lookup_va(maps, value)
    if hit is None:
        # sp/rsp often in regions not labeled; keep stack hint by register name
        if name.lower() in ("sp", "rsp", "esp") or kind_wo == "stack":
            entry.update({"mapped": False, "kind": "stack"})
        else:
            entry.update({"mapped": False, "kind": "unmapped"})
        return entry

    kind = classify_mapped_kind(hit, special_name=name)
    entry.update(
        {
            "mapped": True,
            "kind": kind,
            "perm": hit.perms,
            "module": hit.basename or None,
            "module_path": hit.path or None,
        }
    )
    if kind == "code" or hit.executable:
        off = so_relative_offset(maps, hit, value)
        if off >= 0:
            entry["offset"] = f"0x{off:x}"
            entry["kind"] = "code"
    return entry


def _select_address_map_names(
    regs: List[RegisterValue],
    maps: List[MapEntry],
    *,
    crash_addr_source: str,
) -> List[str]:
    by_name = {r.name: r for r in regs}
    ordered: List[str] = []
    seen: Set[str] = set()

    def _add(n: str) -> None:
        if n in by_name and n not in seen:
            seen.add(n)
            ordered.append(n)

    for n in ("pc", "lr", "rip", "eip", "sp", "rsp", "esp", "fp", "x29", "rbp", "ebp"):
        _add(n)
    if crash_addr_source:
        _add(crash_addr_source)
    for r in regs:
        if r.note:
            _add(r.name)

    # Extra code-pointer candidates from maps (e.g. x22 near pc)
    if maps:
        extras = 0
        for r in regs:
            if r.name in seen or r.value == 0:
                continue
            kind_wo, _ = _value_kind_without_maps(r.value)
            if kind_wo in ("null", "poison_or_fill"):
                continue
            hit = lookup_va(maps, r.value)
            if hit is not None and hit.executable:
                _add(r.name)
                extras += 1
                if extras >= _MAX_EXTRA_CODE_REGS:
                    break
    return ordered


def _build_address_map(
    regs: List[RegisterValue],
    maps: List[MapEntry],
    *,
    crash_addr_source: str,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str], List[str]]:
    names = _select_address_map_names(
        regs, maps, crash_addr_source=crash_addr_source
    )
    address_map: Dict[str, Dict[str, Any]] = {}
    module_bases: Dict[str, str] = {}
    notes: List[str] = []

    for name in names:
        reg = next(r for r in regs if r.name == name)
        info = _classify_register_va(name, reg.value, maps)
        address_map[name] = info
        if info.get("kind") == "code" and info.get("module") and info.get("offset"):
            hit = lookup_va(maps, reg.value) if maps else None
            if hit is not None:
                base = so_load_base(maps, hit)
                module_bases.setdefault(hit.basename, f"0x{base:x}")

    # Compact evidence from address_map
    code_regs = [n for n, i in address_map.items() if i.get("kind") == "code"]
    if code_regs:
        mods = []
        for n in code_regs[:3]:
            m = address_map[n].get("module")
            if m:
                mods.append(f"{n}∈{m}")
        if mods:
            notes.append("代码指针寄存器: " + ", ".join(mods))
    src = crash_addr_source
    if src and src in address_map:
        sk = address_map[src].get("kind")
        if sk in ("poison_or_fill", "unmapped", "null"):
            notes.append(
                f"访问来源寄存器 {src} 分类为 {sk} → 非法/损坏地址访问佐证"
            )
    return address_map, module_bases, notes
