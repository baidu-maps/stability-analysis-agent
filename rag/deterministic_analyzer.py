#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""确定性前置分析器。

在 LLM 调用前执行确定性规则判断，将 100% 可确定的结论
标记为"已确认事实"注入 prompt，减轻 LLM 推理负担。
"""

from __future__ import annotations
import re
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class DeterministicFact:
    """一条确定性事实。"""
    fact_type: str       # "null_pointer" / "stack_overflow" / "abort" / "detector_report" / "divide_by_zero"
    description: str     # 人类可读描述
    confidence: float    # 1.0 = 100% certain
    evidence: str        # 支撑证据
    implication: str     # 对分析的指导意义


@dataclass
class DeterministicConclusions:
    """确定性分析结果集合。"""
    facts: List[DeterministicFact] = field(default_factory=list)
    has_conclusions: bool = False

    def render(self) -> str:
        """渲染为可注入 prompt 的 Markdown。"""
        if not self.facts:
            return ""
        lines = ["## 已确认事实（无需推理）", ""]
        lines.append("以下结论已通过确定性规则验证，请在分析中直接引用，无需重新推导：")
        lines.append("")
        for i, fact in enumerate(self.facts, 1):
            lines.append(f"### 事实 {i}: {fact.description}")
            lines.append(f"- 确定性: {fact.confidence:.0%}")
            lines.append(f"- 证据: {fact.evidence}")
            lines.append(f"- 意义: {fact.implication}")
            lines.append("")
        return "\n".join(lines)


class DeterministicAnalyzer:
    """执行确定性前置分析。"""

    def analyze(
        self,
        parse_result: Dict[str, Any],
        resolved_stack: Dict[str, Any],
        crash_log_content: str = "",
    ) -> DeterministicConclusions:
        """对解析结果执行确定性判断。

        Args:
            parse_result: 01 crash parser output
            resolved_stack: 02 symbol resolution output
            crash_log_content: raw crash log text

        Returns:
            DeterministicConclusions with any confirmed facts
        """
        conclusions = DeterministicConclusions()
        crash_info = parse_result.get("crash_info", {}) if isinstance(parse_result, dict) else {}

        # Rule 1: Null pointer via fault address / 符号启发
        self._check_null_pointer(crash_info, crash_log_content, conclusions)
        self._check_null_pointer_from_symbols(crash_info, resolved_stack, conclusions)

        # Rule 2: Stack overflow via recursive pattern
        self._check_stack_overflow(crash_info, resolved_stack, conclusions)

        # Rule 3: Explicit abort / allocator heap abort
        self._check_abort(crash_info, crash_log_content, conclusions)

        # Rule 4: Sanitizer/detector report
        self._check_detector_report(crash_log_content, conclusions)

        # Rule 5: Divide by zero
        self._check_divide_by_zero(crash_info, conclusions)

        conclusions.has_conclusions = bool(conclusions.facts)
        return conclusions

    def _check_null_pointer(
        self, crash_info: Dict[str, Any], log_content: str, out: DeterministicConclusions
    ) -> None:
        """Signal SIGSEGV + fault address near zero = null pointer dereference (100%)."""
        signal = str(crash_info.get("signal") or crash_info.get("crash_signal") or "")
        if "SIGSEGV" not in signal.upper() and "EXC_BAD_ACCESS" not in signal.upper():
            return

        # Try to extract fault address
        crash_address = crash_info.get("crash_address") or ""
        if not crash_address:
            # Try to find in log
            m = re.search(r"fault addr\s+(0x[0-9a-fA-F]+)", log_content[:5000])
            if m:
                crash_address = m.group(1)

        if not crash_address:
            return

        # 若地址看起来像代码段 PC（远大于页内偏移），不当作 fault addr
        try:
            addr_val = int(crash_address, 16) if isinstance(crash_address, str) and crash_address.startswith("0x") else int(str(crash_address), 0)
            if addr_val < 0x1000:
                out.facts.append(DeterministicFact(
                    fact_type="null_pointer",
                    description="空指针解引用（确定）",
                    confidence=1.0,
                    evidence=f"信号={signal}, 故障地址={crash_address} (< 0x1000)",
                    implication="崩溃原因为空指针访问，分析应聚焦于指针为何为空（生命周期/未初始化/竞态）",
                ))
        except (ValueError, TypeError):
            pass

    def _check_null_pointer_from_symbols(
        self,
        crash_info: Dict[str, Any],
        resolved_stack: Dict[str, Any],
        out: DeterministicConclusions,
    ) -> None:
        """无可靠 near-null fault 时，用空指针符号名 + SIGSEGV 给出次级确定性。"""
        if any(f.fact_type == "null_pointer" for f in out.facts):
            return
        signal = str(crash_info.get("signal") or crash_info.get("crash_signal") or "")
        if "SIGSEGV" not in signal.upper() and "EXC_BAD_ACCESS" not in signal.upper():
            return

        reason = str(crash_info.get("crash_reason") or "").lower()
        null_hints = (
            "nullptr", "null_ptr", "nullpointer", "null_deref", "null deref",
            "nullptr_sigsegv", "null ptr",
        )
        corpus_parts = [reason]
        frames: List[Dict[str, Any]] = []
        if isinstance(resolved_stack, dict):
            for thread in resolved_stack.get("resolved_threads") or []:
                if not isinstance(thread, dict):
                    continue
                if thread.get("is_crash_thread") or not frames:
                    frames = list(thread.get("frames") or [])
                    if thread.get("is_crash_thread"):
                        break
        for f in frames[:8]:
            if isinstance(f, dict):
                corpus_parts.append(str(f.get("resolved_function") or f.get("function") or "").lower())
        corpus = " ".join(corpus_parts)
        if not any(h in corpus for h in null_hints):
            return

        hit = next((h for h in null_hints if h in corpus), "nullptr")
        crash_address = str(crash_info.get("crash_address") or "")
        note = ""
        if crash_address:
            try:
                addr_val = int(crash_address, 16) if crash_address.startswith("0x") else int(crash_address, 0)
                if addr_val >= 0x1000:
                    note = f"；日志崩溃地址 {crash_address} 更像 PC 而非 fault addr，未用于定案"
            except (ValueError, TypeError):
                note = f"；崩溃地址 {crash_address} 无法按 fault addr 验证"

        out.facts.append(DeterministicFact(
            fact_type="null_pointer",
            description="空指针解引用（符号启发，次级确定）",
            confidence=0.85,
            evidence=f"信号={signal or 'SEGV'}，符号/原因含「{hit}」{note}",
            implication="倾向空指针，但缺真实 fault_addr/寄存器交叉验证；分析应聚焦指针生命周期",
        ))

    def _check_stack_overflow(
        self, crash_info: Dict[str, Any], resolved_stack: Dict[str, Any], out: DeterministicConclusions
    ) -> None:
        """Stack overflow with recursive frames = definite stack overflow."""
        reason = str(crash_info.get("crash_reason") or "").lower()
        if "stack overflow" not in reason and "stack_overflow" not in reason:
            return

        # Check for recursive frames
        frames = []
        if isinstance(resolved_stack, dict):
            for thread in resolved_stack.get("resolved_threads", []):
                if thread.get("is_crash_thread"):
                    frames = thread.get("frames", [])
                    break

        func_counts: Dict[str, int] = {}
        for f in frames[:30]:
            name = f.get("function") or f.get("resolved_function") or ""
            if name:
                func_counts[name] = func_counts.get(name, 0) + 1

        recursive_funcs = [n for n, c in func_counts.items() if c >= 3]

        if recursive_funcs:
            out.facts.append(DeterministicFact(
                fact_type="stack_overflow",
                description="栈溢出（递归导致，确定）",
                confidence=1.0,
                evidence=f"crash_reason含stack overflow，递归函数: {', '.join(recursive_funcs[:3])}",
                implication="需要检查递归终止条件或改为迭代实现",
            ))
        else:
            out.facts.append(DeterministicFact(
                fact_type="stack_overflow",
                description="栈溢出（确定，原因待定）",
                confidence=0.95,
                evidence=f"crash_reason含stack overflow",
                implication="可能是递归过深或栈帧过大，需检查调用深度和局部变量大小",
            ))

    def _check_abort(
        self,
        crash_info: Dict[str, Any],
        log_content: str,
        out: DeterministicConclusions,
    ) -> None:
        """SIGABRT：区分分配器堆损坏与普通 abort/assert。"""
        signal = str(crash_info.get("signal") or crash_info.get("crash_signal") or "")
        if "SIGABRT" not in signal.upper():
            return

        from tools.crash_parser.abort_message import extract_abort_message, is_heap_allocator_abort

        reason = str(crash_info.get("crash_reason") or "")
        abort_message = str(
            crash_info.get("abort_message") or extract_abort_message(log_content) or ""
        )
        if is_heap_allocator_abort(abort_message, reason, log_content):
            out.facts.append(DeterministicFact(
                fact_type="heap_abort",
                description="堆分配器在释放/校验时 abort（非业务 assert）",
                confidence=1.0,
                evidence=(
                    f"信号=SIGABRT"
                    + (f", Abort message={abort_message}" if abort_message else "")
                    + (f", reason={reason}" if reason else "")
                ),
                implication=(
                    "Scudo/jemalloc 等在 deallocate 时发现 chunk 非法，说明更早发生了越界写或 double-free。"
                    "应检查业务栈第一帧及其被调函数中的堆操作，不要当作普通 assert，也不要根据错误的 libc 符号编故事。"
                ),
            ))
            return

        out.facts.append(DeterministicFact(
            fact_type="abort",
            description="进程主动 abort（非随机崩溃）",
            confidence=0.92,
            evidence=f"信号=SIGABRT" + (f", reason={reason}" if reason else ""),
            implication=(
                "可能是 assert/显式 abort，也可能是未抽取到的分配器错误。"
                "请先阅读 Abort message；仅在确认是业务断言后再查 assert 条件。"
            ),
        ))

    def _check_detector_report(self, log_content: str, out: DeterministicConclusions) -> None:
        """ASan/TSan/GWP-ASan explicit report = trust detector."""
        detectors = {
            "AddressSanitizer": "内存错误检测器 (ASan) 报告",
            "ThreadSanitizer": "线程安全检测器 (TSan) 报告",
            "GWP-ASan": "GWP-ASan 采样检测器报告",
            "LeakSanitizer": "内存泄漏检测器 (LSan) 报告",
            "Scudo ERROR": "Scudo 堆分配器完整性失败",
        }
        search_area = log_content[:10000]
        for keyword, desc in detectors.items():
            if keyword in search_area:
                out.facts.append(DeterministicFact(
                    fact_type="detector_report",
                    description=f"{desc}（直接引用检测器结论）",
                    confidence=1.0,
                    evidence=f"日志中存在 {keyword} 报告",
                    implication="检测器报告证据最强，应直接基于其结论分析根因",
                ))
                break  # One detector is enough

    def _check_divide_by_zero(self, crash_info: Dict[str, Any], out: DeterministicConclusions) -> None:
        """SIGFPE = arithmetic exception (divide by zero)."""
        signal = str(crash_info.get("signal") or crash_info.get("crash_signal") or "")
        if "SIGFPE" not in signal.upper():
            return
        out.facts.append(DeterministicFact(
            fact_type="divide_by_zero",
            description="算术异常 / 除零（确定）",
            confidence=1.0,
            evidence=f"信号=SIGFPE",
            implication="需要检查除法运算的除数是否可能为零，添加零值检查",
        ))
