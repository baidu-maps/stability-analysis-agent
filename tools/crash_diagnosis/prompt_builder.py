#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""将诊断结果渲染为中文 markdown，注入崩溃分析 prompt。

证据呈现顺序对齐诊断思路：
位置(PC) → 符号/源码 → 反汇编(按需) → 寄存器/数据 → 缺证与建议。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from tools.crash_diagnosis.types import (
    CrashClassification,
    FaultAnalysis,
    FpAnalysis,
    PcLrAnalysis,
    RegisterCorrelation,
    SpAnalysis,
)

_ACCESS_DIR_ZH = {
    "read": "读（load）",
    "write": "写（store）",
    "call": "调用/跳转",
    "unknown": "未知",
}


def build_diagnosis_prompt_section(
    classification: CrashClassification,
    sp: SpAnalysis,
    fp: FpAnalysis,
    pc_lr: PcLrAnalysis,
    fault: FaultAnalysis,
    correlation: RegisterCorrelation,
    evidence_chain: List[Dict[str, Any]],
    data_availability: Optional[Dict[str, Any]] = None,
    stack_summary: Optional[Dict[str, Any]] = None,
    deterministic_facts: Optional[List[Dict[str, Any]]] = None,
    disassembly: Optional[Dict[str, Any]] = None,
    evidence_compass: Optional[Dict[str, Any]] = None,
) -> str:
    """构建崩溃证据诊断提示词段落。"""
    data_availability = data_availability or {}
    stack_summary = stack_summary or {}
    deterministic_facts = deterministic_facts or []
    disassembly = disassembly or {}
    evidence_compass = evidence_compass or {}
    lines: List[str] = []
    lines.append("## 崩溃证据诊断")
    lines.append("")

    # --- 0. 摘要 + 罗盘 ---
    conf_pct = int(classification.confidence * 100)
    ceiling = evidence_compass.get("confidence_ceiling")
    ceiling_s = f"（完备度上限约 {float(ceiling):.0%}）" if ceiling is not None else ""
    lines.append(f"崩溃模式: {classification.summary_zh or '证据不足，待结合源码进一步判断'}")
    lines.append(f"置信度: {conf_pct}%{ceiling_s}")
    if classification.secondary_patterns:
        lines.append(f"次要模式: {', '.join(classification.secondary_patterns)}")
    lines.append("")

    if evidence_compass:
        lines.append("### 证据罗盘（请按此顺序串联根因）")
        for step in evidence_compass.get("analysis_order_zh") or []:
            lines.append(f"- {step}")
        note = evidence_compass.get("confidence_note_zh")
        if note:
            lines.append(f"- 完备度说明: {note}")
        missing = evidence_compass.get("missing_evidence") or []
        if missing:
            lines.append(f"- 缺证清单: {', '.join(str(m) for m in missing)}")
        lines.append("")
        lines.append(
            "分析要求: 结论须引用下列各层证据；缺证项不得假装已验证，"
            "应明确写出「无法交叉验证」。"
        )
        lines.append("")

    # --- 1. 已确认事实 ---
    if deterministic_facts:
        lines.append("### 已确认事实（无需推理）")
        lines.append("以下结论已通过确定性规则验证，请在分析中直接引用，无需重新推导：")
        for i, fact in enumerate(deterministic_facts, 1):
            desc = fact.get("description") or fact.get("fact_type") or "事实"
            conf = float(fact.get("confidence") or 0.0)
            lines.append(f"{i}. **{desc}**（确定性 {conf:.0%}）")
            if fact.get("evidence"):
                lines.append(f"   - 证据: {fact['evidence']}")
            if fact.get("implication"):
                lines.append(f"   - 意义: {fact['implication']}")
        lines.append("")

    # --- 2. 位置：PC / fault ---
    lines.append("### 1) 位置（PC / fault）— 在哪里崩溃")
    loc_bits: List[str] = []
    if pc_lr.pc_value:
        pc_info = ""
        if pc_lr.pc_resolved and pc_lr.pc_resolved.get("function"):
            pc_info = f" → {pc_lr.pc_resolved['function']}"
        elif pc_lr.pc_resolved and pc_lr.pc_resolved.get("module"):
            pc_info = f" → {pc_lr.pc_resolved['module']}+{pc_lr.pc_resolved.get('offset', '?')}"
        code_status = "代码段" if pc_lr.pc_in_code_region else (
            "非代码段（异常）" if pc_lr.pc_in_code_region is False else "段属性未知"
        )
        loc_bits.append(f"- PC: {pc_lr.pc_value}，{code_status}{pc_info}")
    if pc_lr.lr_value:
        lr_info = ""
        if pc_lr.lr_resolved and pc_lr.lr_resolved.get("function"):
            lr_info = f" → {pc_lr.lr_resolved['function']}"
        elif pc_lr.lr_resolved and pc_lr.lr_resolved.get("module"):
            lr_info = f" → {pc_lr.lr_resolved['module']}+{pc_lr.lr_resolved.get('offset', '?')}"
        loc_bits.append(f"- LR: {pc_lr.lr_value}{lr_info}")
    if fault.crash_address:
        fault_detail = f"崩溃/故障地址: {fault.crash_address}"
        if fault.source_register:
            fault_detail += f"（来源: {fault.source_register}={fault.source_register_value}）"
        if fault.base_register:
            fault_detail += f"，基址: {fault.base_register}={fault.base_register_value}"
        if fault.member_offset is not None:
            fault_detail += (
                f"，成员偏移: 0x{fault.member_offset:x}"
                f"（第{fault.member_index_estimate}个{fault.pointer_size}B成员）"
            )
        loc_bits.append(f"- {fault_detail}")
        for n in (fault.notes or [])[:3]:
            loc_bits.append(f"  - 注: {n}")
    layers = (evidence_compass.get("layers") or {}) if evidence_compass else {}
    pc_vs = ((layers.get("location_pc") or {}).get("pc_vs_fault"))
    if pc_vs and pc_vs != "unknown":
        loc_bits.append(f"- PC vs fault 判定: {pc_vs}")
    if not loc_bits:
        loc_bits.append("- （无可用 PC/fault 地址）")
    lines.extend(loc_bits)
    lines.append("")

    # --- 3. 符号 / 源码 ---
    lines.append("### 2) 符号/源码（addr2line）— 对应什么代码")
    top_frames = stack_summary.get("top_frames") or []
    if top_frames:
        for i, fr in enumerate(top_frames[:5]):
            func = fr.get("function") or "?"
            module = fr.get("module") or "?"
            file_line = ""
            if fr.get("file"):
                file_line = f" @ {fr.get('file')}:{fr.get('line') or '?'}"
            lines.append(f"- #{i:02d} {func} @ {module}{file_line}")
        if not data_availability.get("has_source_file_line") and data_availability.get(
            "has_symbolized_function"
        ):
            lines.append("- （有符号名，但无源码文件:行号；完整源码见后续 04b/函数源码节）")
    else:
        lines.append("- （无符号化栈摘要）")
    lines.append("")

    # --- 4. 反汇编 ---
    lines.append("### 3) 指令（反汇编）— CPU 正在做什么")
    if disassembly.get("triggered") and disassembly.get("markdown"):
        reasons = disassembly.get("trigger_reasons") or []
        if reasons:
            lines.append(f"- 触发原因: {', '.join(str(r) for r in reasons)}")
        result = disassembly.get("result") if isinstance(disassembly.get("result"), dict) else {}
        direction = (result or {}).get("access_direction") or ""
        if direction:
            lines.append(
                f"- 访存方向推断: {_ACCESS_DIR_ZH.get(direction, direction)}"
            )
        regs = (result or {}).get("involved_registers") or []
        if regs:
            lines.append(f"- 指令涉及寄存器: {', '.join(str(r) for r in regs)}")
        lines.append("- 请在根因论述中引用下列崩溃 PC 指令（标记 >>>）：")
        lines.append(str(disassembly.get("markdown") or "").rstrip())
    else:
        skip = disassembly.get("skip_reason") or "未触发"
        detail = disassembly.get("skip_detail") or ""
        lines.append(f"- 未执行或未采用反汇编（原因: {skip}{(' — ' + detail) if detail else ''}）")
        lines.append("- 本层可跳过；勿臆造指令语义。")
    lines.append("")

    # --- 5. 寄存器 / 数据 ---
    lines.append("### 4) 数据（寄存器）— CPU 在操作什么")
    has_reg_detail = False
    if sp.sp_value:
        has_reg_detail = True
        sp_status = "正常" if sp.stack_overflow_risk == "none" else f"风险={sp.stack_overflow_risk}"
        sp_region = ""
        if sp.in_stack_region is True:
            sp_region = f"，在栈区域内（距边界 {sp.distance_to_boundary_bytes} 字节）"
        elif sp.in_stack_region is False:
            sp_region = "，不在栈区域（异常）"
        align_info = "对齐正常" if sp.alignment_ok else "未对齐（异常）"
        lines.append(f"- SP: {sp.sp_value}，{align_info}{sp_region}，栈状态: {sp_status}")
    if fp.fp_value:
        has_reg_detail = True
        fp_status_parts = []
        if fp.in_stack_region is True:
            fp_status_parts.append("在栈区域内")
        elif fp.in_stack_region is False:
            fp_status_parts.append("不在栈区域（帧链损坏）")
        if fp.above_sp is True:
            fp_status_parts.append("FP > SP（正常）")
        elif fp.above_sp is False:
            fp_status_parts.append("FP < SP（异常）")
        lines.append(f"- FP: {fp.fp_value}，{'; '.join(fp_status_parts)}")
    if correlation.arg_registers:
        has_reg_detail = True
        arg_parts = [f"{k}={v}" for k, v in list(correlation.arg_registers.items())[:4]]
        lines.append(f"- 参数寄存器: {', '.join(arg_parts)}")
    if correlation.uaf_pattern_registers:
        has_reg_detail = True
        lines.append(f"- UAF 特征: {', '.join(correlation.freed_memory_indicators[:3])}")
    if correlation.null_registers:
        has_reg_detail = True
        lines.append(f"- NULL 寄存器: {', '.join(correlation.null_registers[:8])}")
    if not has_reg_detail:
        if not data_availability.get("has_registers", True):
            lines.append("- （日志无寄存器转储，跳过 SP/FP/参数寄存器健康度检查）")
        else:
            lines.append("- （寄存器存在但未提取到可用摘要）")
    lines.append("")

    # --- 6. 证据链摘要 ---
    lines.append("### 证据链摘要")
    if evidence_chain:
        for ev in evidence_chain:
            ev_type = ev.get("type", "")
            finding = ev.get("finding", "")
            implication = ev.get("implication", "")
            if finding and implication:
                lines.append(f"- [{ev_type}] {finding} → {implication}")
            elif finding:
                lines.append(f"- [{ev_type}] {finding}")
    else:
        lines.append("- （暂无结构化证据）")
    lines.append("")

    # --- 7. 建议 ---
    lines.append("### 分析建议")
    suggestions = _generate_suggestions(classification, fault, sp, fp, correlation)
    if not data_availability.get("has_registers", True):
        suggestions.insert(0, "建议补充含寄存器/Fault address 的完整崩溃日志以交叉验证")
    if disassembly.get("triggered"):
        suggestions.insert(0, "根因论述须点名引用反汇编中的崩溃 PC 指令及访存方向")
    for s in suggestions:
        lines.append(f"- {s}")

    return "\n".join(lines)


