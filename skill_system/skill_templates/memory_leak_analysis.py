#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内存泄漏分析技能模板。

提供内存泄漏故障类型的分析框架骨架，供后续填充具体实现。
参考华为 DFX Skills 的 jsleak-analysis / nativeleak-analysis 设计。
"""

from __future__ import annotations
from typing import Any, Dict


# 内存泄漏故障模式（预定义，待后续扩展）
LEAK_FAULT_MODES = {
    "native_heap_leak": {
        "root_cause_l1": "Native 内存泄漏",
        "root_cause_l2": "堆内存未释放",
        "sub_causes": [
            {"root_cause_l3": "malloc/new 后未对应 free/delete", "keywords": ["malloc", "new", "alloc"]},
            {"root_cause_l3": "异常路径跳过释放逻辑", "keywords": ["exception", "throw", "return"]},
            {"root_cause_l3": "容器持续增长无上限", "keywords": ["vector", "map", "cache", "queue"]},
            {"root_cause_l3": "循环引用（shared_ptr 互相持有）", "keywords": ["shared_ptr", "ref_count", "cycle"]},
        ],
    },
    "managed_heap_leak": {
        "root_cause_l1": "托管内存泄漏",
        "root_cause_l2": "GC Root 可达导致无法回收",
        "sub_causes": [
            {"root_cause_l3": "静态/全局集合持有对象引用", "keywords": ["static", "global", "singleton", "registry"]},
            {"root_cause_l3": "闭包/Lambda 捕获 this 或大对象", "keywords": ["closure", "lambda", "callback", "listener"]},
            {"root_cause_l3": "未注销的 Observer/EventListener", "keywords": ["observer", "listener", "subscribe", "register"]},
            {"root_cause_l3": "Activity/Fragment 泄漏（Android）", "keywords": ["Activity", "Fragment", "Context", "View"]},
        ],
    },
    "resource_leak": {
        "root_cause_l1": "资源泄漏",
        "root_cause_l2": "系统资源句柄未关闭",
        "sub_causes": [
            {"root_cause_l3": "文件描述符未关闭 (fd leak)", "keywords": ["fd", "file", "socket", "pipe"]},
            {"root_cause_l3": "线程未回收 (thread leak)", "keywords": ["thread", "pool", "executor"]},
            {"root_cause_l3": "GPU/纹理资源未释放", "keywords": ["texture", "buffer", "gpu", "surface"]},
            {"root_cause_l3": "数据库连接/游标未关闭", "keywords": ["cursor", "connection", "statement", "database"]},
        ],
    },
}


SKILL_DESCRIPTION = """\
# 内存泄漏分析技能

## 适用场景
- 应用内存持续增长
- OOM (Out of Memory) 崩溃
- 系统报告内存不足
- heap dump / memory profile 分析

## 分析流程（框架，待完善）

### Step 1: 泄漏类型判定
- PSS/RSS 持续增长 → Native 堆泄漏
- Managed heap 增长 → GC 对象泄漏
- FD/thread/GPU 增长 → 资源泄漏

### Step 2: 泄漏量化
- 增长速率（MB/min）
- 总泄漏量
- 泄漏对象 Top N

### Step 3: 泄漏源定位
- Native: 分配调用栈 + 未释放块
- Managed: Retainer chain + GC root
- Resource: 分配点 + 生命周期追踪

### Step 4: 故障模式匹配
- 匹配三级故障模式库
- 输出根因 + 泄漏路径 + 修复建议

## 当前状态
阶段 A 已接线（crash 主轨旁路，非独立 workflow）：
- ``tools.memory_diagnosis.run_memory_pressure_diagnosis`` → ``04d_memory_pressure_diagnosis.json``
- 触发：``log_kind ∈ {oom_kill, memory_pressure, mixed_oom_crash}`` / ``oom_suspected`` / ``--force-memory-analysis``
- 完整 heap snapshot diff 仍待后续阶段
"""


def get_leak_skill_metadata() -> Dict[str, Any]:
    """返回技能元数据。"""
    return {
        "name": "memory-leak-analysis",
        "version": "0.2.0",
        "description": "内存压力/OOM 旁路诊断（阶段 A）+ 泄漏模式骨架",
        "type": "workflow",
        "status": "wired_sidepath",
        "fault_modes": LEAK_FAULT_MODES,
    }


def run_memory_leak_analysis(
    parse_result: Dict[str, Any],
    resolved_stack: Dict[str, Any],
    crash_log_content: str = "",
    *,
    force: bool = False,
) -> Any:
    """技能对外入口：委托 ``tools.memory_diagnosis``（阶段 A）。"""
    from tools.memory_diagnosis.core import run_memory_pressure_diagnosis
    return run_memory_pressure_diagnosis(
        parse_result,
        resolved_stack,
        crash_log_content,
        force=force,
    )

