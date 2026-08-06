#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
向量数据库初始化脚本（规则 + 模式 + 证据 + 策略 + 指导片段）

每次运行会先清空本地 vector_db 再写入下列静态种子，便于与
crash_cases/demo_basic 等常见崩溃类型（空指针、UAF/悬空、越界、除零、错误转换、栈溢出、
abort、SIGBUS、SIGILL、double free、空函数指针）以及常见并发/多线程问题对齐。

用法（在项目根目录）:
  python3 tools/core/rag/init_vector_db_data.py
"""

import sys
import json
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from stability_analyzer_agent.rag.vector_database_integration import AIStabilityAnalyzerWithVectorDB
except ImportError:
    from rag.vector_database_integration import AIStabilityAnalyzerWithVectorDB


def init_rules(analyzer: AIStabilityAnalyzerWithVectorDB) -> None:
    print("正在初始化规则表...")
    now = datetime.now().isoformat()
    rules = [
        {
            "rule_id": "rule_sigsegv_destructor_async",
            "rule_name": "析构后异步回调访问",
            "trigger_condition": "signal~=SIGSEGV AND function~=~operator delete",
            "required_features": ["signal", "function"],
            "conclusion_type": "lifecycle_issue",
            "conclusion_payload": {"pattern": "uaf", "hint": "析构后仍被访问"},
            "confidence_score": 0.92,
            "enabled": True,
            "created_at": now,
        },
        {
            "rule_id": "rule_nullptr_sigsegv",
            "rule_name": "空指针访问 SIGSEGV（日志含 null pointer 等描述）",
            "trigger_condition": "signal~=SIGSEGV AND crash_reason~=null pointer",
            "required_features": ["signal", "crash_reason"],
            "conclusion_type": "bug_type",
            "conclusion_payload": {"pattern": "null_deref"},
            "confidence_score": 0.88,
            "enabled": True,
            "created_at": now,
        },
        {
            "rule_id": "rule_nullptr_stack_symbol",
            "rule_name": "空指针（栈符号含 crash_nullptr）",
            "trigger_condition": "stack_functions~=crash_nullptr",
            "required_features": ["stack_functions"],
            "conclusion_type": "bug_type",
            "conclusion_payload": {"pattern": "null_deref"},
            "confidence_score": 0.86,
            "enabled": True,
            "created_at": now,
        },
        {
            "rule_id": "rule_stack_overflow_reason",
            "rule_name": "递归导致栈溢出（日志含 stack overflow）",
            "trigger_condition": "crash_reason~=stack overflow",
            "required_features": ["crash_reason"],
            "conclusion_type": "bug_type",
            "conclusion_payload": {"pattern": "stack_overflow"},
            "confidence_score": 0.9,
            "enabled": True,
            "created_at": now,
        },
        {
            "rule_id": "rule_stack_overflow_symbol",
            "rule_name": "栈溢出（栈符号含 crash_stackoverflow）",
            "trigger_condition": "stack_functions~=crash_stackoverflow",
            "required_features": ["stack_functions"],
            "conclusion_type": "bug_type",
            "conclusion_payload": {"pattern": "stack_overflow"},
            "confidence_score": 0.87,
            "enabled": True,
            "created_at": now,
        },
        {
            "rule_id": "rule_divide_by_zero",
            "rule_name": "整数除零 / SIGFPE（栈符号含 crash_divzero）",
            "trigger_condition": "stack_functions~=crash_divzero",
            "required_features": ["stack_functions"],
            "conclusion_type": "bug_type",
            "conclusion_payload": {"pattern": "divide_by_zero"},
            "confidence_score": 0.9,
            "enabled": True,
            "created_at": now,
        },
        {
            "rule_id": "rule_dangling_pointer",
            "rule_name": "悬空指针 / UAF（栈符号含 crash_dangling）",
            "trigger_condition": "stack_functions~=crash_dangling",
            "required_features": ["stack_functions"],
            "conclusion_type": "bug_type",
            "conclusion_payload": {"pattern": "dangling_pointer", "hint": "delete 后仍解引用"},
            "confidence_score": 0.88,
            "enabled": True,
            "created_at": now,
        },
        {
            "rule_id": "rule_out_of_bounds",
            "rule_name": "数组/容器越界（栈符号含 crash_oob）",
            "trigger_condition": "stack_functions~=crash_oob",
            "required_features": ["stack_functions"],
            "conclusion_type": "bug_type",
            "conclusion_payload": {"pattern": "out_of_bounds"},
            "confidence_score": 0.88,
            "enabled": True,
            "created_at": now,
        },
        {
            "rule_id": "rule_bad_cast",
            "rule_name": "错误 dynamic_cast / 类型体系不一致（栈符号含 crash_bad_cast）",
            "trigger_condition": "stack_functions~=crash_bad_cast",
            "required_features": ["stack_functions"],
            "conclusion_type": "bug_type",
            "conclusion_payload": {"pattern": "bad_cast"},
            "confidence_score": 0.85,
            "enabled": True,
            "created_at": now,
        },
        {
            "rule_id": "rule_abort_sigabrt",
            "rule_name": "abort / SIGABRT",
            "trigger_condition": "signal~=SIGABRT",
            "required_features": ["signal"],
            "conclusion_type": "bug_type",
            "conclusion_payload": {"pattern": "abort"},
            "confidence_score": 0.9,
            "enabled": True,
            "created_at": now,
        },
        {
            "rule_id": "rule_abort_stack_symbol",
            "rule_name": "abort（栈符号含 crash_abort）",
            "trigger_condition": "stack_functions~=crash_abort",
            "required_features": ["stack_functions"],
            "conclusion_type": "bug_type",
            "conclusion_payload": {"pattern": "abort"},
            "confidence_score": 0.88,
            "enabled": True,
            "created_at": now,
        },
        {
            "rule_id": "rule_sigbus_signal",
            "rule_name": "SIGBUS 总线错误",
            "trigger_condition": "signal~=SIGBUS",
            "required_features": ["signal"],
            "conclusion_type": "bug_type",
            "conclusion_payload": {"pattern": "sigbus"},
            "confidence_score": 0.9,
            "enabled": True,
            "created_at": now,
        },
        {
            "rule_id": "rule_sigbus_stack_symbol",
            "rule_name": "SIGBUS（栈符号含 crash_sigbus）",
            "trigger_condition": "stack_functions~=crash_sigbus",
            "required_features": ["stack_functions"],
            "conclusion_type": "bug_type",
            "conclusion_payload": {"pattern": "sigbus"},
            "confidence_score": 0.88,
            "enabled": True,
            "created_at": now,
        },
        {
            "rule_id": "rule_sigill_signal",
            "rule_name": "SIGILL 非法指令",
            "trigger_condition": "signal~=SIGILL",
            "required_features": ["signal"],
            "conclusion_type": "bug_type",
            "conclusion_payload": {"pattern": "sigill"},
            "confidence_score": 0.9,
            "enabled": True,
            "created_at": now,
        },
        {
            "rule_id": "rule_sigill_stack_symbol",
            "rule_name": "SIGILL（栈符号含 crash_sigill）",
            "trigger_condition": "stack_functions~=crash_sigill",
            "required_features": ["stack_functions"],
            "conclusion_type": "bug_type",
            "conclusion_payload": {"pattern": "sigill"},
            "confidence_score": 0.88,
            "enabled": True,
            "created_at": now,
        },
        {
            "rule_id": "rule_double_free_stack_symbol",
            "rule_name": "double free（栈符号含 crash_double_free）",
            "trigger_condition": "stack_functions~=crash_double_free",
            "required_features": ["stack_functions"],
            "conclusion_type": "bug_type",
            "conclusion_payload": {"pattern": "double_free"},
            "confidence_score": 0.9,
            "enabled": True,
            "created_at": now,
        },
        {
            "rule_id": "rule_null_func_ptr_stack_symbol",
            "rule_name": "空函数指针调用（栈符号含 crash_null_func_ptr）",
            "trigger_condition": "stack_functions~=crash_null_func_ptr",
            "required_features": ["stack_functions"],
            "conclusion_type": "bug_type",
            "conclusion_payload": {"pattern": "null_function_pointer"},
            "confidence_score": 0.9,
            "enabled": True,
            "created_at": now,
        },
        {
            "rule_id": "rule_concurrency_pthread_api",
            "rule_name": "并发：栈符号含 pthread 同步原语",
            "trigger_condition": "stack_functions~=pthread_mutex|pthread_rwlock|pthread_cond|pthread_join|pthread_once",
            "required_features": ["stack_functions"],
            "conclusion_type": "concurrency_issue",
            "conclusion_payload": {"pattern": "pthread_sync"},
            "confidence_score": 0.84,
            "enabled": True,
            "created_at": now,
        },
        {
            "rule_id": "rule_concurrency_stl_lock",
            "rule_name": "并发：栈符号含 std 锁/条件变量封装",
            "trigger_condition": "stack_functions~=lock_guard|unique_lock|shared_lock|scoped_lock|condition_variable",
            "required_features": ["stack_functions"],
            "conclusion_type": "concurrency_issue",
            "conclusion_payload": {"pattern": "stl_lock"},
            "confidence_score": 0.84,
            "enabled": True,
            "created_at": now,
        },
        {
            "rule_id": "rule_concurrency_dispatch",
            "rule_name": "并发：栈符号含 GCD dispatch",
            "trigger_condition": "stack_functions~=dispatch_async|dispatch_sync|dispatch_barrier",
            "required_features": ["stack_functions"],
            "conclusion_type": "concurrency_issue",
            "conclusion_payload": {"pattern": "gcd_dispatch"},
            "confidence_score": 0.82,
            "enabled": True,
            "created_at": now,
        },
        {
            "rule_id": "rule_concurrency_std_thread",
            "rule_name": "并发：栈符号含 std::thread / join / detach",
            "trigger_condition": "stack_functions~=std::thread|thread::join|thread::detach",
            "required_features": ["stack_functions"],
            "conclusion_type": "concurrency_issue",
            "conclusion_payload": {"pattern": "std_thread"},
            "confidence_score": 0.83,
            "enabled": True,
            "created_at": now,
        },
        {
            "rule_id": "rule_concurrency_future_async",
            "rule_name": "并发：栈符号含 std::async / future / packaged_task",
            "trigger_condition": "stack_functions~=std::async|std::future|std::packaged_task",
            "required_features": ["stack_functions"],
            "conclusion_type": "concurrency_issue",
            "conclusion_payload": {"pattern": "future_async"},
            "confidence_score": 0.82,
            "enabled": True,
            "created_at": now,
        },
    ]
    for r in rules:
        analyzer.add_rule(r)


def init_patterns(analyzer: AIStabilityAnalyzerWithVectorDB) -> None:
    print("正在初始化经验模式索引...")
    now = datetime.now().isoformat()
    patterns = [
        {
            "pattern_id": "pattern_uaf_async",
            "pattern_summary": "对象在析构后仍被异步回调访问，常见于跨线程释放场景",
            "crash_signature": "destructor + async_callback + freed_object",
            "platform_scope": {"os": "Android", "language": "C++"},
            "crash_category": "memory",
            "evidence_requirements": ["stack_trace", "code_snippet"],
            "confidence_score": 0.8,
            "validation_state": "verified",
            "source_type": "internal_case",
            "created_at": now,
        },
        {
            "pattern_id": "pattern_nullptr",
            "pattern_summary": "空指针访问导致崩溃，常见于未检查指针的访问路径",
            "crash_signature": "SIGSEGV + null pointer + deref",
            "platform_scope": {"os": "Linux", "language": "C++"},
            "crash_category": "memory",
            "evidence_requirements": ["stack_trace", "log_fragment"],
            "confidence_score": 0.85,
            "validation_state": "verified",
            "source_type": "internal_case",
            "created_at": now,
        },
        {
            "pattern_id": "pattern_deadlock",
            "pattern_summary": "多线程互斥锁顺序反转导致死锁，触发 watchdog",
            "crash_signature": "deadlock + mutex + watchdog",
            "platform_scope": {"os": "Windows", "language": "C++"},
            "crash_category": "concurrency",
            "evidence_requirements": ["stack_trace"],
            "confidence_score": 0.7,
            "validation_state": "draft",
            "source_type": "synthetic_summary",
            "created_at": now,
        },
        {
            "pattern_id": "pattern_stack_overflow",
            "pattern_summary": "深度递归导致栈空间耗尽，常见于无终止条件或深度过大的递归",
            "crash_signature": "recursive_function + stack overflow + SIGSEGV",
            "platform_scope": {"os": "macos", "language": "C++"},
            "crash_category": "memory",
            "evidence_requirements": ["stack_trace"],
            "confidence_score": 0.78,
            "validation_state": "verified",
            "source_type": "internal_case",
            "created_at": now,
        },
        {
            "pattern_id": "pattern_use_after_free",
            "pattern_summary": "释放后继续访问对象，可能由引用计数不一致导致",
            "crash_signature": "free + later_access + invalid_address",
            "platform_scope": {"os": "iOS", "language": "Objective-C"},
            "crash_category": "memory",
            "evidence_requirements": ["stack_trace", "code_snippet"],
            "confidence_score": 0.75,
            "validation_state": "draft",
            "source_type": "internal_case",
            "created_at": now,
        },
        {
            "pattern_id": "pattern_dangling_heap",
            "pattern_summary": "delete/free 后仍读写同一指针，典型悬空指针与堆 UAF",
            "crash_signature": "SIGSEGV + delete + use_after_free + dangling",
            "platform_scope": {"os": "macos", "language": "C++"},
            "crash_category": "memory",
            "evidence_requirements": ["stack_trace", "code_snippet"],
            "confidence_score": 0.82,
            "validation_state": "verified",
            "source_type": "demo_basic",
            "created_at": now,
        },
        {
            "pattern_id": "pattern_out_of_bounds",
            "pattern_summary": "数组或 std::vector 等下标越界读写导致 SIGSEGV",
            "crash_signature": "SIGSEGV + oob + index >= size",
            "platform_scope": {"os": "macos", "language": "C++"},
            "crash_category": "memory",
            "evidence_requirements": ["stack_trace", "code_snippet"],
            "confidence_score": 0.8,
            "validation_state": "verified",
            "source_type": "demo_basic",
            "created_at": now,
        },
        {
            "pattern_id": "pattern_divide_by_zero",
            "pattern_summary": "整数除零或相关算术异常触发 SIGFPE",
            "crash_signature": "SIGFPE + divide by zero + divisor_guard",
            "platform_scope": {"os": "macos", "language": "C++"},
            "crash_category": "arithmetic",
            "evidence_requirements": ["stack_trace"],
            "confidence_score": 0.83,
            "validation_state": "verified",
            "source_type": "demo_basic",
            "created_at": now,
        },
        {
            "pattern_id": "pattern_bad_cast",
            "pattern_summary": "dynamic_cast 到不兼容派生类型失败或错误类型体系导致崩溃",
            "crash_signature": "SIGSEGV + dynamic_cast + bad_cast",
            "platform_scope": {"os": "macos", "language": "C++"},
            "crash_category": "type_safety",
            "evidence_requirements": ["stack_trace", "code_snippet"],
            "confidence_score": 0.8,
            "validation_state": "verified",
            "source_type": "demo_basic",
            "created_at": now,
        },
        {
            "pattern_id": "pattern_abort_explicit",
            "pattern_summary": "显式 abort/断言失败或致命错误路径调用 std::abort",
            "crash_signature": "SIGABRT + abort + fatal path",
            "platform_scope": {"os": "macos", "language": "C++"},
            "crash_category": "logic",
            "evidence_requirements": ["stack_trace"],
            "confidence_score": 0.82,
            "validation_state": "verified",
            "source_type": "demo_basic",
            "created_at": now,
        },
        {
            "pattern_id": "pattern_sigbus_unaligned_or_bus_error",
            "pattern_summary": "总线错误（SIGBUS），常见于未对齐访问、非法物理地址或映射异常",
            "crash_signature": "SIGBUS + bus error + memory access",
            "platform_scope": {"os": "macos", "language": "C++"},
            "crash_category": "memory",
            "evidence_requirements": ["stack_trace"],
            "confidence_score": 0.81,
            "validation_state": "verified",
            "source_type": "demo_basic",
            "created_at": now,
        },
        {
            "pattern_id": "pattern_sigill_illegal_instruction",
            "pattern_summary": "非法指令（SIGILL），常见于执行到无效指令、指令集不兼容或代码损坏",
            "crash_signature": "SIGILL + illegal instruction",
            "platform_scope": {"os": "macos", "language": "C++"},
            "crash_category": "execution",
            "evidence_requirements": ["stack_trace"],
            "confidence_score": 0.8,
            "validation_state": "verified",
            "source_type": "demo_basic",
            "created_at": now,
        },
        {
            "pattern_id": "pattern_double_free_heap",
            "pattern_summary": "同一堆内存被重复释放（double free），常导致堆一致性检查失败并触发 SIGABRT",
            "crash_signature": "double free + heap corruption + SIGABRT",
            "platform_scope": {"os": "macos", "language": "C++"},
            "crash_category": "memory",
            "evidence_requirements": ["stack_trace", "code_snippet"],
            "confidence_score": 0.84,
            "validation_state": "verified",
            "source_type": "demo_basic",
            "created_at": now,
        },
        {
            "pattern_id": "pattern_null_function_pointer_call",
            "pattern_summary": "调用空函数指针导致控制流跳转到空地址并触发 SIGSEGV",
            "crash_signature": "null function pointer + call + SIGSEGV",
            "platform_scope": {"os": "macos", "language": "C++"},
            "crash_category": "memory",
            "evidence_requirements": ["stack_trace", "code_snippet"],
            "confidence_score": 0.82,
            "validation_state": "verified",
            "source_type": "demo_basic",
            "created_at": now,
        },
        {
            "pattern_id": "pattern_data_race_unprotected_shared_state",
            "pattern_summary": "多线程读写共享可变状态但未统一加锁或原子保护，表现为间歇性崩溃或内存破坏",
            "crash_signature": "data race + shared mutable + missing synchronization",
            "platform_scope": {"os": "Linux", "language": "C++"},
            "crash_category": "concurrency",
            "evidence_requirements": ["stack_trace", "code_snippet", "log_fragment"],
            "confidence_score": 0.79,
            "validation_state": "verified",
            "source_type": "seed_concurrency",
            "created_at": now,
        },
        {
            "pattern_id": "pattern_atomic_visibility_release_acquire",
            "pattern_summary": "原子变量或发布-订阅更新缺少正确的 memory_order，导致可见性错误与数据竞争",
            "crash_signature": "atomic + wrong memory_order + visibility bug",
            "platform_scope": {"os": "Linux", "language": "C++"},
            "crash_category": "concurrency",
            "evidence_requirements": ["stack_trace", "code_snippet"],
            "confidence_score": 0.77,
            "validation_state": "verified",
            "source_type": "seed_concurrency",
            "created_at": now,
        },
        {
            "pattern_id": "pattern_nonrecursive_mutex_double_lock",
            "pattern_summary": "在同一线程对非递归互斥锁二次加锁导致死锁或运行时检测中止",
            "crash_signature": "std::mutex + double lock + non-recursive",
            "platform_scope": {"os": "macos", "language": "C++"},
            "crash_category": "concurrency",
            "evidence_requirements": ["stack_trace", "code_snippet"],
            "confidence_score": 0.8,
            "validation_state": "verified",
            "source_type": "seed_concurrency",
            "created_at": now,
        },
        {
            "pattern_id": "pattern_abba_lock_order_inversion",
            "pattern_summary": "两把锁以相反顺序在不同线程获取（AB-BA），形成经典死锁",
            "crash_signature": "mutex A + mutex B + lock order inversion",
            "platform_scope": {"os": "Windows", "language": "C++"},
            "crash_category": "concurrency",
            "evidence_requirements": ["stack_trace", "log_fragment"],
            "confidence_score": 0.81,
            "validation_state": "verified",
            "source_type": "seed_concurrency",
            "created_at": now,
        },
        {
            "pattern_id": "pattern_condition_variable_wait_notify_mismatch",
            "pattern_summary": "条件变量使用不当：未与谓词循环配合、notify 丢失或虚假唤醒未处理",
            "crash_signature": "condition_variable + wait + predicate + lost wakeup",
            "platform_scope": {"os": "Linux", "language": "C++"},
            "crash_category": "concurrency",
            "evidence_requirements": ["stack_trace", "code_snippet"],
            "confidence_score": 0.78,
            "validation_state": "verified",
            "source_type": "seed_concurrency",
            "created_at": now,
        },
        {
            "pattern_id": "pattern_use_after_thread_join_or_exit",
            "pattern_summary": "线程已 join/detach 结束后仍访问其栈上对象或线程局部资源",
            "crash_signature": "thread join + stack use-after-return + UAF",
            "platform_scope": {"os": "macos", "language": "C++"},
            "crash_category": "concurrency",
            "evidence_requirements": ["stack_trace", "code_snippet"],
            "confidence_score": 0.8,
            "validation_state": "verified",
            "source_type": "seed_concurrency",
            "created_at": now,
        },
        {
            "pattern_id": "pattern_destroy_sync_primitive_in_use",
            "pattern_summary": "在仍有线程持有锁或等待条件变量时销毁 mutex/cond 等同步对象",
            "crash_signature": "mutex destroy + still locked + undefined behavior",
            "platform_scope": {"os": "Android", "language": "C++"},
            "crash_category": "concurrency",
            "evidence_requirements": ["stack_trace", "code_snippet"],
            "confidence_score": 0.79,
            "validation_state": "verified",
            "source_type": "seed_concurrency",
            "created_at": now,
        },
        {
            "pattern_id": "pattern_producer_consumer_queue_race",
            "pattern_summary": "无锁或半无锁队列在多生产者/多消费者下未正确同步头尾指针，导致越界或 UAF",
            "crash_signature": "MPMC queue + head/tail race + SIGSEGV",
            "platform_scope": {"os": "Linux", "language": "C++"},
            "crash_category": "concurrency",
            "evidence_requirements": ["stack_trace", "code_snippet"],
            "confidence_score": 0.77,
            "validation_state": "draft",
            "source_type": "seed_concurrency",
            "created_at": now,
        },
        {
            "pattern_id": "pattern_threadpool_task_after_shutdown",
            "pattern_summary": "线程池已关闭或进入析构后仍提交任务或执行回调，导致访问已释放对象",
            "crash_signature": "thread pool shutdown + late task + UAF",
            "platform_scope": {"os": "Linux", "language": "C++"},
            "crash_category": "concurrency",
            "evidence_requirements": ["stack_trace", "code_snippet"],
            "confidence_score": 0.78,
            "validation_state": "verified",
            "source_type": "seed_concurrency",
            "created_at": now,
        },
        {
            "pattern_id": "pattern_cross_thread_callback_weak_ptr",
            "pattern_summary": "跨线程回调未用 weak_ptr/显式取消注册，对象销毁后异步仍触发",
            "crash_signature": "callback + weak_ptr missing + post to queue + UAF",
            "platform_scope": {"os": "Android", "language": "C++"},
            "crash_category": "concurrency",
            "evidence_requirements": ["stack_trace", "code_snippet"],
            "confidence_score": 0.8,
            "validation_state": "verified",
            "source_type": "seed_concurrency",
            "created_at": now,
        },
        {
            "pattern_id": "pattern_main_thread_block_deadlock",
            "pattern_summary": "主线程持锁等待子线程，子线程又同步等待主线程，形成死锁并触发 watchdog/ANR 类症状",
            "crash_signature": "main thread + synchronous wait + circular wait",
            "platform_scope": {"os": "Android", "language": "C++"},
            "crash_category": "concurrency",
            "evidence_requirements": ["stack_trace", "log_fragment"],
            "confidence_score": 0.76,
            "validation_state": "draft",
            "source_type": "seed_concurrency",
            "created_at": now,
        },
        {
            "pattern_id": "pattern_lockfree_memory_order_bug",
            "pattern_summary": "无锁结构依赖错误 memory_order 或缺少 happens-before，导致读到半初始化数据",
            "crash_signature": "lock-free + relaxed order + torn read",
            "platform_scope": {"os": "Linux", "language": "C++"},
            "crash_category": "concurrency",
            "evidence_requirements": ["stack_trace", "code_snippet"],
            "confidence_score": 0.75,
            "validation_state": "draft",
            "source_type": "seed_concurrency",
            "created_at": now,
        },
        {
            "pattern_id": "pattern_semaphore_count_mismatch",
            "pattern_summary": "信号量/计数器与资源实际数量不一致，导致过度 post 或永久 wait 引发崩溃或僵死",
            "crash_signature": "semaphore + count mismatch + deadlock or heap corruption",
            "platform_scope": {"os": "macos", "language": "C++"},
            "crash_category": "concurrency",
            "evidence_requirements": ["stack_trace", "code_snippet"],
            "confidence_score": 0.74,
            "validation_state": "draft",
            "source_type": "seed_concurrency",
            "created_at": now,
        },
    ]
    for p in patterns:
        analyzer.add_pattern(p)


def init_evidence(analyzer: AIStabilityAnalyzerWithVectorDB) -> None:
    print("正在初始化证据表...")
    now = datetime.now().isoformat()
    evidence_list = [
        {
            "evidence_id": "evidence_uaf_001",
            "pattern_id": "pattern_uaf_async",
            "evidence_type": "stack_trace",
            "raw_content": json.dumps({"function": "~Foo", "module": "libfoo.so"}, ensure_ascii=False),
            "normalized_features": {"function": "~Foo", "lifecycle": "destructor"},
            "reliability_score": 0.8,
            "created_at": now,
        },
        {
            "evidence_id": "evidence_nullptr_001",
            "pattern_id": "pattern_nullptr",
            "evidence_type": "log_fragment",
            "raw_content": "Cause: null pointer dereference",
            "normalized_features": {"signal": "SIGSEGV"},
            "reliability_score": 0.7,
            "created_at": now,
        },
        {
            "evidence_id": "evidence_stack_001",
            "pattern_id": "pattern_stack_overflow",
            "evidence_type": "stack_trace",
            "raw_content": json.dumps({"function": "recursive_function", "depth": 1024}, ensure_ascii=False),
            "normalized_features": {"function": "recursive_function"},
            "reliability_score": 0.75,
            "created_at": now,
        },
        {
            "evidence_id": "evidence_dangling_001",
            "pattern_id": "pattern_dangling_heap",
            "evidence_type": "stack_trace",
            "raw_content": json.dumps({"function": "crash_dangling", "after": "delete"}, ensure_ascii=False),
            "normalized_features": {"memory": "heap", "access": "use_after_free"},
            "reliability_score": 0.78,
            "created_at": now,
        },
        {
            "evidence_id": "evidence_oob_001",
            "pattern_id": "pattern_out_of_bounds",
            "evidence_type": "stack_trace",
            "raw_content": json.dumps({"function": "crash_oob", "container": "vector"}, ensure_ascii=False),
            "normalized_features": {"bug": "index_out_of_range"},
            "reliability_score": 0.76,
            "created_at": now,
        },
        {
            "evidence_id": "evidence_divzero_001",
            "pattern_id": "pattern_divide_by_zero",
            "evidence_type": "stack_trace",
            "raw_content": json.dumps({"function": "crash_divzero", "signal": "SIGFPE"}, ensure_ascii=False),
            "normalized_features": {"arithmetic": "divide_by_zero"},
            "reliability_score": 0.8,
            "created_at": now,
        },
        {
            "evidence_id": "evidence_bad_cast_001",
            "pattern_id": "pattern_bad_cast",
            "evidence_type": "code_snippet",
            "raw_content": "dynamic_cast<Derived*>(base) // 可能返回 nullptr 或类型不一致",
            "normalized_features": {"cast": "dynamic_cast"},
            "reliability_score": 0.74,
            "created_at": now,
        },
        {
            "evidence_id": "evidence_abort_001",
            "pattern_id": "pattern_abort_explicit",
            "evidence_type": "log_fragment",
            "raw_content": "SIGABRT: abort() called",
            "normalized_features": {"signal": "SIGABRT"},
            "reliability_score": 0.77,
            "created_at": now,
        },
        {
            "evidence_id": "evidence_sigbus_001",
            "pattern_id": "pattern_sigbus_unaligned_or_bus_error",
            "evidence_type": "log_fragment",
            "raw_content": "SIGBUS: bus error",
            "normalized_features": {"signal": "SIGBUS"},
            "reliability_score": 0.76,
            "created_at": now,
        },
        {
            "evidence_id": "evidence_sigill_001",
            "pattern_id": "pattern_sigill_illegal_instruction",
            "evidence_type": "log_fragment",
            "raw_content": "SIGILL: illegal instruction",
            "normalized_features": {"signal": "SIGILL"},
            "reliability_score": 0.76,
            "created_at": now,
        },
        {
            "evidence_id": "evidence_double_free_001",
            "pattern_id": "pattern_double_free_heap",
            "evidence_type": "code_snippet",
            "raw_content": "free(p); free(p); // 重复释放",
            "normalized_features": {"memory": "heap", "bug": "double_free"},
            "reliability_score": 0.82,
            "created_at": now,
        },
        {
            "evidence_id": "evidence_null_func_ptr_001",
            "pattern_id": "pattern_null_function_pointer_call",
            "evidence_type": "code_snippet",
            "raw_content": "using Func = void (*)(); Func f = nullptr; f();",
            "normalized_features": {"pointer_type": "function_ptr", "null_call": True},
            "reliability_score": 0.8,
            "created_at": now,
        },
        {
            "evidence_id": "evidence_deadlock_001",
            "pattern_id": "pattern_deadlock",
            "evidence_type": "stack_trace",
            "raw_content": json.dumps(
                {"threads": ["worker", "io"], "blocked_on": ["mutex_B", "mutex_A"]},
                ensure_ascii=False,
            ),
            "normalized_features": {"symptom": "deadlock", "lock_order": "ABBA"},
            "reliability_score": 0.78,
            "created_at": now,
        },
        {
            "evidence_id": "evidence_data_race_001",
            "pattern_id": "pattern_data_race_unprotected_shared_state",
            "evidence_type": "code_snippet",
            "raw_content": "shared_counter++; // 多线程未加锁",
            "normalized_features": {"sync": "none", "access": "read_write_race"},
            "reliability_score": 0.77,
            "created_at": now,
        },
        {
            "evidence_id": "evidence_atomic_order_001",
            "pattern_id": "pattern_atomic_visibility_release_acquire",
            "evidence_type": "code_snippet",
            "raw_content": "flag.store(true, std::memory_order_relaxed); // 发布侧缺 release",
            "normalized_features": {"memory_order": "relaxed", "bug": "visibility"},
            "reliability_score": 0.76,
            "created_at": now,
        },
        {
            "evidence_id": "evidence_double_lock_001",
            "pattern_id": "pattern_nonrecursive_mutex_double_lock",
            "evidence_type": "code_snippet",
            "raw_content": "mtx.lock(); foo(); mtx.lock(); // 同线程二次加锁",
            "normalized_features": {"mutex": "nonrecursive", "bug": "double_lock"},
            "reliability_score": 0.79,
            "created_at": now,
        },
        {
            "evidence_id": "evidence_abba_001",
            "pattern_id": "pattern_abba_lock_order_inversion",
            "evidence_type": "log_fragment",
            "raw_content": "Thread1: lock(A) then wait(B); Thread2: lock(B) then wait(A)",
            "normalized_features": {"deadlock_class": "ABBA"},
            "reliability_score": 0.78,
            "created_at": now,
        },
        {
            "evidence_id": "evidence_condvar_001",
            "pattern_id": "pattern_condition_variable_wait_notify_mismatch",
            "evidence_type": "code_snippet",
            "raw_content": "cv.wait(lock); // 未使用谓词循环，可能丢失唤醒",
            "normalized_features": {"primitive": "condition_variable", "bug": "lost_wakeup"},
            "reliability_score": 0.77,
            "created_at": now,
        },
        {
            "evidence_id": "evidence_use_after_join_001",
            "pattern_id": "pattern_use_after_thread_join_or_exit",
            "evidence_type": "code_snippet",
            "raw_content": "Worker w; std::thread t([&]{ w.run(); }); t.join(); use(w.local_buf);",
            "normalized_features": {"lifecycle": "thread_join", "bug": "stack_uar"},
            "reliability_score": 0.78,
            "created_at": now,
        },
        {
            "evidence_id": "evidence_destroy_sync_001",
            "pattern_id": "pattern_destroy_sync_primitive_in_use",
            "evidence_type": "code_snippet",
            "raw_content": "pthread_mutex_destroy(&m); // 仍有线程持有 m",
            "normalized_features": {"api": "pthread_mutex_destroy", "bug": "destroy_while_held"},
            "reliability_score": 0.8,
            "created_at": now,
        },
        {
            "evidence_id": "evidence_mpmc_queue_001",
            "pattern_id": "pattern_producer_consumer_queue_race",
            "evidence_type": "code_snippet",
            "raw_content": "head++; tail++; // 无原子或锁保护的多生产者入队",
            "normalized_features": {"queue": "MPMC", "bug": "torn_update"},
            "reliability_score": 0.75,
            "created_at": now,
        },
        {
            "evidence_id": "evidence_threadpool_shutdown_001",
            "pattern_id": "pattern_threadpool_task_after_shutdown",
            "evidence_type": "log_fragment",
            "raw_content": "ThreadPool::~ThreadPool: join workers; task still scheduled",
            "normalized_features": {"component": "thread_pool", "bug": "late_task"},
            "reliability_score": 0.77,
            "created_at": now,
        },
        {
            "evidence_id": "evidence_weak_callback_001",
            "pattern_id": "pattern_cross_thread_callback_weak_ptr",
            "evidence_type": "code_snippet",
            "raw_content": "dispatch_async(q, ^{ [self doWork]; }); // self 已释放",
            "normalized_features": {"callback": "cross_thread", "lifetime": "raw_pointer"},
            "reliability_score": 0.79,
            "created_at": now,
        },
        {
            "evidence_id": "evidence_main_deadlock_001",
            "pattern_id": "pattern_main_thread_block_deadlock",
            "evidence_type": "log_fragment",
            "raw_content": "watchdog: main thread blocked > Ns on mutex / condition",
            "normalized_features": {"symptom": "watchdog", "thread": "main"},
            "reliability_score": 0.74,
            "created_at": now,
        },
        {
            "evidence_id": "evidence_lockfree_order_001",
            "pattern_id": "pattern_lockfree_memory_order_bug",
            "evidence_type": "code_snippet",
            "raw_content": "auto v = node->next.load(std::memory_order_relaxed); // 读到半初始化节点",
            "normalized_features": {"structure": "lockfree_stack", "bug": "memory_order"},
            "reliability_score": 0.76,
            "created_at": now,
        },
        {
            "evidence_id": "evidence_semaphore_001",
            "pattern_id": "pattern_semaphore_count_mismatch",
            "evidence_type": "code_snippet",
            "raw_content": "sem_post x3; sem_wait x2; // 计数与资源不一致",
            "normalized_features": {"primitive": "semaphore", "bug": "count_drift"},
            "reliability_score": 0.73,
            "created_at": now,
        },
    ]
    for ev in evidence_list:
        analyzer.add_evidence(ev)


def init_strategies(analyzer: AIStabilityAnalyzerWithVectorDB) -> None:
    print("正在初始化修复策略表...")
    now = datetime.now().isoformat()
    strategies = [
        {
            "strategy_id": "strategy_avoid_uaf",
            "applicable_pattern_ids": [
                "pattern_uaf_async",
                "pattern_use_after_free",
                "pattern_dangling_heap",
                "pattern_cross_thread_callback_weak_ptr",
                "pattern_threadpool_task_after_shutdown",
                "pattern_use_after_thread_join_or_exit",
            ],
            "fix_intent": "避免 UAF：延迟释放、弱引用、显式生命周期协议或智能指针",
            "constraints": {"perf": "medium", "platform": ["Android", "iOS", "macos"]},
            "risk_level": "medium",
            "confidence_score": 0.72,
            "example_diff": None,
            "notes": "适合异步回调与 delete 后仍可能访问的路径",
            "created_at": now,
        },
        {
            "strategy_id": "strategy_null_check",
            "applicable_pattern_ids": ["pattern_nullptr"],
            "fix_intent": "增加空指针检查或提前返回；用 optional/断言约束前置条件",
            "constraints": {"perf": "low"},
            "risk_level": "low",
            "confidence_score": 0.85,
            "example_diff": None,
            "notes": "适用于参数校验缺失场景",
            "created_at": now,
        },
        {
            "strategy_id": "strategy_bounds_check",
            "applicable_pattern_ids": ["pattern_out_of_bounds"],
            "fix_intent": "访问前校验下标与 size；使用 at() 或封装安全访问接口",
            "constraints": {"perf": "low"},
            "risk_level": "low",
            "confidence_score": 0.84,
            "example_diff": None,
            "notes": "vector/数组场景优先核对循环与边界",
            "created_at": now,
        },
        {
            "strategy_id": "strategy_guard_divisor",
            "applicable_pattern_ids": ["pattern_divide_by_zero"],
            "fix_intent": "除法前检查除数为零；对可能为零的路径提前返回或改用安全除法",
            "constraints": {"perf": "low"},
            "risk_level": "low",
            "confidence_score": 0.86,
            "example_diff": None,
            "notes": "整数与浮点除法均需覆盖",
            "created_at": now,
        },
        {
            "strategy_id": "strategy_fix_cast_and_type",
            "applicable_pattern_ids": ["pattern_bad_cast"],
            "fix_intent": "核对继承体系；dynamic_cast 结果判空；必要时用 visitor/variant 替代不安全向下转型",
            "constraints": {"perf": "low"},
            "risk_level": "medium",
            "confidence_score": 0.78,
            "example_diff": None,
            "notes": "失败路径与成功路径分支都要覆盖",
            "created_at": now,
        },
        {
            "strategy_id": "strategy_iterative_or_limit_recursion",
            "applicable_pattern_ids": ["pattern_stack_overflow"],
            "fix_intent": "改为迭代、限制递归深度，或拆分调用栈；检查终止条件",
            "constraints": {"perf": "low"},
            "risk_level": "low",
            "confidence_score": 0.8,
            "example_diff": None,
            "notes": "无限递归与过深递归均适用",
            "created_at": now,
        },
        {
            "strategy_id": "strategy_replace_abort_with_error_path",
            "applicable_pattern_ids": ["pattern_abort_explicit"],
            "fix_intent": "用可恢复错误码/异常替代 abort；或收紧前置条件与日志后再终止",
            "constraints": {"perf": "low"},
            "risk_level": "medium",
            "confidence_score": 0.75,
            "example_diff": None,
            "notes": "区分「应崩溃」的断言与可处理错误",
            "created_at": now,
        },
        {
            "strategy_id": "strategy_sigbus_alignment_and_mapping",
            "applicable_pattern_ids": ["pattern_sigbus_unaligned_or_bus_error"],
            "fix_intent": "检查地址对齐与内存映射合法性；避免未对齐强转与失效映射访问",
            "constraints": {"perf": "low"},
            "risk_level": "medium",
            "confidence_score": 0.77,
            "example_diff": None,
            "notes": "重点排查 reinterpret_cast、packed 结构和 mmap 生命周期",
            "created_at": now,
        },
        {
            "strategy_id": "strategy_sigill_binary_compat_and_dispatch",
            "applicable_pattern_ids": ["pattern_sigill_illegal_instruction"],
            "fix_intent": "确认 CPU 指令集兼容性；为特性指令增加运行时检测与降级路径",
            "constraints": {"perf": "low", "platform": ["macos", "linux", "android"]},
            "risk_level": "medium",
            "confidence_score": 0.76,
            "example_diff": None,
            "notes": "关注编译选项、JIT/内联汇编和第三方二进制兼容性",
            "created_at": now,
        },
        {
            "strategy_id": "strategy_double_free_ownership_model",
            "applicable_pattern_ids": ["pattern_double_free_heap", "pattern_use_after_free", "pattern_dangling_heap"],
            "fix_intent": "统一所有权模型，避免重复释放；释放后立即置空并限制多路径销毁",
            "constraints": {"perf": "low"},
            "risk_level": "medium",
            "confidence_score": 0.83,
            "example_diff": None,
            "notes": "优先用 unique_ptr/shared_ptr 管理资源生命周期",
            "created_at": now,
        },
        {
            "strategy_id": "strategy_null_function_pointer_guard",
            "applicable_pattern_ids": ["pattern_null_function_pointer_call"],
            "fix_intent": "函数指针调用前判空；初始化默认回调或改用 std::function + 安全包装",
            "constraints": {"perf": "low"},
            "risk_level": "low",
            "confidence_score": 0.85,
            "example_diff": None,
            "notes": "同时检查回调注册/反注册时序，避免悬空回调地址",
            "created_at": now,
        },
        {
            "strategy_id": "strategy_synchronize_shared_mutable_state",
            "applicable_pattern_ids": ["pattern_data_race_unprotected_shared_state"],
            "fix_intent": "对共享可变状态使用统一互斥锁、读写锁或原子操作；缩小临界区并避免锁嵌套过深",
            "constraints": {"perf": "medium"},
            "risk_level": "medium",
            "confidence_score": 0.81,
            "example_diff": None,
            "notes": "数据竞争类问题优先用 ThreadSanitizer 复现与定位",
            "created_at": now,
        },
        {
            "strategy_id": "strategy_atomic_release_acquire_pairs",
            "applicable_pattern_ids": ["pattern_atomic_visibility_release_acquire"],
            "fix_intent": "为发布-订阅语义配对 release/acquire 或 seq_cst；避免在跨线程可见性路径上使用 relaxed",
            "constraints": {"perf": "low"},
            "risk_level": "medium",
            "confidence_score": 0.8,
            "example_diff": None,
            "notes": "单生产者单消费者可用 acquire/release，多生产者需更强顺序或锁",
            "created_at": now,
        },
        {
            "strategy_id": "strategy_recursive_mutex_or_refactor",
            "applicable_pattern_ids": ["pattern_nonrecursive_mutex_double_lock"],
            "fix_intent": "改为递归锁、拆分函数避免重入加锁，或明确分层 API 不再嵌套获取同一把锁",
            "constraints": {"perf": "low"},
            "risk_level": "low",
            "confidence_score": 0.82,
            "example_diff": None,
            "notes": "优先通过设计消除重入而非仅换递归锁",
            "created_at": now,
        },
        {
            "strategy_id": "strategy_global_lock_ordering_policy",
            "applicable_pattern_ids": ["pattern_deadlock", "pattern_abba_lock_order_inversion"],
            "fix_intent": "全工程统一锁获取顺序；使用 std::lock 同时取多把锁；关键路径加超时与死锁检测日志",
            "constraints": {"perf": "medium"},
            "risk_level": "medium",
            "confidence_score": 0.83,
            "example_diff": None,
            "notes": "AB-BA 与嵌套锁顺序反转是最常见死锁来源",
            "created_at": now,
        },
        {
            "strategy_id": "strategy_condvar_wait_with_predicate",
            "applicable_pattern_ids": ["pattern_condition_variable_wait_notify_mismatch"],
            "fix_intent": "wait 必须放在带谓词的循环中；notify 前更新受保护状态并持锁通知",
            "constraints": {"perf": "low"},
            "risk_level": "medium",
            "confidence_score": 0.82,
            "example_diff": None,
            "notes": "区分 notify_one 与 notify_all 的语义，避免丢失唤醒",
            "created_at": now,
        },
        {
            "strategy_id": "strategy_shutdown_and_join_handshake",
            "applicable_pattern_ids": ["pattern_threadpool_task_after_shutdown", "pattern_destroy_sync_primitive_in_use"],
            "fix_intent": "关闭流程上先停止接收新任务、排空队列、join 线程，最后再销毁同步原语与共享对象",
            "constraints": {"perf": "medium"},
            "risk_level": "medium",
            "confidence_score": 0.82,
            "example_diff": None,
            "notes": "析构顺序与线程池 shutdown 必须可证明无并发访问",
            "created_at": now,
        },
        {
            "strategy_id": "strategy_bounded_queue_single_writer_or_lock",
            "applicable_pattern_ids": ["pattern_producer_consumer_queue_race"],
            "fix_intent": "多生产者队列使用成熟实现或显式分段锁；无锁队列必须证明内存序与 ABA 安全",
            "constraints": {"perf": "medium"},
            "risk_level": "high",
            "confidence_score": 0.76,
            "example_diff": None,
            "notes": "优先用有界队列与背压，避免无限增长掩盖竞争",
            "created_at": now,
        },
        {
            "strategy_id": "strategy_avoid_blocking_main_thread",
            "applicable_pattern_ids": ["pattern_main_thread_block_deadlock"],
            "fix_intent": "主线程避免持锁等待工作线程同步完成；长任务异步化并显式进度回调",
            "constraints": {"perf": "medium", "platform": ["Android", "iOS", "macos"]},
            "risk_level": "medium",
            "confidence_score": 0.75,
            "example_diff": None,
            "notes": "与 UI watchdog / ANR 症状高度相关",
            "created_at": now,
        },
        {
            "strategy_id": "strategy_lockfree_verified_memory_orders",
            "applicable_pattern_ids": ["pattern_lockfree_memory_order_bug"],
            "fix_intent": "无锁结构使用经证明的算法或退化为带锁实现；为每个原子操作选择可证明的 memory_order",
            "constraints": {"perf": "medium"},
            "risk_level": "high",
            "confidence_score": 0.74,
            "example_diff": None,
            "notes": "谨慎使用 relaxed；跨线程发布数据至少 acquire/release",
            "created_at": now,
        },
        {
            "strategy_id": "strategy_semaphore_resource_invariant",
            "applicable_pattern_ids": ["pattern_semaphore_count_mismatch"],
            "fix_intent": "为信号量/计数器建立资源不变式；post/wait 成对审计；关闭时 drain 或显式重置",
            "constraints": {"perf": "low"},
            "risk_level": "medium",
            "confidence_score": 0.76,
            "example_diff": None,
            "notes": "与条件变量混用时注意等价谓词与虚假唤醒",
            "created_at": now,
        },
    ]
    for s in strategies:
        analyzer.add_fix_strategy(s)


def init_guidance_blocks(analyzer: AIStabilityAnalyzerWithVectorDB) -> None:
    """从 default_guidance_blocks.json 加载并写入指导片段表。"""
    print("正在初始化指导片段表...")
    path = PROJECT_ROOT / "configs" / "default_guidance_blocks.json"
    if not path.exists():
        path = PROJECT_ROOT / "tools" / "configs" / "default_guidance_blocks.json"
    if not path.exists():
        print(f"  跳过：未找到 {path}")
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            blocks = json.load(f)
    except Exception as e:
        print(f"  加载失败: {e}")
        return
    n_ok = 0
    for blk in blocks:
        try:
            block_id = blk.get("block_id")
            if not block_id:
                continue
            analyzer.add_guidance_block(blk)
            n_ok += 1
        except Exception as e:
            print(f"  写入 block {blk.get('block_id')} 失败: {e}")
    print(f"  已写入 {n_ok} 条指导片段（共 {len(blocks)} 条配置）")


def init_fault_modes(analyzer: AIStabilityAnalyzerWithVectorDB) -> None:
    """从 fault_mode_library.json 加载三级故障模式规则并写入 RuleStore。"""
    print("正在初始化三级故障模式库...")
    path = PROJECT_ROOT / "rag" / "seed_data" / "fault_mode_library.json"
    if not path.exists():
        print(f"  跳过：未找到 {path}")
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"  加载失败: {e}")
        return
    fault_modes = data.get("fault_modes", []) if isinstance(data, dict) else data
    n_ok = 0
    for rule in fault_modes:
        try:
            rule_id = rule.get("rule_id")
            if not rule_id:
                continue
            analyzer.add_rule(rule)
            n_ok += 1
        except Exception as e:
            print(f"  写入 rule {rule.get('rule_id')} 失败: {e}")
    print(f"  已写入 {n_ok} 条故障模式规则（共 {len(fault_modes)} 条）")


def main() -> int:
    print("=" * 60)
    print("Stability Analysis Agent 向量数据库初始化（规则+模式+证据+策略+指导片段+故障模式库）")
    print("=" * 60)
    try:
        analyzer = AIStabilityAnalyzerWithVectorDB()
        print("✅ 向量数据库连接成功")
        print("🗑  清空现有向量库与元数据后写入静态种子…")
        analyzer.clear_all()
        init_rules(analyzer)
        init_fault_modes(analyzer)
        init_patterns(analyzer)
        init_evidence(analyzer)
        init_strategies(analyzer)
        init_guidance_blocks(analyzer)
        stats = analyzer.get_database_statistics()
        print("\n初始化完成！数据库统计信息:")
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        return 0
    except Exception as e:
        print(f"❌ 初始化失败: {e}")
        print("请确保已安装向量数据库依赖:")
        print("  pip install -r requirements_vector_db.txt")
        return 1


if __name__ == "__main__":
    sys.exit(main())
