#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""反汇编工具封装。

可选的深入分析工具：当用户提供 .so / .dylib 二进制文件时，
通过 llvm-objdump 获取崩溃 PC 附近的汇编指令，
帮助判断访存方向、寄存器含义和实际操作。

参考华为 DFX Skills cppcrash-analysis 的反汇编使用模式。

使用条件（三层递进，仅在需要时触发）：
1. 符号化后行号仍不足以判断问题
2. 需要分析多参数调用、虚表、函数指针或访存寄存器
3. 用户提供了匹配的 .so/.dylib 二进制文件

本工具仅作证据辅助，不能单独作为根因判定依据。
"""

from __future__ import annotations
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class DisassemblyInstruction:
    """单条反汇编指令。"""
    address: str
    opcode: str
    operands: str
    is_crash_pc: bool = False
    source_line: str = ""  # 对应的源码行（如果有 -S 混合输出）


@dataclass
class DisassemblyContext:
    """反汇编上下文（PC 附近的指令）。"""
    binary_path: str
    crash_pc: str
    function_name: str = ""
    instructions: List[DisassemblyInstruction] = field(default_factory=list)
    access_direction: str = ""  # "read" / "write" / "call" / "unknown"
    access_direction_zh: str = ""
    instruction_summary_zh: str = ""
    involved_registers: List[str] = field(default_factory=list)
    tool_used: str = ""       # "llvm-objdump" / "objdump" / "otool"
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "binary_path": self.binary_path,
            "crash_pc": self.crash_pc,
            "function_name": self.function_name,
            "instruction_count": len(self.instructions),
            "access_direction": self.access_direction,
            "access_direction_zh": self.access_direction_zh,
            "instruction_summary_zh": self.instruction_summary_zh,
            "involved_registers": self.involved_registers,
            "tool_used": self.tool_used,
            "error": self.error,
            "instructions_near_pc": [
                {
                    "address": i.address,
                    "opcode": i.opcode,
                    "operands": i.operands,
                    "is_crash_pc": i.is_crash_pc,
                }
                for i in self.instructions
                if i.is_crash_pc or abs(self.instructions.index(i) - self._pc_index()) <= 5
            ] if self.instructions else [],
        }

    def _pc_index(self) -> int:
        for i, inst in enumerate(self.instructions):
            if inst.is_crash_pc:
                return i
        return len(self.instructions) // 2

    def render_markdown(self) -> str:
        """渲染为 Markdown 格式的反汇编上下文。"""
        if self.error:
            return f"反汇编失败: {self.error}"
        if not self.instructions:
            return ""

        lines = [
            f"反汇编上下文 (工具: {self.tool_used}):",
            f"- 二进制: {Path(self.binary_path).name}",
            f"- 崩溃 PC: {self.crash_pc}",
        ]
        if self.function_name:
            lines.append(f"- 函数: {self.function_name}")
        if self.access_direction:
            zh = self.access_direction_zh or self.access_direction
            lines.append(f"- 访存方向: {zh}")
        if self.instruction_summary_zh:
            lines.append(f"- 指令解读: {self.instruction_summary_zh}")
        if self.involved_registers:
            lines.append(f"- 涉及寄存器: {', '.join(self.involved_registers)}")

        lines.append("\n```asm")
        pc_idx = self._pc_index()
        start = max(0, pc_idx - 5)
        end = min(len(self.instructions), pc_idx + 6)
        for inst in self.instructions[start:end]:
            marker = ">>>" if inst.is_crash_pc else "   "
            lines.append(f"{marker} {inst.address}: {inst.opcode}\t{inst.operands}")
        lines.append("```")

        return "\n".join(lines)


# --- Tool discovery ---

def _find_objdump_tool() -> Optional[str]:
    """查找可用的 objdump 工具。"""
    candidates = [
        "llvm-objdump",
        "objdump",
        # Common paths
        "/usr/bin/llvm-objdump",
        "/usr/local/bin/llvm-objdump",
        "/opt/homebrew/bin/llvm-objdump",
    ]
    # Also check Android NDK
    ndk_home = os.environ.get("ANDROID_NDK_HOME") or os.environ.get("NDK_HOME")
    if ndk_home:
        candidates.append(f"{ndk_home}/toolchains/llvm/prebuilt/*/bin/llvm-objdump")

    for candidate in candidates:
        path = shutil.which(candidate)
        if path:
            return path
    return None


def _find_otool() -> Optional[str]:
    """查找 macOS otool (for Mach-O)."""
    return shutil.which("otool")


# --- Disassembly execution ---

class DisassemblyTool:
    """反汇编工具封装。"""

    def __init__(self, tool_path: Optional[str] = None):
        """
        Args:
            tool_path: 显式指定 objdump 路径，None 时自动查找
        """
        self._tool_path = tool_path or _find_objdump_tool()
        self._otool_path = _find_otool()

    @property
    def available(self) -> bool:
        """是否有可用的反汇编工具。"""
        return self._tool_path is not None or self._otool_path is not None

    def disassemble_around_pc(
        self,
        binary_path: str,
        crash_pc: str,
        context_lines: int = 20,
    ) -> DisassemblyContext:
        """对二进制文件执行反汇编，提取 PC 附近的指令。

        Args:
            binary_path: .so / .dylib / 可执行文件路径
            crash_pc: 崩溃 PC 地址（相对偏移或绝对地址）
            context_lines: PC 前后各取多少行

        Returns:
            DisassemblyContext 结果
        """
        result = DisassemblyContext(
            binary_path=binary_path,
            crash_pc=crash_pc,
        )

        if not Path(binary_path).exists():
            result.error = f"二进制文件不存在: {binary_path}"
            return result

        if not self.available:
            result.error = "未找到 llvm-objdump / objdump 工具"
            return result

        # Try llvm-objdump first
        if self._tool_path:
            return self._run_objdump(binary_path, crash_pc, context_lines, result)
        elif self._otool_path:
            return self._run_otool(binary_path, crash_pc, context_lines, result)

        result.error = "无可用反汇编工具"
        return result

    def _run_objdump(
        self,
        binary_path: str,
        crash_pc: str,
        context_lines: int,
        result: DisassemblyContext,
    ) -> DisassemblyContext:
        """使用 llvm-objdump / objdump 进行反汇编。"""
        cmd = [
            self._tool_path,
            "-d",         # disassemble
            "-C",         # demangle C++ names
            "-l",         # show line numbers
            "--no-show-raw-insn",  # cleaner output
            binary_path,
        ]
        result.tool_used = Path(self._tool_path).name

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode != 0:
                result.error = f"objdump 执行失败 (code={proc.returncode}): {proc.stderr[:200]}"
                return result

            self._parse_objdump_output(proc.stdout, crash_pc, context_lines, result)
            return result

        except subprocess.TimeoutExpired:
            result.error = "objdump 执行超时 (30s)"
            return result
        except Exception as e:
            result.error = f"objdump 执行异常: {e}"
            return result

    def _run_otool(
        self,
        binary_path: str,
        crash_pc: str,
        context_lines: int,
        result: DisassemblyContext,
    ) -> DisassemblyContext:
        """使用 otool (macOS) 进行反汇编。"""
        cmd = ["otool", "-tV", binary_path]
        result.tool_used = "otool"

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode != 0:
                result.error = f"otool 执行失败: {proc.stderr[:200]}"
                return result

            self._parse_objdump_output(proc.stdout, crash_pc, context_lines, result)
            return result
        except Exception as e:
            result.error = f"otool 执行异常: {e}"
            return result

    def _parse_objdump_output(
        self,
        output: str,
        crash_pc: str,
        context_lines: int,
        result: DisassemblyContext,
    ) -> None:
        """解析 objdump 输出，提取 PC 附近的指令。"""
        # Normalize PC address for matching
        pc_val = self._normalize_address(crash_pc)
        if pc_val is None:
            result.error = f"无法解析 PC 地址: {crash_pc}"
            return

        # Parse instructions
        # Format: "  address: opcode\toperands" or "  address:\topcode operands"
        inst_re = re.compile(r"^\s*([0-9a-fA-F]+):\s*(\S+)\s*(.*?)$")
        func_re = re.compile(r"^([0-9a-fA-F]+)\s+<(.+)>:")

        all_instructions: List[DisassemblyInstruction] = []
        current_func = ""
        pc_found = False

        for line in output.splitlines():
            # Check for function header
            fm = func_re.match(line)
            if fm:
                current_func = fm.group(2)
                continue

            # Check for instruction
            im = inst_re.match(line)
            if im:
                addr_str = im.group(1)
                opcode = im.group(2)
                operands = im.group(3).strip()

                addr_val = int(addr_str, 16)
                is_pc = (addr_val == pc_val)
                if is_pc:
                    pc_found = True
                    result.function_name = current_func

                all_instructions.append(DisassemblyInstruction(
                    address=f"0x{addr_str}",
                    opcode=opcode,
                    operands=operands,
                    is_crash_pc=is_pc,
                ))

        if not pc_found and all_instructions:
            # Try to find closest instruction
            min_dist = float("inf")
            closest_idx = 0
            for i, inst in enumerate(all_instructions):
                try:
                    inst_addr = int(inst.address, 16)
                    dist = abs(inst_addr - pc_val)
                    if dist < min_dist:
                        min_dist = dist
                        closest_idx = i
                except ValueError:
                    pass
            if min_dist < 0x100:  # Within 256 bytes
                all_instructions[closest_idx].is_crash_pc = True
                pc_found = True

        # Extract context around PC
        if pc_found:
            pc_idx = next(
                (i for i, inst in enumerate(all_instructions) if inst.is_crash_pc),
                len(all_instructions) // 2,
            )
            start = max(0, pc_idx - context_lines)
            end = min(len(all_instructions), pc_idx + context_lines + 1)
            result.instructions = all_instructions[start:end]

            # Analyze the crash instruction
            if all_instructions[pc_idx]:
                self._analyze_crash_instruction(all_instructions[pc_idx], result)

    def _analyze_crash_instruction(
        self,
        instruction: DisassemblyInstruction,
        result: DisassemblyContext,
    ) -> None:
        """分析崩溃指令，推断访存方向和涉及寄存器。"""
        opcode = instruction.opcode.lower()
        operands = instruction.operands
        summary = ""

        # ARM64 load/store（含 ldur/stur/ldxr 等）
        if opcode.startswith(("ldr", "ldp", "ldur", "ldxr", "ldar", "ldarb", "ldurb")):
            result.access_direction = "read"
            summary = f"从内存加载（{opcode}），关注基址寄存器是否为空/野指针"
        elif opcode.startswith(("str", "stp", "stur", "stxr", "stlr", "sturb")):
            result.access_direction = "write"
            summary = f"向内存存储（{opcode}），关注写入目标地址是否合法"
        # x86 mov / 访存
        elif opcode in ("mov", "movb", "movw", "movl", "movq") or opcode.startswith(
            ("movz", "movk", "movd", "movs")
        ):
            if "[" in operands:
                parts = [p.strip() for p in operands.split(",")]
                if len(parts) >= 2 and "[" in parts[-1]:
                    result.access_direction = "read"
                    summary = "x86 风格从内存读入寄存器"
                elif "[" in parts[0]:
                    result.access_direction = "write"
                    summary = "x86 风格写入内存"
        elif opcode in ("blr", "br", "call", "callq", "jmp", "jmpq"):
            result.access_direction = "call"
            summary = f"间接调用/跳转（{opcode}），关注目标是否为损坏函数指针/虚表"
        elif opcode.startswith(("cbz", "cbnz", "tbz", "tbnz")):
            result.access_direction = "unknown"
            summary = f"条件分支（{opcode}），崩溃点可能在分支前后的访存指令"
        elif opcode in ("ret", "retq"):
            result.access_direction = "call"
            summary = "函数返回；若崩溃在此，可能是返回地址/栈被破坏"

        if not result.access_direction:
            result.access_direction = "unknown"

        _zh = {
            "read": "读（load）",
            "write": "写（store）",
            "call": "调用/跳转",
            "unknown": "未知/非典型访存",
        }
        result.access_direction_zh = _zh.get(result.access_direction, result.access_direction)
        if summary:
            result.instruction_summary_zh = summary
        else:
            result.instruction_summary_zh = (
                f"崩溃指令 `{opcode} {operands}`，未能可靠推断访存方向"
            )

        # Extract register names from operands
        reg_re = re.compile(
            r"\b(x\d{1,2}|w\d{1,2}|sp|lr|pc|fp|r\d{1,2}|rax|rbx|rcx|rdx|rsi|rdi|rbp|rsp|"
            r"rip|eax|ebx|ecx|edx|esi|edi)\b",
            re.IGNORECASE,
        )
        result.involved_registers = list(dict.fromkeys(reg_re.findall(operands)))

        # 空基址启发式：ARM `[xN]` / `[xN, #imm]` 且操作数含 xzr/wzr
        if "xzr" in operands.lower() or "wzr" in operands.lower():
            result.instruction_summary_zh += "；操作数含零寄存器(xzr/wzr)，高度可疑空基址"
        elif result.access_direction in ("read", "write") and re.search(
            r"\[(x|w)(\d{1,2})\s*[, \]]", operands, re.I
        ):
            result.instruction_summary_zh += "；请结合寄存器值核对基址是否为 NULL/野指针"

    def _normalize_address(self, addr_str: str) -> Optional[int]:
        """Normalize address string to integer."""
        addr_str = addr_str.strip()
        try:
            if addr_str.startswith("0x") or addr_str.startswith("0X"):
                return int(addr_str, 16)
            if re.match(r'^[0-9a-fA-F]+$', addr_str):
                return int(addr_str, 16)
            return int(addr_str, 0)
        except (ValueError, TypeError):
            return None
