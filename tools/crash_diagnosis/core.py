#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""崩溃诊断编排入口 — 组合各分析模块，产出完整诊断结果。"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from tools.crash_diagnosis.classifier import classify_crash
from tools.crash_diagnosis.correlation import analyze_register_correlation
from tools.crash_diagnosis.fault_analysis import analyze_fault_register
from tools.crash_diagnosis.fp_analysis import analyze_fp
from tools.crash_diagnosis.pc_lr_analysis import analyze_pc_lr
from tools.crash_diagnosis.prompt_builder import build_diagnosis_prompt_section
from tools.crash_diagnosis.sp_analysis import analyze_sp
from tools.crash_diagnosis.types import (
    CrashClassification,
    CrashDiagnosis,
    FaultAnalysis,
    FpAnalysis,
    PcLrAnalysis,
    RegisterCorrelation,
    SpAnalysis,
)

logger = logging.getLogger(__name__)


def run_crash_diagnosis(
    parse_result: Dict[str, Any],
    memory_maps_data: Dict[str, Any],
    resolved_stack: Dict[str, Any],
    crash_log_content: str = "",
    library_dir: str = "",
    force_disassembly: bool = False,
) -> Dict[str, Any]:
    """执行完整崩溃诊断流程。

    Args:
        parse_result: 01 的输出（crash_info, registers 等）
        memory_maps_data: 02_memory_maps 的输出（entries, stack_regions）
        resolved_stack: 03_add2line_resolver 的输出（resolved_registers 等）
        crash_log_content: 原始崩溃日志（供 DeterministicAnalyzer 检测 ASan 等）
        library_dir: 库目录（可选反汇编）
        force_disassembly: 强制尝试反汇编（跳过软触发，仍需硬前置）

    Returns:
        04a_crash_diagnosis.json 对应的字典

    即使日志无寄存器 / Maps，也会基于 crash_info + 符号化栈产出非空总结，
    避免 04a 整文件缺失。确定性规则结论写入 ``deterministic_facts``，
    并合并进 ``prompt_section_zh``（不再在 workflow 旁路重复注入）。
    """
    if not isinstance(parse_result, dict):
        parse_result = {}
    if not isinstance(memory_maps_data, dict):
        memory_maps_data = {}
    if not isinstance(resolved_stack, dict):
        resolved_stack = {}
    if not crash_log_content:
        crash_log_content = str(parse_result.get("raw_content") or "")

    crash_info = parse_result.get("crash_info") or {}
    if not isinstance(crash_info, dict):
        crash_info = {}
    meta_info = parse_result.get("meta_info") or {}
    if not isinstance(meta_info, dict):
        meta_info = {}
    registers = parse_result.get("registers") or {}
    if not isinstance(registers, dict):
        registers = {}
    stack_regions = memory_maps_data.get("stack_regions") or []
    map_entries = memory_maps_data.get("entries") or []
    resolved_registers = resolved_stack.get("resolved_registers")
    arch = _detect_arch(registers, parse_result)
    stack_summary = _extract_stack_summary(resolved_stack, parse_result)
    data_availability = {
        "has_registers": bool(registers.get("values")),
        "has_memory_maps": bool(map_entries) or bool(memory_maps_data.get("maps_present")),
        "has_resolved_stack": bool(stack_summary.get("top_frames")),
        "has_resolved_registers": isinstance(resolved_registers, dict) and bool(resolved_registers),
    }

    # --- 运行各分析模块 ---
    sp_result = analyze_sp(registers, stack_regions, arch)
    sp_value_int = _parse_hex(sp_result.sp_value)

    fp_result = analyze_fp(registers, stack_regions, sp_value_int, arch)
    pc_lr_result = analyze_pc_lr(registers, resolved_registers, map_entries)
    fault_result = analyze_fault_register(crash_info, registers, arch)
    corr_result = analyze_register_correlation(registers, crash_info)

    # crash_address 若与 #00 帧地址相同，多半是 PC 而非 fault addr（常见于精简 mac 日志）
    _annotate_crash_addr_vs_pc(fault_result, stack_summary)

    # --- 确定性前置规则（DeterministicAnalyzer）---
    deterministic_facts = _run_deterministic_analyzer(
        parse_result, resolved_stack, crash_log_content
    )

    # --- 综合分类（寄存器不足时回退到信号+栈符号；高置信确定性事实可锚定）---
    classification = classify_crash(
        sp_result, fp_result, pc_lr_result, fault_result, corr_result, crash_info
    )
    _enrich_classification_from_stack(classification, crash_info, stack_summary)
    _apply_deterministic_to_classification(classification, deterministic_facts)
    _align_weak_deterministic_with_classification(
        classification, deterministic_facts, stack_summary, crash_info
    )

    # --- 构建证据链（不含 deterministic 条目，避免与 deterministic_facts 字段重复）---
    evidence_chain = _build_evidence_chain(
        crash_info, sp_result, fp_result, pc_lr_result, fault_result, corr_result,
        stack_summary=stack_summary,
        data_availability=data_availability,
        meta_info=meta_info,
    )

    # --- 可选反汇编（硬前置 + 软触发；结果写入 04a.disassembly）---
    disassembly: Dict[str, Any] = {}
    try:
        from tools.crash_diagnosis.disassembly_gate import maybe_run_disassembly
        disassembly = maybe_run_disassembly(
            parse_result=parse_result,
            resolved_stack=resolved_stack,
            stack_summary=stack_summary,
            classification=_to_clean_dict(classification),
            deterministic_facts=deterministic_facts,
            fault_pattern=str(fault_result.pattern or ""),
            data_availability=data_availability,
            library_dir=library_dir or str(resolved_stack.get("library_path") or ""),
            force=bool(force_disassembly),
        )
    except Exception as dis_exc:
        logger.debug("disassembly gate skipped: %s", dis_exc)
        disassembly = {
            "triggered": False,
            "skip_reason": "gate_error",
            "skip_detail": str(dis_exc),
            "trigger_reasons": [],
            "result": None,
            "markdown": "",
        }

    # --- 证据罗盘（完备度 / 缺证 / 置信度上限）---
    from tools.crash_diagnosis.evidence_compass import (
        build_evidence_compass,
        enrich_data_availability,
    )
    evidence_compass = build_evidence_compass(
        crash_info=crash_info,
        stack_summary=stack_summary,
        data_availability=data_availability,
        fault_notes=list(getattr(fault_result, "notes", None) or []),
        fault_address=getattr(fault_result, "crash_address", None),
        fault_pattern=getattr(fault_result, "pattern", None),
        pc_value=getattr(pc_lr_result, "pc_value", None),
        classification=_to_clean_dict(classification),
        deterministic_facts=deterministic_facts,
        disassembly=disassembly,
    )
    data_availability = enrich_data_availability(
        data_availability,
        stack_summary=stack_summary,
        disassembly=disassembly,
        pc_vs_fault=str(
            (evidence_compass.get("layers") or {})
            .get("location_pc", {})
            .get("pc_vs_fault")
            or "unknown"
        ),
    )
    # 用完备度上限约束展示用置信度（不改写原始分类字段以外的语义）
    ceiling = float(evidence_compass.get("confidence_ceiling") or classification.confidence)
    if classification.confidence > ceiling:
        classification.confidence = ceiling

    # --- 生成 prompt 段落（含确定性事实 / 可选反汇编 / 证据罗盘）---
    prompt_section = build_diagnosis_prompt_section(
        classification, sp_result, fp_result, pc_lr_result,
        fault_result, corr_result, evidence_chain,
        data_availability=data_availability,
        stack_summary=stack_summary,
        deterministic_facts=deterministic_facts,
        disassembly=disassembly,
        evidence_compass=evidence_compass,
    )

    # --- 组装输出（无寄存器时仍保留各子模块 notes，便于排障）---
    register_diagnosis: Dict[str, Any] = {
        "sp_analysis": _to_clean_dict(sp_result),
        "fp_analysis": _to_clean_dict(fp_result),
        "pc_lr_analysis": _to_clean_dict(pc_lr_result),
        "fault_register_analysis": _to_clean_dict(fault_result),
        "register_correlation": _to_clean_dict(corr_result),
    }

    return {
        "crash_classification": _to_clean_dict(classification),
        "deterministic_facts": deterministic_facts,
        "evidence_compass": evidence_compass,
        "disassembly": disassembly,
        "register_diagnosis": register_diagnosis,
        "stack_summary": stack_summary or None,
        "data_availability": data_availability,
        "evidence_chain": evidence_chain,
        "prompt_section_zh": prompt_section,
    }


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


