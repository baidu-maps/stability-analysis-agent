#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Feature extraction for crash analysis.
"""

from typing import Any, Dict, Tuple


def extract_features(parsed_data: Dict[str, Any], resolved_data: Dict[str, Any], prompt_data: Dict[str, Any]) -> Dict[str, Any]:
    from tools.resolve_stack_errors import flatten_resolved_frames_from_stack

    features: Dict[str, Any] = {}
    crash_info = parsed_data.get("crash_info", {}) if isinstance(parsed_data, dict) else {}
    meta_info = parsed_data.get("meta_info", {}) if isinstance(parsed_data, dict) else {}

    features["OS"] = meta_info.get("os_type") or meta_info.get("platform")
    features["os_type"] = features["OS"]
    features["platform"] = meta_info.get("platform")
    features["signal"] = crash_info.get("signal") or crash_info.get("crash_signal")
    features["crash_reason"] = crash_info.get("crash_reason")
    features["thread_type"] = crash_info.get("thread_type")
    features["exception_type"] = crash_info.get("exception_type")

    # Top frame info
    function_name = None
    module_name = None
    if isinstance(resolved_data, dict):
        frames = flatten_resolved_frames_from_stack(resolved_data)
        if frames:
            function_name = frames[0].get("function")
            module_name = frames[0].get("module")
    if not function_name:
        # 从按线程分组的 threads 中获取主线程的首帧信息
        threads = parsed_data.get("threads") or []
        if threads:
            primary = threads[0]
            frames = primary.get("frames") or []
            if frames:
                function_name = frames[0].get("function")
                module_name = frames[0].get("module")

    features["function"] = function_name
    features["module"] = module_name

    # Stack signature for rule matching
    stack_functions = []
    for f in (
        flatten_resolved_frames_from_stack(resolved_data)
        if isinstance(resolved_data, dict)
        else []
    ):
        if f.get("function"):
            stack_functions.append(f.get("function"))
    features["stack_functions"] = " ".join(stack_functions[:10])

    # Prompt summary hint
    if isinstance(prompt_data, dict):
        features["crash_summary"] = prompt_data.get("crash_summary")

    return features


def build_pattern_query(parsed_data: Dict[str, Any], resolved_data: Dict[str, Any], prompt_data: Dict[str, Any]) -> Tuple[str, str]:
    features = extract_features(parsed_data, resolved_data, prompt_data)
    summary = str(features.get("crash_summary") or "")
    signature_parts = [
        str(features.get("crash_reason") or ""),
        str(features.get("signal") or ""),
        str(features.get("OS") or ""),
        str(features.get("function") or ""),
        str(features.get("module") or ""),
    ]
    signature = " ".join([p for p in signature_parts if p])
    query = " ".join([summary, signature]).strip()
    return query, signature
