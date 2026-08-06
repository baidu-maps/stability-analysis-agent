#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ANR/Freeze (冻屏/卡死) 分析技能。

接线工具：
- ``tools.stack_hotspot_analyzer.StackHotspotAnalyzer``
- ``tools.event_handler_analyzer.EventHandlerAnalyzer`` / ``BinderChainTracer``

编排入口：``tools.anr_diagnosis.run_anr_freeze_diagnosis``
主路径：CLI 按 ``log_kind`` 路由 ``workflows.anr_freeze_workflow``；
crash workflow 仅作预分类失败时的兜底旁路。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


# ANR/Freeze 故障模式（供诊断匹配与文档）
ANR_FAULT_MODES = {
    "main_thread_blocking": {
        "root_cause_l1": "主线程卡死超时",
        "root_cause_l2": "主线程阻塞",
        "sub_causes": [
            {"root_cause_l3": "等锁（pthread_mutex_lock / @synchronized）", "keywords": ["mutex", "lock", "synchronized"]},
            {"root_cause_l3": "同步 IPC/Binder 调用阻塞", "keywords": ["binder", "transact", "aidl"]},
            {"root_cause_l3": "同步 IO 操作（文件/网络/数据库）", "keywords": ["read", "write", "connect", "sqlite"]},
            {"root_cause_l3": "主线程等待子线程完成", "keywords": ["join", "wait", "future.get", "dispatch_sync"]},
        ],
    },
    "main_thread_busy": {
        "root_cause_l1": "主线程卡死超时",
        "root_cause_l2": "主线程繁忙",
        "sub_causes": [
            {"root_cause_l3": "业务计算密集（CPU-bound）", "keywords": ["sort", "loop", "calculate"]},
            {"root_cause_l3": "UI 布局/渲染过重", "keywords": ["layout", "measure", "draw", "bindView"]},
            {"root_cause_l3": "大量序列化/反序列化", "keywords": ["JSON", "protobuf", "parse", "encode"]},
        ],
    },
    "system_overload": {
        "root_cause_l1": "主线程卡死超时",
        "root_cause_l2": "系统高负载",
        "sub_causes": [
            {"root_cause_l3": "CPU 整体高负载（>85%）", "keywords": ["cpu", "load", "throttle"]},
            {"root_cause_l3": "内存水位过低（内存回收压力）", "keywords": ["memory", "lowmem", "oom", "pressure"]},
            {"root_cause_l3": "热限频（温度过高导致降频）", "keywords": ["thermal", "temperature", "throttle"]},
        ],
    },
}


SKILL_DESCRIPTION = """\
# ANR/Freeze 冻屏分析技能

## 适用场景
- 应用无响应（ANR）
- 界面冻屏/卡死
- 操作超时无反馈
- watchdog 超时

## 已接线工具
1. StackHotspotAnalyzer — 多线程栈热点 / 阻塞迹象 / 重复调用模式
2. EventHandlerAnalyzer — 消息队列耗时任务（日志含队列片段时）
3. BinderChainTracer — Binder/IPC 等待链与死锁环（日志含 binder 片段时）

## 触发条件
- CLI / daemon 预分类 ``log_kind ∈ {anr_trace, app_freeze, watchdog, mixed_anr_crash}``，或
- CLI ``--force-anr-analysis``，或
- crash workflow 兜底：``01.meta_info.log_kind`` 属 ANR 族 / 兼容 ``anr_suspected``

## 产物
- ``04c_anr_freeze_diagnosis.json``
- 提示词段落 ``prompt_section_zh``（注入 LLM / gen_prompt，不改动 05 既有 01/02/03 装配结构以外的旁路规则）
"""


def get_anr_skill_metadata() -> Dict[str, Any]:
    """返回技能元数据。"""
    return {
        "name": "anr-freeze-analysis",
        "version": "0.2.0",
        "description": "ANR/Freeze 冻屏问题分析技能（热点栈 + 队列/Binder）",
        "type": "workflow",
        "status": "wired",
        "fault_modes": ANR_FAULT_MODES,
        "tools": [
            "stack_hotspot_analyzer",
            "event_handler_analyzer",
            "binder_chain_tracer",
        ],
    }


def run_anr_freeze_analysis(
    parse_result: Dict[str, Any],
    resolved_stack: Dict[str, Any],
    crash_log_content: str = "",
    *,
    force: bool = False,
) -> Optional[Dict[str, Any]]:
    """技能对外入口：委托 ``tools.anr_diagnosis``。"""
    from tools.anr_diagnosis.core import run_anr_freeze_diagnosis
    return run_anr_freeze_diagnosis(
        parse_result,
        resolved_stack,
        crash_log_content,
        force=force,
    )
