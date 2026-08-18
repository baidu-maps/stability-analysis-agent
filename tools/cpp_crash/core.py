#!/usr/bin/env python3
"""Native crash evidence normalization and deterministic fault-mode matching."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

from tools.crash_parser.address_pattern_analyzer import analyze_crash_address
from tools.cpp_crash.hints import match_crash_hints


SIGNAL_MODES = {
    "SIGSEGV": ("CPP-FM-01", "非法内存访问", ["检查指针生命周期、访问权限和对象边界"]),
    "SIGABRT": ("CPP-FM-08", "主动终止或断言失败", ["检查 abort/assert 触发条件和未捕获 C++ 异常"]),
    "SIGBUS": ("CPP-FM-05", "总线错误或未对齐访问", ["检查 mmap/共享内存、文件映射和数据对齐"]),
    "SIGFPE": ("CPP-FM-06", "算术异常", ["检查除零、溢出和非法浮点运算"]),
    "SIGILL": ("CPP-FM-07", "非法指令", ["核对 ABI/CPU 架构、二进制完整性和函数指针"]),
    "SIGTRAP": ("CPP-FM-09", "陷阱或栈保护中断", ["检查 stack canary、调试断点和硬件断点"]),
    "SIGSYS": ("CPP-FM-10", "非法系统调用或沙箱拦截", ["核对 Seccomp/沙箱策略与系统调用号"]),
}

SI_CODE_MODES = {
    "SEGV_MAPERR": ("CPP-FM-01", "地址未映射", "访问不存在或未映射的内存（空指针、野指针）"),
    "SEGV_ACCERR": ("CPP-FM-03", "映射权限错误", "写只读段、执行数据段或缓冲区溢出改写保护页"),
    "BUS_ADRALN": ("CPP-FM-05", "地址未对齐", "非 x86 架构上的未对齐原子/内存访问"),
    "BUS_ADRERR": ("CPP-FM-05", "mmap 物理地址失效", "映射文件被截断或物理页不存在"),
    "BUS_OBJERR": ("CPP-FM-13", "对象/文件页访问错误", "mmap 文件大小与访问地址不一致"),
    "FPE_INTDIV": ("CPP-FM-06", "整数除零", "除数为零"),
    "FPE_FLTDIV": ("CPP-FM-06", "浮点除零", "浮点除数为零"),
    "ILL_ILLOPC": ("CPP-FM-07", "非法操作码", "CPU 读到无法解析的机器码或错误 ISA"),
    "ILL_ILLPACCFI": ("CPP-FM-07", "指针校验失败", "PAC/CFI 指针校验失败"),
    "TRAP_BRKPT": ("CPP-FM-09", "软件断点或栈保护", "stack canary 失败后主动 brk，或调试断点"),
}

RUNTIME_FRAME_RE = re.compile(
    r"libc\.so|libc\+\+|ld-musl|libsec_shared|libutils|__cfi_check|stub\.an|"
    r"Not mapped|Not\(mapped\)|Unknown|libark_jsruntime|libace_napi|"
    r"ArkNativeReference|ArkNativeFunction|panda::JSNApi|libsystem_|libobjc|libdispatch|"
    r"libdyld|dyld|ntdll\.dll|kernel32\.dll",
    re.I,
)
APP_FRAME_RE = re.compile(r"/data/|/private/var/containers|/Users/|\\Users\\|libapp|/app/")


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _frame_text(frame: Mapping[str, Any]) -> str:
    return " ".join(
        str(frame.get(key) or "")
        for key in ("raw", "function", "symbol", "name", "module", "library", "so", "file", "path")
    )


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


def _fault_thread_name(data: Mapping[str, Any]) -> str:
    threads = data.get("threads") or data.get("thread_stacks") or []
    if isinstance(threads, Mapping):
        threads = list(threads.values())
    for thread in threads:
        if isinstance(thread, Mapping) and (thread.get("crashed") or thread.get("is_fault") or thread.get("fault")):
            return str(thread.get("name") or thread.get("thread_name") or "")
    if threads and isinstance(threads[0], Mapping):
        return str(threads[0].get("name") or threads[0].get("thread_name") or "")
    return ""


def _signal_name(value: Any) -> str:
    match = re.search(r"SIG(?:SEGV|ABRT|BUS|FPE|ILL|TRAP|PIPE|TERM|KILL|INT|QUIT|ALRM|HUP|SYS|STKFLT)", str(value or "").upper())
    return match.group(0) if match else str(value or "").upper()


def classify_stack_layers(frames: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Split crash / first non-runtime / first application frames (Huawei stack contract)."""
    crash = dict(frames[0]) if frames else {}
    non_runtime = {}
    application = {}
    for frame in frames:
        text = _frame_text(frame)
        if not non_runtime and text.strip() and not RUNTIME_FRAME_RE.search(text):
            non_runtime = dict(frame)
        path = str(_first(frame, "module", "library", "so", "path", "file") or "")
        if not application and (APP_FRAME_RE.search(path) or APP_FRAME_RE.search(text)):
            application = dict(frame)
        if non_runtime and application:
            break
    return {
        "crash_frame": crash,
        "first_non_runtime_caller": non_runtime,
        "first_application_frame": application,
        "runtime_only": bool(frames) and not non_runtime,
    }


