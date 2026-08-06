#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Feature extraction for crash analysis.

扩展特征提取：支持三级故障模式匹配、证据分级、选择性知识加载所需的额外特征。
"""

from typing import Any, Dict, List, Tuple


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

    # --- 新增：崩溃地址特征（用于确定性分析和证据分级） ---
    features["crash_address"] = crash_info.get("crash_address") or crash_info.get("fault_addr")

    # Top frame info
    function_name = None
    module_name = None
    all_frames: List[Dict[str, Any]] = []
    if isinstance(resolved_data, dict):
        all_frames = flatten_resolved_frames_from_stack(resolved_data)
        if all_frames:
            function_name = all_frames[0].get("function")
            module_name = all_frames[0].get("module")
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
    for f in (all_frames if all_frames else []):
        if f.get("function"):
            stack_functions.append(f.get("function"))
    features["stack_functions"] = " ".join(stack_functions[:10])

    # --- 新增：模块列表（用于选择性知识加载） ---
    module_list: List[str] = []
    for f in (all_frames if all_frames else []):
        m = f.get("module")
        if m and m not in module_list:
            module_list.append(m)
    features["module_list"] = module_list

    # --- 新增：多线程证据（用于并发故障模式匹配） ---
    thread_count = 0
    if isinstance(parsed_data, dict):
        threads = parsed_data.get("threads") or []
        thread_count = len(threads)
    features["thread_count"] = thread_count
    features["multi_thread_evidence"] = thread_count > 1

    # --- 新增：故障地址范围判定（用于确定性空指针判断） ---
    crash_addr = features.get("crash_address")
    if crash_addr:
        try:
            addr_str = str(crash_addr)
            addr_val = int(addr_str, 16) if addr_str.startswith("0x") else int(addr_str, 0)
            features["fault_address_range"] = (
                "near_zero" if addr_val < 0x1000
                else "low" if addr_val < 0x10000
                else "normal"
            )
        except (ValueError, TypeError):
            features["fault_address_range"] = "unknown"
    else:
        features["fault_address_range"] = "unknown"

    # Prompt summary hint
    if isinstance(prompt_data, dict):
        features["crash_summary"] = prompt_data.get("crash_summary")

    # --- 新增：信号子码语义（用于增强故障模式规则匹配） ---
    try:
        from tools.crash_parser.signal_semantics import get_signal_semantics
        signal_str = str(features.get("signal") or "")
        if signal_str:
            semantics = get_signal_semantics(signal_str)
            if semantics:
                features["signal_sub_code"] = semantics.get("sub_code", "")
                features["signal_hint"] = semantics.get("hint", "")
                features["signal_likely_causes"] = semantics.get("likely_root_cause", [])
    except Exception:
        pass

    # --- 新增：崩溃地址模式特征（用于增强地址相关规则） ---
    try:
        from tools.crash_parser.address_pattern_analyzer import analyze_crash_address
        if crash_addr:
            addr_analysis = analyze_crash_address(str(crash_addr))
            features["address_pattern"] = addr_analysis.get("pattern", "")
            features["address_region"] = addr_analysis.get("address_region", "")
    except Exception:
        pass

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


def extract_module_list(resolved_data: Dict[str, Any], parsed_data: Dict[str, Any]) -> List[str]:
    """提取调用栈中涉及的所有 module 名（去重保序）。

    用于选择性知识加载（ModuleKnowledgeRouter）。
    """
    from tools.resolve_stack_errors import flatten_resolved_frames_from_stack

    modules: List[str] = []
    frames = (
        flatten_resolved_frames_from_stack(resolved_data)
        if isinstance(resolved_data, dict)
        else []
    )
    if not frames:
        threads = parsed_data.get("threads") or [] if isinstance(parsed_data, dict) else []
        for t in threads:
            for f in (t.get("frames") or []):
                m = f.get("module")
                if m and m not in modules:
                    modules.append(m)
        return modules

    for f in frames:
        m = f.get("module")
        if m and m not in modules:
            modules.append(m)
    return modules
