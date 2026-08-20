#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""崩溃信息与元信息提取。"""

from __future__ import annotations

import logging
import re
from typing import Optional

from tools.crash_parser.abort_message import (
    extract_abort_message,
    is_heap_allocator_abort,
    thread_type_from_name,
)
from tools.crash_parser.android import (
    _android_heuristic_anr_stack,
    _android_mixed_native_java_jni_sample,
    _android_native_only_pc_stack_sample,
)
from tools.crash_parser.format_detect import (
    _detect_apple_ios_freeze_report,
    _detect_apple_ios_truncated_crash,
    _detect_harmony_native_stack,
    _detect_ios_mach_tool_export,
    _detect_ios_pre_parsed_symbolized_crash,
    detect_os_type,
    _IOS_HW_MODEL_RE,
    _IOS_OS_VERSION_RE,
)
from tools._stack_symbol_utils import looks_like_cpp_qualified_stack
from tools.crash_parser.types import CrashInfo, MetaInfo

logger = logging.getLogger(__name__)

def extract_crash_info(content: str) -> CrashInfo:
    """提取崩溃信息"""
    header_scope = "\n".join(content.splitlines()[:120])
    content_lower = header_scope.lower()

    thread_type = "main"
    crash_reason = "unknown"
    signal = None
    exception_type = None
    category = None
    primary_language = None
    crash_address = None
    abort_message = extract_abort_message(content)

    dbg_name = re.search(
        r"pid:\s*\d+,\s*tid:\s*\d+,\s*name:\s*([^\s>]+)\s*>>>",
        content,
        re.IGNORECASE,
    )
    if dbg_name:
        thread_type = thread_type_from_name(dbg_name.group(1))

    reason_line_match = re.search(r'^Reason:\s*([^\n\r]+)$', content, re.IGNORECASE | re.MULTILINE)
    reason_text = reason_line_match.group(1).strip() if reason_line_match else ""

    signal_reason_map = {
        "SIGSEGV": "segmentation fault",
        "SIGABRT": "abort",
        "SIGILL": "illegal instruction",
        "SIGBUS": "bus error",
        "SIGFPE": "divide by zero",
        "SIGTRAP": "trap",
        "SIGALRM": "timeout",
    }

    # 已符号化精简 iOS 导出：``* SIGSEGV: 0x... UUID + offset``
    pre_parsed_sig = re.search(
        r"^\*\s*(SIG[A-Z0-9_]+):\s*(0x[0-9a-fA-F]+)",
        header_scope,
        re.MULTILINE | re.IGNORECASE,
    )
    if pre_parsed_sig:
        sig_name = pre_parsed_sig.group(1).upper()
        signal = sig_name
        crash_reason = signal_reason_map.get(sig_name, crash_reason)
        category = category or "native_crash"
        if crash_address is None:
            crash_address = pre_parsed_sig.group(2)

    pre_parsed_crash_fn = re.search(
        r"^\*\s*(?!SIG)(\S+)\s+(.+)$",
        header_scope,
        re.MULTILINE | re.IGNORECASE,
    )
    if pre_parsed_crash_fn and exception_type is None:
        exception_type = pre_parsed_crash_fn.group(2).strip()

    # iOS/macOS: Exception Type: EXC_CRASH (SIGABRT)
    exception_type_line = re.search(r'^Exception Type:\s*([^\n\r]+)$', header_scope, re.IGNORECASE | re.MULTILINE)
    if exception_type_line and "exc_crash" in exception_type_line.group(1).lower():
        # 从圆括号中提取信号名称，如 SIGABRT
        m_sig = re.search(r'\(([A-Z0-9_]+)\)', exception_type_line.group(1))
        if m_sig:
            sig_name = m_sig.group(1)
            signal = sig_name
            if crash_reason == "unknown":
                crash_reason = signal_reason_map.get(sig_name, "native_crash")
            if category is None:
                category = "native_crash"
            primary_language = primary_language or "objc"

    signal_match = re.search(r'Signal:([A-Za-z0-9]+)(?:\(([^)]+)\))?', reason_text, re.IGNORECASE)
    if signal_match:
        signal_name = signal_match.group(1).upper()
        signal_code = signal_match.group(2)
        signal = f"{signal_name} ({signal_code})" if signal_code else signal_name
        crash_reason = signal_reason_map.get(signal_name, crash_reason)
    else:
        # 纯 JS/前端崩溃：Reason 为 Error 类型名（TypeError、ReferenceError 等）
        js_error_types = ("TypeError", "ReferenceError", "SyntaxError", "RangeError", "URIError", "EvalError")
        if reason_text and any(reason_text.startswith(t) for t in js_error_types):
            crash_reason = reason_text.split("(")[0].strip() or reason_text
            error_name_match = re.search(r'^Error name:\s*([^\n\r]+)$', header_scope, re.IGNORECASE | re.MULTILINE)
            error_msg_match = re.search(r'^Error message:\s*([^\n\r]+)$', header_scope, re.IGNORECASE | re.MULTILINE)
            err_name = error_name_match.group(1).strip() if error_name_match else ""
            err_msg = error_msg_match.group(1).strip() if error_msg_match else ""
            if err_name or err_msg:
                exception_type = f"{err_name}: {err_msg}".strip(": ").strip() if (err_name and err_msg) else (err_name or err_msg)
            category = category or "js_exception"
            primary_language = primary_language or "arkts"

    crash_patterns = [
        ('segmentation fault', 'segmentation fault'),
        ('segfault', 'segmentation fault'),
        ('段错误', 'segmentation fault'),
        ('null pointer', 'null pointer dereference'),
        ('空指针', 'null pointer dereference'),
        ('out of memory', 'out of memory'),
        ('内存不足', 'out of memory'),
        ('stack overflow', 'stack overflow'),
        ('栈溢出', 'stack overflow'),
        ('illegal instruction', 'illegal instruction'),
        ('非法指令', 'illegal instruction'),
        ('abort', 'abort'),
        ('终止', 'abort'),
        ('access violation', 'access violation'),
        ('访问违规', 'access violation'),
        ('divide by zero', 'divide by zero'),
        ('除零错误', 'divide by zero'),
        ('assertion failed', 'assertion failed'),
        ('断言失败', 'assertion failed'),
        ('exc_bad_access', 'segmentation fault'),
        ('sigsegv', 'segmentation fault'),
        ('sigill', 'illegal instruction'),
        ('sigabrt', 'abort'),
        ('sigalrm', 'timeout'),
    ]

    for pattern, reason in crash_patterns:
        if crash_reason == "unknown" and pattern in content_lower:
            crash_reason = reason
            break

    if not signal:
        # Android tombstone: signal 11 (SIGSEGV), code 1 (SEGV_MAPERR), fault addr ...
        full_sig = re.search(
            r"signal\s+(\d+)\s*\((\w+)\)\s*,\s*code\s+(-?\d+)\s*(?:\((\w+)\))?",
            header_scope,
            re.IGNORECASE,
        )
        if full_sig:
            sig_name = full_sig.group(2).upper()
            code_name = full_sig.group(4)
            signal = f"{sig_name} ({code_name})" if code_name else f"{full_sig.group(1)} ({sig_name})"
            crash_reason = signal_reason_map.get(sig_name, crash_reason)

    if not signal:
        signal_patterns = [
            r'signal\s+(\d+)\s*\(([^)]+)\)',
            r'signal\s+(\d+)',
            r'Signal:([A-Za-z0-9]+)\(([^)]+)\)',
            r'Signal:([A-Za-z0-9]+)',
            r'fault\s+addr\s+([0-9a-fA-F]{8,16})',
            r'Exception\s+Code:\s+([0-9a-fA-F]{8})',
            r'signal\s+(\d+)\s+code\s+(-?\d+)',
        ]
        for pattern in signal_patterns:
            signal_match = re.search(pattern, header_scope, re.IGNORECASE)
            if signal_match:
                groups = signal_match.groups()
                signal = f"{signal_match.group(1)} ({signal_match.group(2)})" if len(groups) >= 2 else signal_match.group(1)
                break

    if not signal:
        if 'segmentation fault' in content_lower or 'null pointer' in content_lower or 'sigsegv' in content_lower:
            signal = "11 (SIGSEGV)"
        elif 'illegal instruction' in content_lower or 'sigill' in content_lower:
            signal = "6 (SIGILL)"
        elif 'abort' in content_lower or 'sigabrt' in content_lower:
            signal = "6 (SIGABRT)"
        elif 'timeout' in content_lower or 'sigalrm' in content_lower:
            signal = "14 (SIGALRM)"
        elif 'access violation' in content_lower:
            signal = "0xC0000005"

    # 基于关键字的通用场景分类（category）补全
    low = content_lower
    heap_abort = is_heap_allocator_abort(abort_message, content)
    if heap_abort and (not crash_reason or crash_reason in {"unknown", "abort"}):
        crash_reason = "heap allocator abort"
    if category is None:
        if "out of memory" in low or "outofmemoryerror" in low or "lowmemory" in low:
            category = "oom"
        elif "anr" in low or "appfreeze" in low or "application not responding" in low:
            category = "anr"
        elif heap_abort:
            category = "native_crash"
        elif re.search(
            r"gpu\s+(crash|fault)|vk_error_device_lost|gles_crash|gpu hung",
            low,
        ):
            category = "gpu_crash"
        elif "ability" in low or "entryability" in low:
            category = "ability_crash"
        elif signal:
            category = "native_crash"

    if category is None and _android_heuristic_anr_stack(content):
        category = "anr"
        if crash_reason == "unknown":
            crash_reason = "application not responding (suspected)"
        primary_language = primary_language or "java"

    if category is None and _android_mixed_native_java_jni_sample(content):
        category = "native_crash"
        if crash_reason == "unknown":
            crash_reason = "native crash (jni / stack sample)"
        primary_language = primary_language or "cpp"

    if category is None and _android_native_only_pc_stack_sample(content):
        category = "native_crash"
        if crash_reason == "unknown":
            crash_reason = "native crash (stack sample)"
        primary_language = primary_language or "cpp"

    if exception_type is None:
        exception_patterns = [
            r'exception:\s*([^\n\r]+)',
            r'Exception\s+Type:\s*([^\n\r]+)',
            r'Error\s+Type:\s*([^\n\r]+)',
        ]
        for pattern in exception_patterns:
            exception_match = re.search(pattern, header_scope, re.IGNORECASE)
            if exception_match:
                exception_type = exception_match.group(1).strip()
                break

    if reason_text:
        reason_address_match = re.search(r'@0x([0-9a-fA-F]+)', reason_text)
        if reason_address_match:
            crash_address = f"0x{reason_address_match.group(1)}"

    if not crash_address:
        address_patterns = [
            r'崩溃地址:\s*0x([0-9a-fA-F]{8,16})',
            r'崩溃地址:\s*([0-9a-fA-F]{8,16})',
            r'crash address:\s*0x([0-9a-fA-F]{8,16})',
            r'crash address:\s*([0-9a-fA-F]{8,16})',
            r'fault address:\s*0x([0-9a-fA-F]{8,16})',
            r'fault address:\s*([0-9a-fA-F]{8,16})',
            # Android logcat / tombstone：``fault addr 0xa0``
            r'fault\s+addr\s+(0x[0-9a-fA-F]{1,16})\b',
        ]

        for pattern in address_patterns:
            address_match = re.search(pattern, header_scope, re.IGNORECASE)
            if address_match:
                crash_address = address_match.group(1)
                break

    return CrashInfo(
        thread_type=thread_type,
        crash_reason=crash_reason,
        signal=signal,
        exception_type=exception_type,
        crash_address=crash_address,
        category=category,
        primary_language=primary_language,
        abort_message=abort_message or None,
    )

