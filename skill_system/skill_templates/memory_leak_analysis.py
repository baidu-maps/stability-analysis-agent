#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内存泄漏分析技能入口。

Crash 日志走 04d 内存压力旁路；HarmonyOS Native 泄漏采集包由
``tools.native_leak_diagnosis`` 执行完整的确定性分析。
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

## 分析流程

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
阶段 A（Crash 主轨旁路）：
- ``tools.memory_diagnosis.run_memory_pressure_diagnosis`` → ``04d_memory_pressure_diagnosis.json``
- 触发：``log_kind ∈ {oom_kill, memory_pressure, mixed_oom_crash}`` / ``oom_suspected`` / ``--force-memory-analysis``

阶段 B（HarmonyOS Native 泄漏）：
- ``native_leak_analysis`` 独立 workflow 与 ``native_leak_analyzer`` Tool
- sample 趋势、smaps 分类、NMD diff、native_hook 未释放调用栈、kernel DMA 归属
- CLI：``sa-agent native-leak --input <dir> [--trace-db <db>]``
- 可通过 ``--native-leak-dir`` 合并到 Crash 04d sidecar

ArkTS heapsnapshot retainer-chain 分析仍需专用 heap snapshot 能力。
"""


def get_leak_skill_metadata() -> Dict[str, Any]:
    """返回技能元数据。"""
    return {
        "name": "memory-leak-analysis",
        "version": "1.0.0",
        "description": "内存压力/OOM 旁路 + HarmonyOS Native 泄漏确定性分析",
        "type": "workflow",
        "status": "implemented",
        "fault_modes": LEAK_FAULT_MODES,
    }


def run_memory_leak_analysis(
    parse_result: Dict[str, Any],
    resolved_stack: Dict[str, Any],
    crash_log_content: str = "",
    *,
    force: bool = False,
    native_leak_path: str = "",
    native_leak_trace_db: str = "",
) -> Any:
    """技能对外入口：委托 04d，并可合并 Native 泄漏采集证据。"""
    from tools.memory_diagnosis.core import run_memory_pressure_diagnosis
    return run_memory_pressure_diagnosis(
        parse_result,
        resolved_stack,
        crash_log_content,
        force=force,
        native_leak_path=native_leak_path,
        native_leak_trace_db=native_leak_trace_db,
    )
