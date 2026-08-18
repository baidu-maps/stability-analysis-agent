#!/usr/bin/env python3
"""Historical Native crash signature hints, adapted from Huawei cppcrash-analysis.

Hits are clues for later diagnosis, not a substitute for address/register evidence.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional


def _as_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        return int(text, 16) if text.lower().startswith("0x") else int(text)
    except ValueError:
        return None


def _address_contains_ascii(address: Any) -> bool:
    value = _as_int(address)
    if value is None or value.bit_length() > 64:
        return False
    packed = value.to_bytes(8, byteorder="little", signed=False)
    return bool(re.search(rb"[\x20-\x7e]{4,}", packed) or re.search(rb"[\x20-\x7e]{4,}", packed[::-1]))


def _has_js_binding(stack: str) -> bool:
    return bool(re.search(
        r"libark_jsruntime|libace_napi|napi_|ArkNativeReference|InternalJSMemberFunctionCallback|"
        r"ThreadSafeCallback|NativeAsyncWork|TaskPool|jsruntime|Napi",
        stack,
        re.I,
    ))


def match_crash_hints(
    *,
    signal: str = "",
    signal_code: str = "",
    fault_address: Any = None,
    stack_text: str = "",
    thread_name: str = "",
    last_fatal_message: str = "",
    raw: str = "",
) -> List[Dict[str, Any]]:
    """Return structured historical-signature hits."""
    stack = stack_text or ""
    fatal = last_fatal_message or ""
    blob = "\n".join([stack, fatal, raw or ""])
    address = _as_int(fault_address)
    hits: List[Dict[str, Any]] = []

    exception = re.search(r"terminating due to uncaught exception of type (\S+)", fatal)
    if exception:
        hits.append({
            "id": "uncaught_exception",
            "hint": f"C++ 未捕获异常导致 abort：类型 {exception.group(1).rstrip(':')}，应定位 throw 点而不是 abort 帧",
            "confidence": 0.88,
        })
    if re.search(r"libark_jsruntime.*(?:HandleUncatchableError|AllocateAlignedRegion|ThrowOutOfMemoryError)|"
                 r"\[gc].*(?:Out of Memory|OOM fatal|SharedHeap OOM)", blob, re.I):
        hits.append({
            "id": "js_oom",
            "hint": "JS 堆 OOM 特征：栈或 LastFatalMessage 出现 GC OOM，应结合堆趋势判断泄漏/峰值/超大分配",
            "confidence": 0.78,
        })
    if re.search(r"ecma_vm cannot run in multi-thread", fatal, re.I):
        hits.append({
            "id": "vm_multi_thread",
            "hint": "跨线程使用 JS 对象：当前 env/JS 对象有线程归属，禁止在非所属线程直接访问",
            "confidence": 0.92,
        })
    if _address_contains_ascii(fault_address) and _has_js_binding(stack):
        hits.append({
            "id": "ascii_js_corruption",
            "hint": "故障地址含连续可打印 ASCII，且调用链有 N-API/JS 绑定，优先怀疑对象被字符数据覆盖后的延迟崩溃",
            "confidence": 0.7,
        })
    if address is not None and address <= 0x1000 and _has_js_binding(stack):
        hits.append({
            "id": "small_offset_js",
            "hint": "故障地址为 NULL 小偏移且存在 JS/N-API 路径，优先核对跨线程 env 与对象生命周期",
            "confidence": 0.68,
        })
    if address is not None and re.search(r"napi_wrap|ObjectRef::DefineProperty|ObjectOperator::AddProperty", stack) and \
            re.search(r"ThreadSafeCallback|NativeSafeAsyncWork|NativeAsyncWork|TaskPool", stack):
        hits.append({
            "id": "napi_async_object",
            "hint": "异步回调在 N-API 对象操作中崩溃，可能是跨线程 env 导致的延迟破坏",
            "confidence": 0.66,
        })
    runtime_only = bool(stack) and all(
        re.search(r"libark_jsruntime|libc\.so|libc\+\+|ld-musl|libffrt|Not mapped|Unknown|libsystem", line, re.I)
        for line in stack.splitlines() if line.strip()
    )
    if re.search(r"OS_GC_Thread|ConcurrentMarker|ProcessMarkStack|EvacuateObject|FullGCRunner", blob) and \
            signal in {"SIGSEGV", "SIGBUS"} and runtime_only:
        hits.append({
            "id": "gc_delayed_corruption",
            "hint": "GC/Runtime 帧上的非法访问通常是更早内存破坏的延迟爆炸，不应默认归因于 GC",
            "confidence": 0.74,
        })
    if re.search(r"napi_delete_reference|napi_get_reference_value|napi_create_(object|array|reference)|ArkNativeReference", stack):
        hits.append({
            "id": "napi_wild_pointer",
            "hint": "N-API 引用/创建接口出现在崩溃栈，需联合检查入参、napi_ref/env 生命周期和线程归属",
            "confidence": 0.6,
        })
    if re.search(r"napi_|ArkNativeReference", stack) and re.search(r"OS_(FFRT|IPC)|Worker|TaskPool", thread_name or blob):
        hits.append({
            "id": "napi_in_worker",
            "hint": "工作线程调用链包含 N-API，确认接口是否允许当前线程调用",
            "confidence": 0.62,
        })
    if "libuv" in stack.lower() and re.search(r"uv_ffrt_work|uv_queue_done|uv_queue_work|uv_async_send", stack):
        hits.append({
            "id": "uv_async_task",
            "hint": "libuv 异步任务特征：常见于 uv_work_t/napi_async_work/loop 生命周期管理不当",
            "confidence": 0.64,
        })
    if re.search(r"uv_run|uv__run_closing_handles|uv__finish_close|uv_close", stack):
        hits.append({
            "id": "uv_close_misuse",
            "hint": "libuv 句柄关闭特征：uv_close 是异步的，close_cb 完成前释放 handle 会导致事件循环 UAF",
            "confidence": 0.7,
        })
    if re.search(r"errno is (9|22)\b", fatal):
        hits.append({
            "id": "fd_double_close",
            "hint": "LastFatalMessage 中 errno=9(EBADF)/22(EINVAL) 常见于事件循环 fd 被重复关闭",
            "confidence": 0.72,
        })
    if "XComponentPattern::OnSurfaceDestroyed" in stack or ("OH_NativeXComponent" in stack and "libace_ndk" in stack):
        hits.append({
            "id": "xcomponent_lifecycle",
            "hint": "XComponent 生命周期特征：疑似在 OnSurfaceDestroyed 前析构回调对象，或销毁后继续调用接口",
            "confidence": 0.7,
        })
    if "SIGBUS" in (signal or "") and signal_code == "BUS_OBJERR" and re.search(r"sqlite", stack, re.I):
        hits.append({
            "id": "sqlite_bus",
            "hint": "SIGBUS(BUS_OBJERR)+sqlite：常见于非数据库接口操作 db 文件导致页映射失效",
            "confidence": 0.82,
        })
    if re.search(r"gwp[-_ ]?asan|GWP-ASan", blob, re.I):
        hits.append({
            "id": "gwp_asan",
            "hint": "GWP-ASan 检测报告优先于普通栈顶归因，应同时查看 violation/free/alloc 三栈",
            "confidence": 0.95,
        })
    if re.search(r"libmemtracker|mem_abort", stack, re.I):
        hits.append({
            "id": "memtracker",
            "hint": "崩溃由内存检测工具触发；若监控值为全 e 填充，需排除误报",
            "confidence": 0.55,
        })
    return hits