def _signal_taxonomy(signal: str, code: str) -> Dict[str, Any]:
    ident, name, _guidance = SIGNAL_MODES.get(signal, ("", signal or "Unknown", []))
    detail = SI_CODE_MODES.get(code.upper())
    return {
        "level_1": "NativeCrash",
        "level_2": name or signal or "Unknown",
        "level_3": detail[1] if detail else (code or "未给出 si_code"),
        "signal": signal,
        "signal_code": code,
        "mode_id": detail[0] if detail else ident,
    }


def extract_cpp_evidence(data: Mapping[str, Any]) -> Dict[str, Any]:
    info = data.get("crash_info") if isinstance(data.get("crash_info"), Mapping) else data
    registers = data.get("registers") if isinstance(data.get("registers"), Mapping) else {}
    raw = str(data.get("raw_content") or data.get("raw_log") or data.get("content") or "")
    signal = _signal_name(_first(info, "signal", "reason", "crash_reason"))
    code = str(_first(info, "signal_code", "si_code", "code") or "")
    fault_address = _first(info, "fault_addr", "crash_address", "fault_address")
    last_fatal = str(_first(info, "last_fatal_message", "LastFatalMessage", "abort_message") or "")
    if raw:
        raw_signal = re.search(r"SIG(?:SEGV|ABRT|BUS|FPE|ILL|TRAP|PIPE|TERM|KILL|INT|QUIT|ALRM|HUP|SYS|STKFLT)", raw.upper())
        if raw_signal:
            signal = _signal_name(raw_signal.group(0))
        if not fault_address:
            match = re.search(r"(?i)(?:fault addr|fault address|crash address|@)\s*[:=]?\s*(0x[0-9a-f]+)", raw)
            fault_address = match.group(1) if match else None
        if not code:
            match = re.search(r"(?i)(?:SEGV_[A-Z]+|BUS_[A-Z]+|FPE_[A-Z]+|ILL_[A-Z]+|TRAP_[A-Z]+|SI_[A-Z]+)", raw)
            code = match.group(0) if match else ""
        if not last_fatal:
            match = re.search(r"(?im)^\s*LastFatalMessage\s*:\s*(.+?)\s*$", raw)
            last_fatal = match.group(1).strip() if match else ""
    frames = _frames(data)
    modules = [str(_first(frame, "module", "library", "so") or "") for frame in frames[:20]]
    native_frames = [
        frame for frame in frames
        if str(_first(frame, "language", "layer") or "").lower() in {"cpp", "c++", "native", "c"}
        or str(_first(frame, "module", "library") or "").lower().endswith((".so", ".dylib", ".dll"))
        or bool(frame)
    ]
    if not native_frames:
        native_frames = list(frames)
    layers = classify_stack_layers(native_frames)
    return {
        "signal": signal,
        "signal_code": code,
        "fault_address": fault_address,
        "registers": dict(registers),
        "native_frames": native_frames[:30],
        "modules": [module for module in modules if module],
        "raw_sections": data.get("raw_log_sections") or data.get("sections") or {},
        "last_fatal_message": last_fatal,
        "thread_name": _fault_thread_name(data),
        "stack_layers": layers,
        "raw_content": raw,
    }