_DETERMINISTIC_PATTERN_MAP = {
    "null_pointer": ("null_pointer_dereference", "空指针解引用（确定性规则确认）"),
    "stack_overflow": ("stack_overflow", "栈溢出（确定性规则确认）"),
    "abort": ("explicit_abort", "进程主动 abort（确定性规则确认）"),
    "heap_abort": ("heap_corruption", "堆分配器检出损坏（确定性规则确认）"),
    "detector_report": ("detector_confirmed", "检测器报告（确定性规则确认，请直接采信）"),
    "divide_by_zero": ("divide_by_zero", "算术异常/除零（确定性规则确认）"),
}


def _run_deterministic_analyzer(
    parse_result: Dict[str, Any],
    resolved_stack: Dict[str, Any],
    crash_log_content: str,
) -> List[Dict[str, Any]]:
    """调用 DeterministicAnalyzer，返回可 JSON 序列化的事实列表。"""
    try:
        from rag.deterministic_analyzer import DeterministicAnalyzer
        conclusions = DeterministicAnalyzer().analyze(
            parse_result, resolved_stack, crash_log_content or ""
        )
    except Exception as exc:
        logger.debug("DeterministicAnalyzer skipped inside 04a: %s", exc)
        return []
    facts: List[Dict[str, Any]] = []
    for fact in conclusions.facts or []:
        facts.append({
            "fact_type": fact.fact_type,
            "description": fact.description,
            "confidence": fact.confidence,
            "evidence": fact.evidence,
            "implication": fact.implication,
        })
    return facts


