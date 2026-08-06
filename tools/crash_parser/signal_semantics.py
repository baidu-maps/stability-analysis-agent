#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""信号子码语义知识库与分析。

在 parser 阶段对信号子码进行语义解读，输出初步根因推断。
例如 SIGSEGV + SEGV_MAPERR → "未映射地址访问，高概率 UAF 或越界"。
"""

from __future__ import annotations
import re
from typing import Any, Dict, List, Optional, Tuple


# 信号子码语义库
# key: (signal_name, sub_code) → 语义信息
SIGNAL_SUBCODE_SEMANTICS: Dict[Tuple[str, str], Dict[str, Any]] = {
    # --- SIGSEGV (Signal 11) ---
    ("SIGSEGV", "SEGV_MAPERR"): {
        "hint": "访问未映射地址（地址不在进程映射范围内）",
        "likely_root_cause": ["use_after_free", "out_of_bounds", "null_pointer"],
        "evidence_strength": "medium",
        "explanation": "进程访问了一个未在 maps 中出现的虚拟地址。如果地址接近零则为空指针；"
                       "如果地址含 0x6b 特征则疑似 UAF；否则可能为越界写后的间接访问。",
    },
    ("SIGSEGV", "SEGV_ACCERR"): {
        "hint": "内存区域存在但权限不足（如写只读页）",
        "likely_root_cause": ["write_to_readonly", "stack_guard_page", "code_signing"],
        "evidence_strength": "medium",
        "explanation": "地址已映射但进程无相应权限。常见于栈溢出触碰 guard page、"
                       "写入只读数据段、或 iOS 代码签名校验失败。",
    },
    ("SIGSEGV", "1"): {  # si_code=1 on some platforms = SEGV_MAPERR
        "hint": "未映射地址访问 (si_code=1, 等价于 SEGV_MAPERR)",
        "likely_root_cause": ["use_after_free", "out_of_bounds", "null_pointer"],
        "evidence_strength": "medium",
        "explanation": "si_code=1 对应 SEGV_MAPERR。",
    },
    ("SIGSEGV", "2"): {  # si_code=2 on some platforms = SEGV_ACCERR
        "hint": "权限不足的内存访问 (si_code=2, 等价于 SEGV_ACCERR)",
        "likely_root_cause": ["write_to_readonly", "stack_guard_page"],
        "evidence_strength": "medium",
        "explanation": "si_code=2 对应 SEGV_ACCERR。",
    },

    # --- SIGBUS (Signal 7) ---
    ("SIGBUS", "BUS_ADRALN"): {
        "hint": "非对齐内存访问",
        "likely_root_cause": ["unaligned_access", "type_punning"],
        "evidence_strength": "high",
        "explanation": "CPU 要求对齐访问但实际地址未对齐。"
                       "常见于 ARM 平台上对未对齐地址做 int/long 读写。",
    },
    ("SIGBUS", "BUS_ADRERR"): {
        "hint": "不存在的物理地址",
        "likely_root_cause": ["hardware_error", "mmap_hole"],
        "evidence_strength": "medium",
        "explanation": "访问了映射区域内但无物理页的地址，可能是 mmap 文件被截断。",
    },
    ("SIGBUS", "BUS_OBJERR"): {
        "hint": "对象特定的硬件错误",
        "likely_root_cause": ["hardware_error", "io_error"],
        "evidence_strength": "low",
        "explanation": "通常由 I/O 设备或硬件故障触发。",
    },
    ("SIGBUS", "1"): {
        "hint": "非对齐内存访问 (si_code=1)",
        "likely_root_cause": ["unaligned_access"],
        "evidence_strength": "high",
        "explanation": "si_code=1 对应 BUS_ADRALN。",
    },
    ("SIGBUS", "2"): {
        "hint": "不存在的物理地址 (si_code=2)",
        "likely_root_cause": ["mmap_hole", "hardware_error"],
        "evidence_strength": "medium",
        "explanation": "si_code=2 对应 BUS_ADRERR。",
    },

    # --- SIGILL (Signal 4) ---
    ("SIGILL", "ILL_ILLOPC"): {
        "hint": "非法操作码",
        "likely_root_cause": ["code_corruption", "jump_to_data", "jit_error"],
        "evidence_strength": "high",
        "explanation": "CPU 遇到无法解码的指令字节。可能是跳转到数据区或内存损坏。",
    },
    ("SIGILL", "ILL_ILLOPN"): {
        "hint": "非法操作数",
        "likely_root_cause": ["code_corruption", "abi_mismatch"],
        "evidence_strength": "medium",
        "explanation": "指令操作码合法但操作数非法。",
    },
    ("SIGILL", "ILL_PRVOPC"): {
        "hint": "特权指令",
        "likely_root_cause": ["kernel_mode_access", "wrong_arch"],
        "evidence_strength": "high",
        "explanation": "用户态执行了需要内核权限的指令。",
    },
    ("SIGILL", "1"): {
        "hint": "非法操作码 (si_code=1)",
        "likely_root_cause": ["code_corruption", "jump_to_data"],
        "evidence_strength": "high",
        "explanation": "si_code=1 对应 ILL_ILLOPC。",
    },

    # --- SIGFPE (Signal 8) ---
    ("SIGFPE", "FPE_INTDIV"): {
        "hint": "整数除零",
        "likely_root_cause": ["integer_divide_by_zero"],
        "evidence_strength": "high",
        "explanation": "整数除法/取模运算中除数为零。",
    },
    ("SIGFPE", "FPE_INTOVF"): {
        "hint": "整数溢出",
        "likely_root_cause": ["integer_overflow"],
        "evidence_strength": "high",
        "explanation": "整数运算结果超出可表示范围（罕见，通常不触发信号）。",
    },
    ("SIGFPE", "FPE_FLTDIV"): {
        "hint": "浮点除零",
        "likely_root_cause": ["float_divide_by_zero"],
        "evidence_strength": "high",
        "explanation": "浮点除法中除数为零或非法浮点运算。",
    },
    ("SIGFPE", "1"): {
        "hint": "整数除零 (si_code=1)",
        "likely_root_cause": ["integer_divide_by_zero"],
        "evidence_strength": "high",
        "explanation": "si_code=1 对应 FPE_INTDIV。",
    },

    # --- SIGABRT (Signal 6) ---
    ("SIGABRT", ""): {
        "hint": "进程主动调用 abort() 或 assert 失败",
        "likely_root_cause": ["assertion_failure", "unhandled_exception", "fatal_error"],
        "evidence_strength": "high",
        "explanation": "通常由 assert/abort/__fortify_fail/std::terminate 触发，需查看调用栈定位触发条件。",
    },

    # --- SIGTRAP (Signal 5) ---
    ("SIGTRAP", "TRAP_BRKPT"): {
        "hint": "断点 / __builtin_trap()",
        "likely_root_cause": ["intentional_trap", "sanitizer_report", "debugger_break"],
        "evidence_strength": "medium",
        "explanation": "可能是 __builtin_trap() 编译器陷阱或调试器断点。",
    },
    ("SIGTRAP", "1"): {
        "hint": "断点触发 (si_code=1)",
        "likely_root_cause": ["intentional_trap", "sanitizer_report"],
        "evidence_strength": "medium",
        "explanation": "si_code=1 对应 TRAP_BRKPT。",
    },
}


# --- 信号名→编号映射（用于数字子码解析） ---
SIGNAL_NAME_MAP = {
    "1": "SIGHUP", "2": "SIGINT", "3": "SIGQUIT", "4": "SIGILL",
    "5": "SIGTRAP", "6": "SIGABRT", "7": "SIGBUS", "8": "SIGFPE",
    "9": "SIGKILL", "10": "SIGUSR1", "11": "SIGSEGV", "12": "SIGUSR2",
    "13": "SIGPIPE", "14": "SIGALRM", "15": "SIGTERM",
}


def parse_signal_and_subcode(signal_str: str) -> Tuple[str, str]:
    """从信号字符串中提取信号名和子码。

    支持格式:
    - "SIGSEGV"
    - "SIGSEGV (SEGV_MAPERR)"
    - "11 (SIGSEGV)"
    - "Signal:SIGSEGV(SEGV_MAPERR)"
    - "signal 11 code 1"
    - "Fatal signal 11 (SIGSEGV), code 1 (SEGV_MAPERR)"

    Returns:
        (signal_name, sub_code) — sub_code 可能是 "" / "SEGV_MAPERR" / "1" 等
    """
    if not signal_str:
        return ("", "")

    signal_str = signal_str.strip()
    signal_name = ""
    sub_code = ""

    # Pattern: "Fatal signal 11 (SIGSEGV), code 1 (SEGV_MAPERR)"
    m = re.search(
        r"(?:fatal\s+)?signal\s+(\d+)\s*\((\w+)\)\s*,?\s*code\s+(-?\d+)\s*(?:\((\w+)\))?",
        signal_str, re.IGNORECASE
    )
    if m:
        signal_name = m.group(2).upper()
        sub_code = m.group(4) or m.group(3)
        return (signal_name, sub_code)

    # Pattern: "signal 11 code 1"
    m = re.search(r"signal\s+(\d+)\s+code\s+(-?\d+)", signal_str, re.IGNORECASE)
    if m:
        signal_name = SIGNAL_NAME_MAP.get(m.group(1), f"SIG{m.group(1)}")
        sub_code = m.group(2)
        return (signal_name, sub_code)

    # Pattern: "SIGSEGV (SEGV_MAPERR)" or "SIGSEGV(SEGV_MAPERR)"
    m = re.match(r"(SIG\w+)\s*\((\w+)\)", signal_str, re.IGNORECASE)
    if m:
        signal_name = m.group(1).upper()
        sub_code = m.group(2)
        return (signal_name, sub_code)

    # Pattern: "11 (SIGSEGV)"
    m = re.match(r"(\d+)\s*\((SIG\w+)\)", signal_str, re.IGNORECASE)
    if m:
        signal_name = m.group(2).upper()
        return (signal_name, "")

    # Pattern: plain "SIGSEGV" or "11"
    m = re.match(r"(SIG\w+|\d+)", signal_str, re.IGNORECASE)
    if m:
        raw = m.group(1)
        if raw.isdigit():
            signal_name = SIGNAL_NAME_MAP.get(raw, f"SIG{raw}")
        else:
            signal_name = raw.upper()
        return (signal_name, "")

    return (signal_str.upper(), "")


def get_signal_semantics(signal_str: str) -> Optional[Dict[str, Any]]:
    """获取信号子码的语义信息。

    Args:
        signal_str: 原始信号字符串（来自 crash_info.signal）

    Returns:
        语义信息字典或 None
    """
    signal_name, sub_code = parse_signal_and_subcode(signal_str)
    if not signal_name:
        return None

    # Try exact match first
    result = SIGNAL_SUBCODE_SEMANTICS.get((signal_name, sub_code))
    if result:
        return {
            "signal": signal_name,
            "sub_code": sub_code,
            **result,
        }

    # Try signal-only match (empty sub_code)
    result = SIGNAL_SUBCODE_SEMANTICS.get((signal_name, ""))
    if result:
        return {
            "signal": signal_name,
            "sub_code": sub_code or "(unknown)",
            **result,
        }

    return None


def analyze_signal_for_crash_info(crash_info: Dict[str, Any]) -> Dict[str, Any]:
    """分析 crash_info 中的信号，返回增强的语义信息。

    Args:
        crash_info: parse_result["crash_info"] 字典

    Returns:
        signal_semantics 字典（可直接写入 crash_info 或报告）
    """
    signal_str = crash_info.get("signal") or crash_info.get("crash_signal") or ""
    semantics = get_signal_semantics(signal_str)
    if not semantics:
        return {}

    return {
        "signal_semantics": {
            "signal_name": semantics.get("signal", ""),
            "sub_code": semantics.get("sub_code", ""),
            "hint": semantics.get("hint", ""),
            "likely_root_cause": semantics.get("likely_root_cause", []),
            "evidence_strength": semantics.get("evidence_strength", "low"),
            "explanation": semantics.get("explanation", ""),
        }
    }
