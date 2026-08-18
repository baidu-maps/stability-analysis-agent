#!/usr/bin/env python3
"""Structured JS/ArkTS crash diagnosis built on the existing parser output."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence


PATTERNS = [
    ("TypeError", re.compile(r"cannot read propert(?:y|ies) of (?:null|undefined)|cannot set propert(?:y|ies) of (?:null|undefined)|cannot load property of null or undefined|undefined is not an object|null is not an object|cannot convert (?:undefined or null|.*null) to object", re.I), "空值访问", "对 null/undefined 访问或设置属性", "JSC-FM-01"),
    ("TypeError", re.compile(r"is not a function|not callable|is not callable", re.I), "调用非函数值", "目标值不是可调用函数", "JSC-FM-02"),
    ("TypeError", re.compile(r"can not get prototype on non ecma object|receiver is not a jsobject", re.I), "N-API 句柄越界", "napi_value 超出 handle scope 或传入非 JS 对象", "JSC-FM-14"),
    ("TypeError", re.compile(r"circular structure|stack contains value", re.I), "循环引用", "JSON.stringify 或深拷贝遇到环", "JSC-FM-11"),
    ("ReferenceError", re.compile(r"is not defined|cannot find name", re.I), "未定义标识符", "变量或模块符号未定义", "JSC-FM-03"),
    ("ReferenceError", re.compile(r"is not initialized|missing @Provide|Fail to resolve @Consume|duplicate @Provide|super\(\) forbidden|must call super", re.I), "初始化或装饰器错误", "变量未初始化，或 @Provide/@Consume/super() 使用错误", "JSC-FM-09"),
    ("SyntaxError", re.compile(r"unexpected token|invalid or unexpected token|parse error|Unexpected Text in JSON|Invalid Token", re.I), "语法或编译错误", "源码、JSON 或生成代码无法解析", "JSC-FM-04"),
    ("RangeError", re.compile(r"maximum call stack|invalid array length|out of range|stack overflow", re.I), "范围或递归深度错误", "递归深度、数组长度或参数超出允许范围", "JSC-FM-05"),
    ("URIError", re.compile(r"malformed uri|uri malformed|invalid uri|decodeuri|invalid character", re.I), "URI 编解码错误", "URI 输入格式不合法", "JSC-FM-06"),
    ("OutOfMemoryError", re.compile(r"out of memory|heap|allocation|AllocateHugeObject|AllocateYoungOrHugeObject", re.I), "JavaScript 内存耗尽", "JS 堆或运行时分配失败", "JSC-FM-07"),
    ("Error", re.compile(r"ArrayBuffer is null or detached", re.I), "分离后的 ArrayBuffer", "跨线程转移或 Native 释放后继续使用 buffer", "JSC-FM-10"),
    ("Error", re.compile(r"WebviewController must be associated|17100001", re.I), "Web 控制器未关联", "WebviewController 尚未绑定 Web 组件", "JSC-FM-12"),
    ("TerminationError", re.compile(r"Terminate execution", re.I), "虚拟机被强制终止", "通过 HybridStack 定位谁触发了终止", "JSC-FM-13"),
    ("BusinessError", re.compile(r"permission|parameter|invalid|not found|timeout|resource|Unexpected Text in JSON", re.I), "系统接口或业务错误", "系统 API 返回 BusinessError 或业务主动透传", "JSC-FM-08"),
]

FRAMEWORK_FRAME_RE = re.compile(
    r"stateMgmt\.js|jsenum\.js|arkui|ace_engine|libark_jsruntime|jsruntime|"
    r"napi_call_function|native_engine|ohos\.router|@ohos\.",
    re.I,
)


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def extract_js_error(data: Mapping[str, Any]) -> Dict[str, Any]:
    info = data.get("crash_info") if isinstance(data.get("crash_info"), Mapping) else data
    raw = data.get("raw_content") or data.get("raw_log") or data.get("content") or ""
    reason = _first(info, "reason", "crash_reason", "error_name", "errorName")
    name = _first(info, "error_name", "errorName", "name") or reason
    message = _first(info, "error_message", "errorMessage", "message")
    code = _first(info, "error_code", "errorCode", "code")
    if raw:
        text = str(raw)

        def find(label: str) -> Optional[str]:
            match = re.search(rf"(?im)^\s*{label}\s*:\s*(.+?)\s*$", text)
            return match.group(1).strip() if match else None

        reason = reason or find("Reason")
        name = name or find("Error name") or find("Name")
        message = message or find("Error message") or find("Message")
        code = code or find("Error code") or find("Code")
    return {"reason": str(reason or ""), "name": str(name or ""), "message": str(message or ""), "code": code}


def match_js_fault_mode(error: Mapping[str, Any]) -> Dict[str, Any]:
    name = str(error.get("name") or error.get("reason") or "").strip()
    message = str(error.get("message") or "").strip()
    for expected, pattern, level2, level3, mode_id in PATTERNS:
        if expected.lower() in name.lower() and pattern.search(message):
            return {"id": mode_id, "level_1": "JSError", "level_2": expected, "level_3": level3, "owner": "ArkTS", "confidence": 0.92, "matched_message": message, "matched_pattern": level2}
    for expected, pattern, level2, level3, mode_id in PATTERNS:
        if message and pattern.search(message) and expected.lower() in {"error", "typeerror", "referenceerror"}:
            return {"id": mode_id, "level_1": "JSError", "level_2": expected, "level_3": level3, "owner": "ArkTS", "confidence": 0.8, "matched_message": message, "matched_pattern": level2}
    seen = set()
    for expected, _, level2, _, mode_id in PATTERNS:
        if expected in seen or expected == "Error":
            continue
        seen.add(expected)
        if expected.lower() in name.lower():
            return {"id": mode_id, "level_1": "JSError", "level_2": expected, "level_3": "未收录子类，需结合 Error message 与栈顶应用帧继续判断", "owner": "ArkTS", "confidence": 0.55, "matched_message": message, "matched_pattern": level2}
    return {"id": "JSC-FM-Unknown", "level_1": name or "Unknown", "level_2": "未知 JS 错误", "level_3": "缺少可匹配的 Error name/Reason", "owner": "Unknown", "confidence": 0.2, "matched_message": message}


def _frames(data: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    result: List[Mapping[str, Any]] = []
    threads = data.get("threads") or data.get("thread_stacks") or []
    if isinstance(threads, Mapping):
        threads = list(threads.values())
    for thread in threads:
        if isinstance(thread, Mapping):
            values = thread.get("frames") or thread.get("stack_frames") or []
            result.extend(item for item in values if isinstance(item, Mapping))
    return result


def _frame_text(frame: Mapping[str, Any]) -> str:
    return " ".join(str(frame.get(key) or "") for key in ("function", "symbol", "name", "file", "module", "library", "raw"))


def first_application_frame(frames: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    for frame in frames:
        text = _frame_text(frame)
        if not text.strip():
            continue
        if FRAMEWORK_FRAME_RE.search(text):
            continue
        return dict(frame)
    return dict(frames[0]) if frames else {}


def looks_like_js_crash(data: Mapping[str, Any]) -> bool:
    error = extract_js_error(data)
    text = " ".join(str(error.get(key) or "") for key in ("reason", "name", "message")).lower()
    if any(token in text for token in ("typeerror", "referenceerror", "syntaxerror", "rangeerror", "urierror", "outofmemory", "businesserror", "terminationerror", "aggregateerror")):
        return True
    frames = _frames(data)
    return any(str(frame.get("layer") or frame.get("language") or "").lower() in {"arkts", "js", "javascript"} for frame in frames)


def diagnose_js_crash(data: Mapping[str, Any]) -> Dict[str, Any]:
    payload = data.get("parse_result") if isinstance(data.get("parse_result"), Mapping) else data
    error = extract_js_error(payload)
    frames = _frames(payload)
    js_frames = [frame for frame in frames if str(frame.get("layer") or frame.get("language") or "").lower() in {"arkts", "js", "javascript"} or "ark" in str(frame.get("function") or "").lower()]
    native_frames = [frame for frame in frames if frame not in js_frames and str(frame.get("module") or frame.get("library") or "").lower().endswith((".so", ".dylib", ".dll"))]
    bridge_frames = [frame for frame in native_frames if any(token in str(frame).lower() for token in ("napi", "ark_jsruntime", "arkruntime", "jsruntime", "libuv"))]
    responsibility = first_application_frame(js_frames or frames)
    fault_mode = match_js_fault_mode(error)
    missing: List[str] = []
    if not error["name"] and not error["reason"]:
        missing.append("Reason/Error name")
    if not error["message"]:
        missing.append("Error message")
    if not js_frames:
        missing.append("JS/ArkTS 应用栈或 source map")
    status = "confirmed" if fault_mode["id"] != "JSC-FM-Unknown" and js_frames else ("probable" if fault_mode["id"] != "JSC-FM-Unknown" else "preliminary")
    return {
        "status": "success",
        "diagnosis_status": status,
        "error": error,
        "fault_mode": fault_mode,
        "stack": {
            "js_frames": js_frames[:20],
            "native_frames": native_frames[:20],
            "hybrid_frames": bridge_frames[:20],
            "has_hybrid_stack": bool(bridge_frames),
            "responsibility_frame": responsibility,
        },
        "missing_evidence": missing,
        "confidence": round(float(fault_mode["confidence"]) * (1.0 if js_frames else 0.75), 3),
        "repair_guidance": _guidance(fault_mode),
    }


def _guidance(mode: Mapping[str, Any]) -> List[str]:
    guidance = {
        "JSC-FM-01": ["在访问属性或调用方法前校验 null/undefined。", "优先修复产生空值的上游分支，而不是只添加空值兜底。"],
        "JSC-FM-02": ["确认函数引用初始化和类型，避免把对象、Promise 或 undefined 当函数调用。"],
        "JSC-FM-03": ["检查变量声明、模块导入和构建产物版本是否一致。"],
        "JSC-FM-04": ["根据文件和行号检查语法、装饰器和编译生成代码。", "JSON.parse 前确认内容是合法 JSON。"],
        "JSC-FM-05": ["检查递归终止条件、数组长度和边界参数。"],
        "JSC-FM-06": ["校验 URI 输入并在解码前处理非法字符串。"],
        "JSC-FM-07": ["结合 JS Heap 分析确认 retained size 增长和长期引用；稳定栈看代码路径，不稳定栈做快照对比。"],
        "JSC-FM-08": ["核对 Error code、权限、参数和系统 API 生命周期；不要停在框架守卫，追溯应用侧调用。"],
        "JSC-FM-09": ["检查 @Provide/@Consume 名称是否匹配，以及构造函数中 super() 是否只调用一次。"],
        "JSC-FM-10": ["确认 ArrayBuffer 未 detached，避免跨线程转移后继续使用。"],
        "JSC-FM-11": ["序列化前用 WeakSet 过滤循环引用，或重构避免环。"],
        "JSC-FM-12": ["在 onControllerAttached 之后再操作 WebviewController。"],
        "JSC-FM-13": ["通过 HybridStack 定位谁触发了虚拟机终止。"],
        "JSC-FM-14": ["检查 N-API handle scope 的打开/关闭，以及 napi_value 生命周期。"],
    }
    return guidance.get(str(mode.get("id")), ["补充完整 Error message、JS 栈和对应版本源码后再确认根因。"])