def _apply_deterministic_to_classification(
    classification: CrashClassification,
    deterministic_facts: List[Dict[str, Any]],
) -> None:
    """高置信确定性事实锚定分类，避免与推断结论打架。"""
    if not deterministic_facts:
        return
    # 取置信度最高的一条作为主锚定
    best = max(deterministic_facts, key=lambda f: float(f.get("confidence") or 0.0))
    conf = float(best.get("confidence") or 0.0)
    # fault-addr 级 (≥0.95) 强锚定；符号启发 (≥0.85) 在分类仍弱时也可锚定
    if conf < 0.85:
        return
    if conf < 0.95 and classification.confidence >= 0.80 and classification.primary_pattern not in (
        "unknown", "wild_pointer", "segmentation_fault", "stack_symbol_hint", ""
    ):
        return
    fact_type = str(best.get("fact_type") or "")
    mapped = _DETERMINISTIC_PATTERN_MAP.get(fact_type)
    if not mapped:
        return
    pattern, default_summary = mapped
    prev = classification.primary_pattern
    if prev and prev not in ("unknown", pattern) and classification.confidence >= 0.5:
        # 保留原推断为次要模式，避免信息丢失
        secs = list(classification.secondary_patterns or [])
        if prev not in secs:
            secs.insert(0, prev)
        classification.secondary_patterns = secs[:4]
    classification.primary_pattern = pattern
    classification.confidence = max(float(classification.confidence or 0), conf)
    desc = str(best.get("description") or "").strip()
    classification.summary_zh = desc or default_summary