def match_cpp_fault_modes(evidence: Mapping[str, Any]) -> List[Dict[str, Any]]:
    signal = str(evidence.get("signal") or "")
    address = evidence.get("fault_address")
    code = str(evidence.get("signal_code") or "").upper()
    matches: List[Dict[str, Any]] = []
    if signal in SIGNAL_MODES:
        ident, name, guidance = SIGNAL_MODES[signal]
        matches.append({"id": ident, "name": name, "level": "signal", "owner": "Native", "confidence": 0.65, "evidence": [f"signal={signal}"], "guidance": guidance})
    if code in SI_CODE_MODES:
        ident, name, detail = SI_CODE_MODES[code]
        matches.append({"id": ident, "name": name, "level": "si_code", "owner": "Native", "confidence": 0.72, "evidence": [f"si_code={code}", detail], "guidance": [detail]})
    address_analysis: Mapping[str, Any] = {}
    if address:
        try:
            address_analysis = analyze_crash_address(str(address))
        except Exception:
            address_analysis = {}
    pattern = str(address_analysis.get("pattern") or "")
    if pattern in {"null_pointer", "null_pointer_offset"}:
        matches.append({"id": "CPP-FM-01", "name": "空指针解引用", "level": "address", "owner": "Native", "confidence": float(address_analysis.get("confidence") or 0.9), "evidence": [pattern, str(address_analysis.get("hint") or "")], "guidance": ["修复空指针来源和生命周期；保留必要的状态校验。"]})
    elif pattern in {"poison_value", "debug_poison", "use_after_free_fill", "low_address", "high_address"}:
        matches.append({"id": "CPP-FM-02", "name": "野指针或 Use-after-free", "level": "address", "owner": "Native", "confidence": float(address_analysis.get("confidence") or 0.7), "evidence": [pattern, str(address_analysis.get("hint") or "")], "guidance": ["核对释放点、所有权和异步回调；使用 ASan/HWASan 复现确认。"]})
    frames_text = " ".join(str(frame) for frame in evidence.get("native_frames") or []).lower()
    if "assert" in frames_text or "__assert" in frames_text:
        matches.append({"id": "CPP-FM-08", "name": "assert 失败", "level": "stack", "owner": "Native", "confidence": 0.88, "evidence": ["assert frame in native stack"], "guidance": ["检查断言前置条件和异常输入，避免仅删除断言。"]})
    if any(token in frames_text for token in ("napi_", "napi::", "arkruntime")):
        matches.append({"id": "CPP-FM-11", "name": "N-API 或运行时边界生命周期问题", "level": "stack", "owner": "Native/Runtime", "confidence": 0.55, "evidence": ["N-API/runtime frame in native stack"], "guidance": ["核对 napi_env、引用和线程归属，确认异步回调未跨线程使用错误环境。"]})
    raw = str(evidence.get("raw_content") or "")
    if re.search(r"gwp[-_ ]?asan", raw + frames_text, re.I):
        matches.append({"id": "CPP-FM-12", "name": "GWP-ASan 检出的堆损坏", "level": "detector", "owner": "Native", "confidence": 0.95, "evidence": ["GWP-ASan marker"], "guidance": ["同时查看 violation / free / alloc 三栈，定位首次释放与非法访问。"]})
    hint_modes = {
        "uncaught_exception": ("CPP-FM-08", "C++ 未捕获异常", 0.86, ["在业务代码中定位 throw 点并补捕获或入参校验。"]),
        "js_oom": ("CPP-FM-14", "JS 堆 OOM 触发 Native 崩溃", 0.7, ["结合 JS Heap 快照确认泄漏或超大分配，而不是只看 Native 栈顶。"]),
        "sqlite_bus": ("CPP-FM-13", "sqlite/mmap 文件页异常", 0.82, ["避免用普通文件接口改写正在 mmap 的数据库文件。"]),
        "gc_delayed_corruption": ("CPP-FM-02", "GC 阶段暴露的延迟内存破坏", 0.74, ["不要归因于 GC；排查跨线程 env、UAF 和越界写。"]),
    }
    for hint in evidence.get("hints") or []:
        mapped = hint_modes.get(str(hint.get("id") or ""))
        if not mapped:
            continue
        ident, name, confidence, guidance = mapped
        matches.append({"id": ident, "name": name, "level": "hint", "owner": "Native", "confidence": confidence, "evidence": [hint.get("hint")], "guidance": guidance})
    dedup: Dict[str, Dict[str, Any]] = {}
    for item in matches:
        old = dedup.get(item["id"])
        if old is None or item["confidence"] > old["confidence"]:
            dedup[item["id"]] = item
    return sorted(dedup.values(), key=lambda item: (-item["confidence"], item["id"]))