def _generate_suggestions(
    cls: CrashClassification,
    fault: FaultAnalysis,
    sp: SpAnalysis,
    fp: FpAnalysis,
    corr: RegisterCorrelation,
) -> List[str]:
    """根据分类生成分析建议。"""
    suggestions: List[str] = []
    pattern = cls.primary_pattern

    if pattern in ("null_pointer_dereference", "null_pointer_member_access"):
        if fault.base_register:
            suggestions.append(f"聚焦: {fault.base_register} 所持对象何时被置 NULL 或释放")
        else:
            suggestions.append("聚焦: 确认崩溃时哪个指针为 NULL")
        suggestions.append("排查: 对象生命周期管理、多线程访问是否有竞争")

    elif pattern == "use_after_free":
        suggestions.append("聚焦: 确认对象释放路径和再次访问路径")
        suggestions.append("排查: shared_ptr/weak_ptr 是否正确使用、析构后是否仍被引用")

    elif pattern in ("stack_overflow", "stack_corruption"):
        suggestions.append("排查: 是否有无限递归或过大的栈上分配")
        if fp.in_stack_region is False:
            suggestions.append("注意: 帧链已损坏，backtrace 可能不完整，需结合其他线索")

    elif pattern == "code_corruption":
        suggestions.append("排查: 虚表指针(vptr)是否被覆盖、函数指针是否有效")

    elif pattern == "explicit_abort":
        suggestions.append("聚焦: abort 前的日志输出和 assert 条件；若有 Abort message 必须先引用原文")
    elif pattern == "heap_corruption":
        suggestions.append(
            "聚焦: 业务栈第一帧及其被调函数中的 vector/new/delete 越界或 double-free，而不是 abort 帧"
        )
        suggestions.append("不要把 Scudo invalid chunk 当成 GL/业务 assert，也不要发明与源码矛盾的 glUseProgram(0)")

    excludes: List[str] = []
    if sp.stack_overflow_risk == "none":
        excludes.append("栈溢出（SP 正常）")
    if not corr.uaf_pattern_registers:
        excludes.append("UAF（无 0x6b 特征）")
    if excludes:
        suggestions.append(f"排除: {', '.join(excludes)}")

    return suggestions or ["结合代码上下文进一步分析"]