def _align_weak_deterministic_with_classification(
    classification: CrashClassification,
    deterministic_facts: List[Dict[str, Any]],
    stack_summary: Dict[str, Any],
    crash_info: Dict[str, Any],
) -> None:
    """分类已判空指针但尚无对应 fact 时，补一条对齐事实（避免 04a 两边打架）。"""
    if classification.primary_pattern not in (
        "null_pointer_dereference",
        "null_pointer_member_access",
    ):
        return
    if any(str(f.get("fact_type") or "") == "null_pointer" for f in deterministic_facts):
        return
    crash_fn = str(stack_summary.get("crash_function") or "")
    signal = str(crash_info.get("signal") or "")
    deterministic_facts.append({
        "fact_type": "null_pointer",
        "description": classification.summary_zh or "空指针解引用（分类对齐）",
        "confidence": float(classification.confidence or 0.72),
        "evidence": (
            f"classification={classification.primary_pattern}, "
            f"signal={signal or '?'}, frame={crash_fn or '?'}"
        ),
        "implication": "与 crash_classification 对齐；建议补充 fault_addr/寄存器做交叉验证",
        "source": "classification_alignment",
    })


def _detect_arch(registers: Dict[str, Any], parse_result: Dict[str, Any]) -> str:
    """推断架构。"""
    # 从 registers.arch
    arch = registers.get("arch", "") if isinstance(registers, dict) else ""
    if arch:
        return arch
    # 从 meta_info.arch
    meta = parse_result.get("meta_info") or {}
    meta_arch = str(meta.get("arch") or "").lower()
    if "arm64" in meta_arch or "aarch64" in meta_arch:
        return "arm64"
    if "x86_64" in meta_arch or "amd64" in meta_arch:
        return "x86_64"
    # 从寄存器名推断
    values = registers.get("values", {}) if isinstance(registers, dict) else {}
    if any(k.startswith("x") and k[1:].isdigit() for k in values):
        return "arm64"
    if "rax" in values or "rip" in values:
        return "x86_64"
    return "arm64"  # 默认