def _follow_up(evidence: Mapping[str, Any], modes: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    checks: List[Dict[str, Any]] = []
    if not evidence.get("fault_address"):
        checks.append({"id": "extract_fault_address", "required": True, "reason": "缺少 fault address，无法完成地址语义分析"})
    if not evidence.get("native_frames"):
        checks.append({"id": "symbolicate_native_stack", "required": True, "reason": "缺少 Native 调用栈或符号化结果"})
    if any(item["id"] in {"CPP-FM-02", "CPP-FM-11", "CPP-FM-12"} for item in modes):
        checks.append({"id": "asan_reproduction", "required": True, "reason": "疑似生命周期/Use-after-free 或 N-API 跨线程问题，静态现场不足以确认首次破坏位置"})
    if evidence.get("signal") in {"SIGILL", "SIGBUS"}:
        checks.append({"id": "verify_build_id_and_arch", "required": True, "reason": "需要核对 BuildID、ABI 和二进制架构"})
    if evidence.get("signal") in {"SIGABRT", "SIGFPE"}:
        checks.append({"id": "inspect_source_condition", "required": True, "reason": "信号可能由显式条件、断言或算术路径触发，需要源码和参数证据"})
    layers = evidence.get("stack_layers") if isinstance(evidence.get("stack_layers"), Mapping) else {}
    if layers.get("runtime_only"):
        checks.append({"id": "find_first_scene", "required": True, "reason": "当前栈只有运行时/分配器帧，可能是延迟崩溃，需要第一现场"})
    checks.append({"id": "disassemble_pc", "required": False, "reason": "若源码行与寄存器无法解释 PC 指令，再执行反汇编"})
    return checks


def _repair_guidance(modes: Sequence[Mapping[str, Any]]) -> Dict[str, List[str]]:
    direct: List[str] = []
    defensive: List[str] = []
    verification: List[str] = []
    for mode in modes:
        ident = mode.get("id")
        direct.extend(mode.get("guidance") or [])
        if ident in {"CPP-FM-01", "CPP-FM-02", "CPP-FM-03"}:
            defensive.append("增加对象状态、边界和所有权检查，避免仅在崩溃点添加兜底。")
        if ident in {"CPP-FM-02", "CPP-FM-11", "CPP-FM-12"}:
            verification.append("使用 ASan/HWASan 和多线程/异步销毁场景回归验证。")
        if ident == "CPP-FM-08":
            verification.append("补充触发断言前置条件的单元测试和错误输入测试。")
        if ident == "CPP-FM-14":
            verification.append("用 JS Heap 快照对比确认泄漏对象和分配峰值。")
    if not direct:
        direct.append("补充完整符号化栈、寄存器和源码上下文后再确定直接修复。")
    if not verification:
        verification.append("使用原始 Crash Case 和新增回归测试验证修复前后行为。")
    return {"direct_fix": list(dict.fromkeys(direct)), "defensive_fix": list(dict.fromkeys(defensive)), "verification": list(dict.fromkeys(verification))}


def _evidence_grade(evidence: Mapping[str, Any], modes: Sequence[Mapping[str, Any]]) -> str:
    if any(item.get("level") == "detector" or item.get("id") == "CPP-FM-12" for item in modes):
        return "detector"
    if evidence.get("registers") and evidence.get("fault_address"):
        return "register"
    if any(item.get("level") in {"address", "si_code"} for item in modes):
        return "address"
    if evidence.get("hints") or any(item.get("level") == "hint" for item in modes):
        return "pattern"
    return "insufficient"


def diagnose_cpp_crash(data: Mapping[str, Any]) -> Dict[str, Any]:
    payload = data.get("parse_result") if isinstance(data.get("parse_result"), Mapping) else data
    evidence = extract_cpp_evidence(payload)
    stack_text = " ".join(_frame_text(frame) for frame in evidence.get("native_frames") or [])
    evidence["hints"] = match_crash_hints(
        signal=str(evidence.get("signal") or ""),
        signal_code=str(evidence.get("signal_code") or ""),
        fault_address=evidence.get("fault_address"),
        stack_text=stack_text,
        thread_name=str(evidence.get("thread_name") or ""),
        last_fatal_message=str(evidence.get("last_fatal_message") or ""),
        raw=str(evidence.get("raw_content") or ""),
    )
    modes = match_cpp_fault_modes(evidence)
    missing = []
    if not evidence["signal"]:
        missing.append("signal")
    if not evidence["registers"]:
        missing.append("registers")
    if not evidence["raw_sections"].get("memory_near") if isinstance(evidence["raw_sections"], Mapping) else True:
        missing.append("memory near registers")
    confidence = max((float(item["confidence"]) for item in modes), default=0.2)
    status = "confirmed" if confidence >= 0.85 else ("probable" if modes else "preliminary")
    return {
        "status": "success",
        "diagnosis_status": status,
        "evidence": evidence,
        "address_analysis": analyze_crash_address(str(evidence["fault_address"])) if evidence.get("fault_address") else {},
        "signal_taxonomy": _signal_taxonomy(str(evidence.get("signal") or ""), str(evidence.get("signal_code") or "")),
        "stack_layers": evidence.get("stack_layers") or {},
        "hints": evidence.get("hints") or [],
        "evidence_grade": _evidence_grade(evidence, modes),
        "fault_modes": modes,
        "confidence": round(confidence, 3),
        "missing_evidence": missing,
        "follow_up_checks": _follow_up(evidence, modes),
        "repair_guidance": _repair_guidance(modes),
    }
