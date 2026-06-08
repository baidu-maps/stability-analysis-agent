#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""add2line_resolver（02）可用性判断与提前终止说明。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from tools.parse_crash_errors import _meaningful_symbol, frame_has_usable_info


def resolved_frames_from_stack(resolved_stack: Any) -> List[Dict[str, Any]]:
    """仅用于读取磁盘上的旧版 02（含 ``resolved_frames``）。"""
    if not isinstance(resolved_stack, dict):
        return []
    frames = resolved_stack.get("resolved_frames")
    if not isinstance(frames, list):
        return []
    return [f for f in frames if isinstance(f, dict)]


def resolved_threads_from_stack(resolved_stack: Any) -> List[Dict[str, Any]]:
    if not isinstance(resolved_stack, dict):
        return []
    threads = resolved_stack.get("resolved_threads")
    if not isinstance(threads, list):
        return []
    return [t for t in threads if isinstance(t, dict)]


def flatten_resolved_frames_from_stack(resolved_stack: Any) -> List[Dict[str, Any]]:
    """从 ``resolved_threads`` 扁平化；无则回退旧版 ``resolved_frames``。"""
    out: List[Dict[str, Any]] = []
    for t in resolved_threads_from_stack(resolved_stack):
        for f in t.get("frames") or []:
            if isinstance(f, dict):
                out.append(f)
    if out:
        return out
    return resolved_frames_from_stack(resolved_stack)


def resolved_frame_has_usable_symbol(frame: Any) -> bool:
    """单帧是否含可用符号/源位置（用户关心的「解析出函数」）。"""
    if not isinstance(frame, dict):
        return False
    for key in ("resolved_function", "function"):
        if _meaningful_symbol(str(frame.get(key) or "")):
            return True
    rf = str(frame.get("resolved_file") or frame.get("file") or "").strip()
    if not rf:
        return False
    line = frame.get("resolved_line", frame.get("line"))
    try:
        line_int = int(line) if line not in (None, "", False) else 0
    except (TypeError, ValueError):
        line_int = 0
    if line_int > 0:
        return True
    return False


def resolved_stack_has_failure(resolved_stack: Any) -> bool:
    if not isinstance(resolved_stack, dict):
        return True
    if str(resolved_stack.get("error") or "").strip():
        return True
    return False


def resolved_stack_has_usable_resolution(resolved_stack: Any) -> bool:
    """02 是否至少有一帧可用于下游源码定位（符号或 file:line）。"""
    if resolved_stack_has_failure(resolved_stack):
        return False
    for t in resolved_threads_from_stack(resolved_stack):
        for f in t.get("frames") or []:
            if isinstance(f, dict) and resolved_frame_has_usable_symbol(f):
                return True
    frames = flatten_resolved_frames_from_stack(resolved_stack)
    if not frames:
        return False
    if any(resolved_frame_has_usable_symbol(f) for f in frames):
        return True
    # 回退：与 01 一致，仅地址也可能触发 symbol-only 兜底
    return any(frame_has_usable_info(f) for f in frames)


def resolved_stack_failure_message(resolved_stack: Any) -> Optional[str]:
    if not isinstance(resolved_stack, dict):
        return "堆栈符号化结果无效"
    err = str(resolved_stack.get("error") or "").strip()
    if err:
        return err
    return None


def resolved_stack_skip_pipeline_message(resolved_stack: Any) -> str:
    fail = resolved_stack_failure_message(resolved_stack)
    if fail:
        return f"{fail} 已终止后续流程。"
    success = resolved_stack.get("success_count") if isinstance(resolved_stack, dict) else None
    total = resolved_stack.get("total_count") if isinstance(resolved_stack, dict) else None
    extra = ""
    if success is not None and total is not None:
        ftotal = resolved_stack.get("frame_count_total") if isinstance(resolved_stack, dict) else None
        if ftotal is not None and ftotal != total:
            extra = f"（成功 {success}/{total} 可符号化帧，日志总帧 {ftotal}）"
        else:
            extra = f"（成功 {success}/{total} 帧）"
    return (
        f"02 中未得到任何可用函数名或 file:line{extra}。"
        "已终止后续流程；请检查 --library-dir 或日志是否已符号化。"
    )


def pipeline_skip_metadata_resolve(
    resolved_stack: Any,
    *,
    reason: str = "no_usable_resolve",
) -> Dict[str, Any]:
    msg = resolved_stack_skip_pipeline_message(resolved_stack)
    return {
        "pipeline_skipped": True,
        "pipeline_skip_reason": reason,
        "pipeline_skip_user_message": msg,
        "llm_skipped": True,
        "llm_skip_reason": reason,
        "llm_skip_user_message": msg,
    }