def _build_evidence_chain(
    crash_info: Dict[str, Any],
    sp: SpAnalysis,
    fp: FpAnalysis,
    pc_lr: PcLrAnalysis,
    fault: FaultAnalysis,
    corr: RegisterCorrelation,
    stack_summary: Optional[Dict[str, Any]] = None,
    data_availability: Optional[Dict[str, Any]] = None,
    meta_info: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """组装证据链列表。"""
    chain: List[Dict[str, Any]] = []
    stack_summary = stack_summary or {}
    data_availability = data_availability or {}
    meta_info = meta_info or {}

    # 数据完备性（无寄存器时也要说清楚）
    missing: List[str] = []
    if not data_availability.get("has_registers"):
        missing.append("寄存器转储")
    if not data_availability.get("has_memory_maps"):
        missing.append("内存映射(Maps)")
    if missing:
        chain.append({
            "type": "data_gap",
            "finding": f"日志缺少: {', '.join(missing)}",
            "implication": "寄存器/栈健康度结论受限，以下主要依据信号、崩溃地址与符号化调用栈",
        })

    # 平台 / 分类
    platform = meta_info.get("platform") or meta_info.get("os_type") or ""
    category = crash_info.get("category") or ""
    if platform or category:
        chain.append({
            "type": "meta",
            "finding": f"平台={platform or '?'}，分类={category or '?'}",
            "implication": "来自日志解析元信息",
        })

    # 信号语义
    signal = crash_info.get("signal") or ""
    crash_reason = crash_info.get("crash_reason") or ""
    if signal:
        chain.append({
            "type": "signal",
            "finding": f"信号: {signal}",
            "implication": crash_reason or "异常终止",
        })

    # 故障地址
    if fault.crash_address:
        finding = f"故障地址: {fault.crash_address}"
        if fault.source_register:
            finding += f"（来源寄存器: {fault.source_register}）"
        implication = fault.pattern
        if any("与 #00" in n for n in (fault.notes or [])):
            implication = "地址与崩溃帧 PC 一致，更可能是指令地址而非 fault address"
        chain.append({
            "type": "fault_address",
            "finding": finding,
            "implication": implication,
        })

    # 符号化栈顶
    top_frames = stack_summary.get("top_frames") or []
    if top_frames:
        frame0 = top_frames[0]
        func = frame0.get("function") or "?"
        module = frame0.get("module") or "?"
        chain.append({
            "type": "stack_top",
            "finding": f"#00 {func} @ {module}",
            "implication": "崩溃点符号化结果（来自 03）",
        })
        if len(top_frames) > 1:
            callers = " → ".join(
                (f.get("function") or "?") for f in top_frames[1:4]
            )
            chain.append({
                "type": "stack_callers",
                "finding": f"上层调用: {callers}",
                "implication": "辅助判断业务入口与延迟崩溃路径",
            })

    # SP 状态
    if sp.sp_value and sp.stack_overflow_risk != "unknown":
        chain.append({
            "type": "sp_check",
            "finding": f"SP={sp.sp_value}，{'在栈内' if sp.in_stack_region else '超出栈区域'}",
            "implication": f"栈溢出风险: {sp.stack_overflow_risk}",
        })

    # FP 状态
    if fp.fp_value and fp.in_stack_region is not None:
        chain.append({
            "type": "fp_check",
            "finding": f"FP={fp.fp_value}，{'在栈内' if fp.in_stack_region else '不在栈内'}",
            "implication": "帧链正常" if fp.frame_chain_plausible else "帧链可能损坏",
        })

    # UAF
    if corr.uaf_pattern_registers:
        chain.append({
            "type": "uaf_indicator",
            "finding": f"寄存器 {', '.join(corr.uaf_pattern_registers)} 含释放特征值",
            "implication": "疑似 use-after-free",
        })

    # PC 状态
    if pc_lr.pc_in_code_region is not None:
        status = "在代码段" if pc_lr.pc_in_code_region else "不在代码段"
        func_info = ""
        if pc_lr.pc_resolved and pc_lr.pc_resolved.get("function"):
            func_info = f"（{pc_lr.pc_resolved['function']}）"
        chain.append({
            "type": "pc_check",
            "finding": f"PC {status}{func_info}",
            "implication": "执行正常" if pc_lr.pc_in_code_region else "可能执行了损坏的地址",
        })

    return chain


def _extract_stack_summary(
    resolved_stack: Dict[str, Any],
    parse_result: Dict[str, Any],
) -> Dict[str, Any]:
    """从 03（优先）或 01 提取崩溃线程栈顶摘要。"""
    top_frames: List[Dict[str, Any]] = []

    threads = resolved_stack.get("resolved_threads") or []
    crash_thread = None
    for t in threads:
        if isinstance(t, dict) and t.get("is_crash_thread"):
            crash_thread = t
            break
    if crash_thread is None and threads and isinstance(threads[0], dict):
        crash_thread = threads[0]

    if crash_thread:
        for fr in (crash_thread.get("frames") or [])[:5]:
            if not isinstance(fr, dict):
                continue
            top_frames.append({
                "function": fr.get("resolved_function") or fr.get("function"),
                "module": fr.get("module"),
                "address": fr.get("address"),
                "file": fr.get("resolved_file") or fr.get("file"),
                "line": fr.get("resolved_line") or fr.get("line"),
            })

    if not top_frames:
        for t in (parse_result.get("threads") or []):
            if not isinstance(t, dict):
                continue
            if not t.get("is_crash_thread") and top_frames:
                continue
            for fr in (t.get("frames") or [])[:5]:
                if not isinstance(fr, dict):
                    continue
                top_frames.append({
                    "function": fr.get("function") or fr.get("symbol"),
                    "module": fr.get("module") or fr.get("library"),
                    "address": fr.get("address"),
                    "file": fr.get("file"),
                    "line": fr.get("line"),
                })
            if top_frames:
                break

    crash_frame = top_frames[0] if top_frames else None
    return {
        "top_frames": top_frames,
        "crash_function": (crash_frame or {}).get("function"),
        "crash_module": (crash_frame or {}).get("module"),
        "frame_count": (
            resolved_stack.get("frame_count_total")
            or len(top_frames)
            or None
        ),
    }


def _annotate_crash_addr_vs_pc(
    fault: FaultAnalysis,
    stack_summary: Dict[str, Any],
) -> None:
    """若 crash_address 与 #00 地址相同，标注为疑似 PC。"""
    if not fault.crash_address:
        return
    frames = stack_summary.get("top_frames") or []
    if not frames:
        return
    frame0_addr = str((frames[0] or {}).get("address") or "").lower().replace("0x", "")
    crash_addr = str(fault.crash_address).lower().replace("0x", "")
    if frame0_addr and crash_addr and frame0_addr == crash_addr:
        fault.notes.append(
            "崩溃地址与 #00 帧地址一致，更可能是 PC/指令地址而非 fault address"
        )
        if fault.pattern == "unmapped_access":
            fault.pattern = "pc_address_as_crash_addr"
            fault.notes.append(
                "无寄存器时无法做 fault-addr 模式匹配，降级为基于调用栈的分析"
            )


def _enrich_classification_from_stack(
    classification: CrashClassification,
    crash_info: Dict[str, Any],
    stack_summary: Dict[str, Any],
) -> None:
    """寄存器证据不足时，用信号 + 符号化栈符号补充分类。"""
    # 已有高置信寄存器结论则不覆盖
    if classification.confidence >= 0.80 and classification.primary_pattern not in (
        "unknown",
        "wild_pointer",
    ):
        return
    if classification.primary_pattern == "pc_address_as_crash_addr":
        pass  # continue to stack enrichment
    elif classification.primary_pattern not in ("unknown", "wild_pointer", ""):
        if classification.confidence >= 0.70:
            return

    signal = str(crash_info.get("signal") or "").upper()
    reason = str(crash_info.get("crash_reason") or "").lower()
    funcs = " ".join(
        str(f.get("function") or "")
        for f in (stack_summary.get("top_frames") or [])
    ).lower()
    crash_fn = str(stack_summary.get("crash_function") or "")
    crash_mod = str(stack_summary.get("crash_module") or "")

    null_hints = ("nullptr", "null_ptr", "nullpointer", "null_deref", "null deref")
    if any(h in funcs for h in null_hints) and (
        "SIGSEGV" in signal or "SIGBUS" in signal or "segfault" in reason
    ):
        classification.primary_pattern = "null_pointer_dereference"
        classification.confidence = max(classification.confidence, 0.72)
        classification.summary_zh = (
            f"符号化栈含空指针相关符号（{crash_fn or 'crash frame'}），"
            f"结合 {signal or '段错误'} 倾向空指针/非法解引用；"
            "日志无寄存器，无法用 fault_addr 交叉验证"
        )
        return

    if "SIGSEGV" in signal or "segfault" in reason or "segmentation" in reason:
        where = f"{crash_fn} @ {crash_mod}" if crash_fn else "未知帧"
        classification.primary_pattern = "segmentation_fault"
        classification.confidence = max(classification.confidence, 0.55)
        classification.summary_zh = (
            f"SIGSEGV/段错误，崩溃帧: {where}；"
            "缺少寄存器与 Maps，仅基于解析与符号化结果归纳"
        )
        return

    if crash_fn and (
        classification.primary_pattern in ("unknown", "wild_pointer", "pc_address_as_crash_addr")
        or classification.confidence < 0.5
    ):
        classification.primary_pattern = "stack_symbol_hint"
        classification.confidence = max(classification.confidence, 0.45)
        classification.summary_zh = (
            f"依据符号化崩溃帧 {crash_fn} @ {crash_mod or '?'} 给出初步方向；"
            "建议补充含寄存器的完整崩溃日志"
        )


def _to_clean_dict(obj: Any) -> Dict[str, Any]:
    """将 dataclass 转为字典，移除 None 值。"""
    if hasattr(obj, "__dataclass_fields__"):
        d = asdict(obj)
    elif isinstance(obj, dict):
        d = obj
    else:
        return {}
    return {k: v for k, v in d.items() if v is not None and v != [] and v != {}}


def _parse_hex(val: Any) -> Optional[int]:
    if val is None:
        return None
    s = str(val).strip()
    try:
        return int(s, 16) if s.startswith("0x") or s.startswith("0X") else int(s, 16)
    except (ValueError, TypeError):
        return None