def extract_meta_info(content: str) -> MetaInfo:
    """提取元信息"""
    os_type = detect_os_type(content)
    header_scope = "\n".join(content.splitlines()[:200])
    
    # 提取平台信息
    platform = None
    platform_match = re.search(r'^平台:\s*([^\n\r]+)$', header_scope, re.MULTILINE)
    if platform_match:
        platform = platform_match.group(1).strip()
    
    # 提取进程名（如 iOS Process: / Android Process:）
    process_name = None
    proc_match = re.search(r'^Process:\s*([^\[\n\r]+)', header_scope, re.IGNORECASE | re.MULTILINE)
    if proc_match:
        process_name = proc_match.group(1).strip()
    if process_name is None:
        # 已符号化精简 iOS 导出：``* AppName crash_function``
        pre_parsed_proc = re.search(
            r"^\*\s*(?!SIG)(\S+)\s+(.+)$",
            header_scope,
            re.MULTILINE | re.IGNORECASE,
        )
        if pre_parsed_proc:
            process_name = pre_parsed_proc.group(1).strip()
    if process_name is None:
        cmdline_m = re.search(r'Cmdline:\s*(\S+)', header_scope, re.IGNORECASE)
        if cmdline_m:
            process_name = cmdline_m.group(1).strip()

    # 提取时间戳
    timestamp = None
    timestamp_match = re.search(r'^(?:时间|Timestamp):\s*([^\n\r]+)$', header_scope, re.IGNORECASE | re.MULTILINE)
    if timestamp_match:
        timestamp = timestamp_match.group(1).strip()
    
    # 提取进程ID
    process_id = None
    pid_match = re.search(r'^(?:进程ID|Pid):\s*(\d+)$', header_scope, re.IGNORECASE | re.MULTILINE)
    if pid_match:
        process_id = pid_match.group(1)
    if process_id is None:
        # Android DEBUG dump：``pid: 9568, tid: 9753, name: ...``
        pid_dbg = re.search(
            r'\bpid:\s*(\d+),\s*tid:\s*\d+',
            header_scope,
            re.IGNORECASE,
        )
        if pid_dbg:
            process_id = pid_dbg.group(1)
    
    # 提取编译器信息
    compiler = None
    compiler_match = re.search(r'^(?:编译器|Compiler):\s*([^\n\r]+)$', header_scope, re.IGNORECASE | re.MULTILINE)
    if compiler_match:
        compiler = compiler_match.group(1).strip()
    
    # 提取模块基址信息
    module_base_addresses = {}
    
    # 提取主程序基址
    main_base_match = re.search(r'主程序基址:\s*([0-9a-fA-Fx]+)', content)
    if main_base_match:
        module_base_addresses['main'] = main_base_match.group(1)
    
    # 提取libmylib.dylib基址
    lib_base_match = re.search(r'libmylib\.dylib基址:\s*([0-9a-fA-Fx]+)', content)
    if lib_base_match:
        module_base_addresses['libmylib.dylib'] = lib_base_match.group(1)
    
    # 提取libsystem_pthread.dylib基址
    pthread_base_match = re.search(r'libsystem_pthread\.dylib基址:\s*([0-9a-fA-Fx]+)', content)
    if pthread_base_match:
        module_base_addresses['libsystem_pthread.dylib'] = pthread_base_match.group(1)
    
    # 提取libsystem_platform.dylib基址
    platform_base_match = re.search(r'libsystem_platform\.dylib基址:\s*([0-9a-fA-Fx]+)', content)
    if platform_base_match:
        module_base_addresses['libsystem_platform.dylib'] = platform_base_match.group(1)
    
    # 如果基址信息缺失，尝试从堆栈跟踪中自动提取（macOS格式）
    if not module_base_addresses:
        # macOS堆栈格式: #1 0x1023cb2bc 1   libmylib.dylib  0x00000001023cb2bc _Z14signal_handleriP9__siginfoPv + 468
        # 从堆栈跟踪中提取每个模块的第一个符号地址，使用页对齐算法估算基址
        macos_stack_pattern = r'#\d+\s+([0-9a-fA-Fx]+)\s+\d+\s+([^\s]+)\s+([0-9a-fA-Fx]+)\s+[^\s]+\s+\+\s+\d+'
        matches = re.finditer(macos_stack_pattern, content, re.MULTILINE)
        
        module_first_addresses = {}  # 记录每个模块的第一个地址
        for match in matches:
            actual_addr_str = match.group(1)
            module = match.group(2)
            symbol_addr_str = match.group(3)
            
            try:
                actual_addr = int(actual_addr_str, 16)
                symbol_addr = int(symbol_addr_str, 16)
                
                # 对于每个模块，记录第一个地址
                if module not in module_first_addresses:
                    module_first_addresses[module] = actual_addr
                    
                    # 使用页对齐算法估算基址
                    # macOS 通常使用 64KB 或 1MB 页对齐
                    # 尝试多种对齐方式，选择最小的合理基址
                    candidates = [
                        (actual_addr // 0x10000) * 0x10000,  # 64KB对齐
                        (actual_addr // 0x100000) * 0x100000,  # 1MB对齐
                        (actual_addr // 0x1000) * 0x1000,  # 4KB对齐（备用）
                    ]
                    
                    # 选择最小的合理候选（但至少是4KB对齐）
                    for candidate in candidates:
                        if candidate > 0 and candidate <= actual_addr:
                            module_base_addresses[module] = f"0x{candidate:x}"
                            logger.debug(f"从堆栈跟踪估算模块基址: {module} = 0x{candidate:x} (从地址 0x{actual_addr:x})")
                            break
            except (ValueError, AttributeError):
                continue
    
    # 增强的OS版本提取
    os_version = None
    version_patterns = {
        'android': [
            r'android\s+([0-9.]+)',
            r'API\s+level\s+(\d+)',
            r'SDK\s+([0-9.]+)',
        ],
        'ios': [
            r'^\s*OS Version:\s*iOS\s+([0-9.]+)',
            r'ios\s+([0-9.]+)',
            r'iPhone\s+OS\s+([0-9.]+)',
            r'iPadOS\s+([0-9.]+)',
        ],
        'linux': [
            r'linux\s+([0-9.]+)',
            r'kernel\s+([0-9.]+)',
            r'ubuntu\s+([0-9.]+)',
            r'centos\s+([0-9.]+)',
            r'debian\s+([0-9.]+)',
        ],
        'macos': [
            r'macos\s+([0-9.]+)',
            r'os\s+x\s+([0-9.]+)',
            r'darwin\s+([0-9.]+)',
        ],
        'windows': [
            r'windows\s+([0-9.]+)',
            r'win\s+([0-9.]+)',
            r'nt\s+([0-9.]+)',
        ]
    }
    
    if os_type in version_patterns:
        for pattern in version_patterns[os_type]:
            version_match = re.search(pattern, content, re.IGNORECASE | re.MULTILINE)
            if version_match:
                os_version = version_match.group(1)
                break
    
    # 增强的应用版本提取
    app_version = None
    app_version_patterns = [
        r'^Version:\s*([0-9.]+)$',
        r'^version[:\s]+([0-9.]+)$',
        r'^ver[:\s]+([0-9.]+)$',
        r'^build[:\s]+([0-9.]+)$',
        r'^app[:\s]+([0-9.]+)$',
        # Android tombstone / bugreport：``Package: com.foo v123 (1.2.3)`` 取括号内版本
        r'^\s*Package:\s+\S+\s+v\d+\s+\(([^)]+)\)',
    ]
    
    for pattern in app_version_patterns:
        version_match = re.search(pattern, header_scope, re.IGNORECASE | re.MULTILINE)
        if version_match:
            app_version = version_match.group(1)
            break
    
    # 增强的设备型号提取
    device_model = None
    device_patterns = [
        r'^Device info:\s*([^\n\r]+)$',
        r'^Device:\s*([^\n\r]+)$',
        r'^设备(?:信息|型号)?[:：]\s*([^\n\r]+)$',
        r'^Model:\s*([^\n\r]+)$',
        r'^Hardware:\s*([^\n\r]+)$',
        r'^Hardware Model:\s*([^\n\r]+)$',
    ]
    
    for pattern in device_patterns:
        model_match = re.search(pattern, header_scope, re.IGNORECASE | re.MULTILINE)
        if model_match:
            device_model = model_match.group(1).strip()
            break
    
    # 如果没有检测到OS类型，根据平台信息推断
    if os_type == 'unknown' and platform:
        if platform.lower() == 'mac':
            os_type = 'macos'
        elif platform.lower() in ['android', 'ios', 'linux', 'windows']:
            os_type = platform.lower()
    
    # 如果没有检测到OS类型，根据内容特征推断（勿用裸子串 dyld：iOS Binary Images 含 libdyld.dylib）
    if os_type == "unknown":
        if _detect_harmony_native_stack(content):
            os_type = "harmonyos"
        elif (
            "harmonyos" in content.lower()
            or "openharmony" in content.lower()
            or "build info:mro" in content.lower()
            or "com.ohos." in content.lower()
            or re.search(r"\bohos\b", content.lower())
        ):
            os_type = "harmonyos"
        elif _IOS_OS_VERSION_RE.search(content) or _IOS_HW_MODEL_RE.search(content):
            os_type = "ios"
        elif _detect_apple_ios_truncated_crash(content):
            os_type = "ios"
        elif _detect_apple_ios_freeze_report(content):
            os_type = "ios"
        elif _detect_ios_mach_tool_export(content):
            os_type = "ios"
        elif _detect_ios_pre_parsed_symbolized_crash(content):
            os_type = "ios"
        elif looks_like_cpp_qualified_stack(content):
            os_type = "ios"
        elif re.search(r"^\s*OS Version:\s*macOS\b", content, re.MULTILINE | re.IGNORECASE) or re.search(
            r"^\s*OS Version:\s*Mac OS X\b", content, re.MULTILINE | re.IGNORECASE
        ):
            os_type = "macos"
        elif "apple llvm" in content.lower():
            os_type = "macos"
        elif platform and platform.lower() == "mac":
            os_type = "macos"
    
    # 提取架构信息
    arch = None
    arch_patterns = [
        r'^(?:architecture|arch|架构)[:：]\s*([A-Za-z0-9._-]+)$',
        r'\b(arm64-v8a|armeabi-v7a|armeabi)\b',
        # Tombstone：``ABI: 'arm64'``
        r'^\s*ABI:\s*[\'"]?([a-zA-Z0-9_-]+)[\'"]?\s*$',
    ]
    
    for pattern in arch_patterns:
        arch_match = re.search(pattern, header_scope, re.IGNORECASE | re.MULTILINE)
        if arch_match:
            arch = arch_match.group(1).strip() if arch_match.lastindex else arch_match.group(0)
            break
    
    # 如果没找到，从模块路径或堆栈信息中推断
    if not arch:
        # 从路径中提取架构信息（如 /lib/arm64/, /lib64/, /data/storage/el1/bundle/libs/arm64/）
        path_arch_patterns = [
            r'/lib(?:64)?/(arm64|aarch64|x86_64|armv7|armv8|i386|i686|x86)',
            r'/libs/(arm64|aarch64|x86_64|armv7|armv8|i386|i686|x86)',
        ]
        for pattern in path_arch_patterns:
            arch_match = re.search(pattern, content, re.IGNORECASE)
            if arch_match:
                arch = arch_match.group(1) if arch_match.lastindex else arch_match.group(0)
                # 标准化架构名称
                if arch.lower() in ['aarch64', 'arm64']:
                    arch = 'arm64'
                elif arch.lower() in ['x86_64', 'amd64']:
                    arch = 'x86_64'
                elif arch.lower() in ['armv7', 'armeabi-v7a']:
                    arch = 'armv7'
                break
    
    # 提取符号文件路径
    symbol_path = None
    symbol_patterns = [
        r'symbol[_\s]?path[:\s]+([^\n\r]+)',
        r'dsym[_\s]?path[:\s]+([^\n\r]+)',
        r'\.dSYM[:\s]+([^\n\r]+)',
        r'\.pdb[:\s]+([^\n\r]+)',
        r'symbol[_\s]?file[:\s]+([^\n\r]+)',
    ]
    for pattern in symbol_patterns:
        symbol_match = re.search(pattern, content, re.IGNORECASE)
        if symbol_match:
            symbol_path = symbol_match.group(1).strip()
            break
    
    # 增强编译器信息提取
    if not compiler:
        compiler_patterns = [
            r'^(Apple LLVM version[^\n\r]+)$',
            r'^(?:clang|gcc|g\+\+) version[^\n\r]+$',
            r'^(?:llvm|msvc)[^\n\r]*$',
        ]
        for pattern in compiler_patterns:
            compiler_match = re.search(pattern, header_scope, re.IGNORECASE | re.MULTILINE)
            if compiler_match:
                compiler = compiler_match.group(1) if compiler_match.lastindex else compiler_match.group(0)
                break
    
    # Harmony Ability 名称（简单从文本中抓取包含 EntryAbility 的行）
    ability_name = None
    ability_match = re.search(r'EntryAbility', content)
    if ability_match:
        ability_name = "EntryAbility"

    # 强分类 log_kind（并兼容 anr_suspected / oom_suspected）
    from tools.crash_parser.log_kind_classifier import classify_log_kind

    kind = classify_log_kind(content)
    meta_fields = kind.to_meta_fields()
    anr_suspected = True if meta_fields.get("anr_suspected") else None
    oom_suspected = True if meta_fields.get("oom_suspected") else None

    return MetaInfo(
        os_type=os_type,
        os_version=os_version,
        app_version=app_version,
        device_model=device_model,
        timestamp=timestamp,
        platform=platform,
        compiler=compiler,
        process_id=process_id,
        module_base_addresses=module_base_addresses if module_base_addresses else None,
        arch=arch,
        symbol_path=symbol_path,
        ability_name=ability_name,
        process_name=process_name,
        anr_suspected=anr_suspected,
        oom_suspected=oom_suspected,
        log_kind=kind.log_kind,
        log_kind_confidence=kind.confidence,
        log_kind_reasons=list(kind.reasons) if kind.reasons else None,
    )
