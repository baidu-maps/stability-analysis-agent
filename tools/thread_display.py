# -*- coding: utf-8 -*-
"""05 提示词等面向 LLM 的线程身份与角色中文展示。"""

from typing import Any, Optional, Tuple


def normalize_harmony_thread_fields(
    thread_id: Any,
    thread_name: Any,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Harmony ``body.stacks[]`` 常将 ``thread_id`` 填为线程名、``thread_name`` 填为数字 tid。

    当 ``thread_id`` 非纯数字且 ``thread_name`` 为纯数字时，交换为 (数字 tid, 字符串名)。
    """
    tid_s = (
        str(thread_id).strip()
        if thread_id is not None and str(thread_id).strip()
        else None
    )
    name_s = str(thread_name).strip() if thread_name else None
    if tid_s and name_s and not tid_s.isdigit() and name_s.isdigit():
        return name_s, tid_s
    return tid_s, name_s


def format_prompt_thread_identity(tid: Any, name: Any = None) -> str:
    """05：线程 ID / 线程名中文标签。"""
    tid_s = str(tid).strip() if tid is not None and str(tid).strip() else None
    name_s = str(name).strip() if name else None
    parts = [f"线程ID={tid_s or '未知'}"]
    if name_s:
        parts.append(f"线程名={name_s}")
    return " ".join(parts)


def format_prompt_thread_role_flags(
    is_crash_thread: bool,
    is_main_thread: Any = None,
) -> str:
    """05：崩溃/主线程角色，偏中文自然表述（非 key=value）。"""
    parts = ["崩溃线程" if is_crash_thread else "非崩溃线程"]
    if is_main_thread is True:
        parts.append("主线程")
    elif is_main_thread is False:
        parts.append("非主线程")
    return "，".join(parts)
