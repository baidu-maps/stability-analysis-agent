#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""crash_log_parser（01）可用性判断与用户可见跳过说明。"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_HEX_ONLY_RE = re.compile(r"^0x[0-9a-fA-F]+\s*$")
_HEX_OFFSET_RE = re.compile(r"^0x[0-9a-fA-F]+\s+\+\s+\d+\s*$")


def flatten_frames_from_parse_result(parse_result: Any) -> List[Dict[str, Any]]:
    """从 01 结构扁平化堆栈帧（支持 threads 与旧 stack_frames）。"""
    if not isinstance(parse_result, dict):
        return []
    if isinstance(parse_result.get("stack_frames"), list):
        return [f for f in parse_result["stack_frames"] if isinstance(f, dict)]
    threads = parse_result.get("threads")
    if not isinstance(threads, list):
        return []
    out: List[Dict[str, Any]] = []
    for t in threads:
        if not isinstance(t, dict):
            continue
        frames = t.get("frames")
        if isinstance(frames, list):
            out.extend(f for f in frames if isinstance(f, dict))
    return out


def _meaningful_symbol(name: str) -> bool:
    s = str(name or "").strip()
    if len(s) < 3:
        return False
    if _HEX_ONLY_RE.match(s) or _HEX_OFFSET_RE.match(s):
        return False
    return True


def frame_has_usable_info(frame: Any) -> bool:
    """单帧是否含可继续下游分析的信息（地址 / 符号 / 源位置）。"""
    if not isinstance(frame, dict):
        return False
    addr = str(frame.get("address") or "").strip()
    if addr.lower().startswith("0x") and len(addr) > 2:
        return True
    for key in ("function", "resolved_function"):
        if _meaningful_symbol(str(frame.get(key) or "")):
            return True
    ff = str(frame.get("file") or "").strip()
    rr = str(frame.get("resolved_file") or "").strip()
    if ff or rr:
        return True
    for key in ("line", "resolved_line"):
        val = frame.get(key)
        if val not in (None, "", 0, "0"):
            return True
    return False


def parse_result_has_failure(parse_result: Any) -> bool:
    """01 顶层解析失败（异常或 parse_status=error）。"""
    if not isinstance(parse_result, dict):
        return True
    if str(parse_result.get("error") or "").strip():
        return True
    if str(parse_result.get("parse_status") or "").strip().lower() == "error":
        return True
    return False


def parse_result_has_usable_crash_data(parse_result: Any) -> bool:
    """01 是否提取到至少一帧可用于符号化/分析的关键信息。"""
    if not isinstance(parse_result, dict):
        return False
    if parse_result_has_failure(parse_result):
        return False
    if not isinstance(parse_result.get("meta_info"), dict):
        return False
    frames = flatten_frames_from_parse_result(parse_result)
    if not frames:
        return False
    return any(frame_has_usable_info(f) for f in frames)


def parse_result_failure_message(parse_result: Any) -> Optional[str]:
    if not isinstance(parse_result, dict):
        return "崩溃日志解析结果无效"
    err = str(parse_result.get("error") or "").strip()
    if err:
        return err
    if str(parse_result.get("parse_status") or "").strip().lower() == "error":
        return "未能从日志中提取任何堆栈帧（parse_status=error）"
    return None


def parse_result_skip_pipeline_message(parse_result: Any) -> str:
    fail = parse_result_failure_message(parse_result)
    if fail:
        return f"{fail} 已终止后续流程。"
    return (
        "01 中未包含可用堆栈信息（无有效地址/符号/源位置）。"
        "已终止后续流程；请检查日志格式或 --crash-segment-index。"
    )


def pipeline_skip_metadata(parse_result: Any, *, reason: str = "no_usable_parse") -> Dict[str, Any]:
    return {
        "pipeline_skipped": True,
        "pipeline_skip_reason": reason,
        "pipeline_skip_user_message": parse_result_skip_pipeline_message(parse_result),
        # 与 03 跳过共用：后续步骤均不调用 LLM
        "llm_skipped": True,
        "llm_skip_reason": reason,
        "llm_skip_user_message": parse_result_skip_pipeline_message(parse_result),
    }
