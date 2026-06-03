#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从日志文本提取 StackFrame 列表。"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import replace
from typing import List, Optional, Tuple

from tools._stack_symbol_utils import sanitize_stack_symbol
from tools.crash_parser.android import (
    _ANDROID_JAVA_AT_RE,
    _ANDROID_SANITIZER_AUX_STACK_HEADER_RE,
    _ART_NATIVE_PC_IN_LINE_RE,
    _ART_NATIVE_PC_LINE_RE,
    _backtrace_segment_ranges,
    _looks_like_android_java_location,
    _parse_android_java_location,
    _parse_art_native_pc_tail,
)
from tools.crash_parser.stack_lines import (
    _try_parse_ios_macos_stack_line,
    _try_parse_ios_pre_parsed_stack_line,
    _try_parse_ios_symbol_only_stack_line,
)
from tools.crash_parser.types import StackFrame

logger = logging.getLogger(__name__)

def _extract_frames_for_crash_segment(
    scope_content: str,
    debug: bool,
    crash_segment_index: int,
    *,
    global_line_start: int = 1,
) -> Tuple[List[StackFrame], int, int, int]:
    """
    在 scope_content 内按 ``backtrace:`` 分段，只解析第 resolved 段。

    返回 (frames, backtrace_segment_count, resolved_segment_index, requested_segment_index)；后两者仅用于内部分段逻辑。
    """
    lines = scope_content.splitlines()
    ranges = _backtrace_segment_ranges(lines)
    count = len(ranges)
    req = max(1, int(crash_segment_index))
    resolved = min(req, count)
    if req > count:
        logger.warning(
            "请求的 crash 段索引 %s 超过检测到的段数 %s，已使用第 %s 段",
            req,
            count,
            resolved,
        )
    s, e = ranges[resolved - 1]
    block = "\n".join(lines[s:e])
    frames = extract_stack_frames(block, debug, base_raw_log_line=global_line_start + s)
    return frames, count, resolved, req


def extract_stack_frames(
    content: str,
    debug: bool = False,
    *,
    base_raw_log_line: int = 1,
) -> List[StackFrame]:
    """提取堆栈帧信息。

    base_raw_log_line: 当前 ``content`` 首行在完整原始日志中的 1-based 行号（按线程切块解析时传入）。
    """
    stack_frames = []

    if debug:
        logger.info("开始提取堆栈帧...")

    def _classify_library(module: Optional[str]) -> str:
        if not module:
            return "unknown"
        name = module.strip()
        base = name
        if ".so." in base:
            base = base.split(".so.")[0] + ".so"
        base_lower = base.lower()

        system_prefixes = (
            "libc.so", "libm.so", "libstdc++", "libdl.so",
            "libunwind.so", "liblog.so", "ld-musl-aarch64.so",
            "libsqlite.so",
        )
        system_exact = {
            "ld-musl-aarch64.so.1",
            "libffrt.so",
            "libeventhandler.z.so",
            "libhicollie.z.so",
            "libipc_core.z.so",
            "libipc_common.z.so",
            "libhdc_register.z.so",
            "libappspawn_ace.z.so",
            "appspawn",
        }
        platform_exact = {
            "libark_jsruntime.so",
            "libace_napi.z.so",
        }

        if base_lower.startswith(system_prefixes) or name in system_exact or base in system_exact:
            return "system"
        if name in platform_exact or base in platform_exact:
            return "system"
        return "app"

    def _extract_module_name(module_path: str) -> str:
        """
        从路径/原始模块字段中提取“纯模块名”，去掉路径、括号内符号信息以及尾部的架构/BuildId 装饰。
        
        目标形态：
        - 输入: "/path/to/libxxx.so (_Zfoo+12) [arm64-v8a::xxxxxxxx]"
        - 输出: "libxxx.so"
        - 输入: "/product/.../Foo.apk!libxxx.so" → ``libxxx.so``（APK 内嵌 so）
        """
        module = module_path.split('/')[-1] if '/' in module_path else module_path
        # 去掉括号中的符号信息，例如 "libxxx.so (_Zfoo+12"
        if '(' in module:
            module = module.split('(')[0]
        # 去掉尾部的 "[arm64-v8a::buildId]" 等装饰，只保留纯文件名
        # 日志示例：
        #   libBaiduMapSDK_map_for_privatenavi_v7_6_1.so [arm64-v8a::1cf7a8708f9a04a37f85cdffa7983068]
        if ' [' in module:
            module = module.split(' [', 1)[0]
        module = module.strip()
        mod_l = module.lower()
        if ".apk!" in mod_l:
            idx = mod_l.find(".apk!")
            module = module[idx + len(".apk!") :].strip()
        return module.strip()

    def _extract_js_module(file_path: str) -> Optional[str]:
        if not file_path:
            return None
        if "|" in file_path:
            for part in file_path.split("|"):
                part = part.strip()
                if part.startswith("@"):
                    return part
        return os.path.basename(file_path) or None

    # macOS 带 #N 前缀的栈
    macos_pattern = re.compile(
        r'^#(\d+)\s+([0-9a-fA-Fx]+)\s+(\d+)\s+([^\s]+)\s+([0-9a-fA-Fx]+)\s+([^\s]+)\s+\+\s+(\d+)$'
    )
    # HarmonyOS / Android ART native「#NN pc」与「native: #NN pc」由模块级 _ART_NATIVE_PC_LINE_RE 统一解析
    js_stack_pattern = re.compile(r'^#(\d+)\s+at\s+(.+?)\s+\((.+?):(\d+):(\d+)\)\s*$')
    # HarmonyOS/OpenHarmony 纯 JS 崩溃：Stacktrace: 下 "    at func (path:line:col)" 无 #NN 前缀
    js_stack_plain_pattern = re.compile(r'^\s*at\s+(.+?)\s+\((.+):(\d+):(\d+)\)\s*$')
    # 匿名/入口栈行: "    at (path:line:col)"
    js_stack_anonymous_pattern = re.compile(r'^\s*at\s+\((.+):(\d+):(\d+)\)\s*$')
    asan_pattern = re.compile(
        r'^#(\d+)\s+([0-9a-fA-Fx]+)\s+\(([^)]+)\+([0-9a-fA-Fx]+)\)(?:\s+\(BuildId:\s+[^)]+\))?$'
    )
    current_stack_type = "read"

    for line_no, line in enumerate(content.splitlines(), start=1):
        raw_log_line = base_raw_log_line + line_no - 1
        stripped = line.strip()
        if not stripped:
            continue

        if _ANDROID_SANITIZER_AUX_STACK_HEADER_RE.search(stripped):
            break

        line_lower = stripped.lower()
        if 'freed by' in line_lower:
            current_stack_type = "freed"
        elif 'previously allocated' in line_lower:
            current_stack_type = "allocated"
        elif 'thread' in line_lower and 'created' in line_lower:
            current_stack_type = "thread_created"
        elif 'read of size' in line_lower or 'write of size' in line_lower:
            current_stack_type = "read"

        # Android ART / HarmonyOS："#NN pc" 或 "native: #NN pc"（含 [anon:dalvik-DEX data] 等含空格的模块名）
        match = _ART_NATIVE_PC_LINE_RE.match(stripped)
        if not match:
            match = _ART_NATIVE_PC_IN_LINE_RE.search(stripped)
        if match:
            try:
                frame_num = int(match.group(1))
                address = match.group(2)
                module, sym = _parse_art_native_pc_tail(match.group(3))
                stack_frames.append(StackFrame(
                    frame_number=frame_num,
                    address=address,
                    function=sym,
                    module=module,
                    library_type=_classify_library(module),
                    layer="native",
                    language="cpp",
                    raw_log_line=raw_log_line,
                ))
            except Exception as e:
                logger.warning(f"解析 ART/HarmonyOS native #pc 堆栈帧时出错: {e}")
            continue

        # macOS 带 #N 前缀的栈
        match = macos_pattern.match(stripped)
        if match:
            try:
                frame_num = int(match.group(1))
                address = match.group(2)
                module = match.group(4)
                function = match.group(6)
                offset = match.group(7)
                if function and function != "===" and not function.isdigit():
                    stack_frames.append(StackFrame(
                        frame_number=frame_num,
                        address=address,
                        function=function,
                        module=module,
                        offset=offset,
                        library_type=_classify_library(module),
                        layer="native",
                        language="cpp",
                        raw_log_line=raw_log_line,
                    ))
            except Exception as e:
                logger.warning(f"解析macOS堆栈帧时出错: {e}")
            continue

        # 已符号化精简 iOS 导出（双序号前缀）；须早于 symbol-only，避免误把帧号当模块
        pre_parsed = _try_parse_ios_pre_parsed_stack_line(line)
        if pre_parsed:
            try:
                module, address, function, offset, frame_num = pre_parsed
                if function and isinstance(function, str) and re.fullmatch(r"0x[0-9a-fA-F]+", function.strip()):
                    function = None
                if function and ("-[" in function or "+[" in function):
                    layer = "objc"
                    language = "objc"
                else:
                    layer = "native"
                    language = "cpp"
                stack_frames.append(StackFrame(
                    frame_number=frame_num,
                    address=address or "",
                    function=function,
                    module=module,
                    offset=offset,
                    library_type=_classify_library(module),
                    layer=layer,
                    language=language,
                    raw_log_line=raw_log_line,
                ))
            except Exception as e:
                logger.warning(f"解析已符号化 iOS 栈帧时出错: {e}")
            continue

        # iOS / macOS .crash 风格栈（没有 #N 前缀）；无 0x 地址时再试纯符号行
        ios_parsed = _try_parse_ios_macos_stack_line(line)
        if not ios_parsed:
            ios_parsed = _try_parse_ios_symbol_only_stack_line(line)
        if ios_parsed:
            try:
                module, address, function, offset = ios_parsed
                # Apple 栈行中有一类形如：
                #   libdispatch.dylib  PC  0x18afca000 + 9072
                # 其中「0x18afca000」是 image base，不是函数名。为避免污染语义，将其归一化为 None。
                if function and isinstance(function, str) and re.fullmatch(r"0x[0-9a-fA-F]+", function.strip()):
                    function = None
                # 仅 ObjC selector（-[Class sel] / +[Class sel]）标 objc；Last Exception 也可能是 C++/系统库
                if function and ("-[" in function or "+[" in function):
                    layer = "objc"
                    language = "objc"
                else:
                    layer = "native"
                    language = "cpp"
                stack_frames.append(StackFrame(
                    frame_number=len(stack_frames),
                    address=address or "",
                    function=function,
                    module=module,
                    offset=offset,
                    library_type=_classify_library(module),
                    layer=layer,
                    language=language,
                    raw_log_line=raw_log_line,
                ))
            except Exception as e:
                logger.warning(f"解析iOS/ObjC栈帧时出错: {e}")
            continue

        # Android Java：at com.pkg.Class.method(File.java:12) / (Native method)
        mjava = _ANDROID_JAVA_AT_RE.match(stripped)
        if mjava:
            loc = mjava.group(2).strip()
            if _looks_like_android_java_location(loc):
                try:
                    func_full = mjava.group(1).strip()
                    jfile, jline = _parse_android_java_location(loc)
                    stack_frames.append(StackFrame(
                        frame_number=len(stack_frames),
                        address="",
                        function=func_full,
                        file=jfile,
                        line=jline,
                        module=os.path.basename(jfile) if jfile else None,
                        library_type="app",
                        layer="java",
                        language="java",
                        raw_log_line=raw_log_line,
                    ))
                except Exception as e:
                    logger.warning(f"解析 Android Java 堆栈帧时出错: {e}")
                continue

        match = js_stack_pattern.match(stripped)
        if match:
            try:
                frame_num = int(match.group(1))
                function = match.group(2).strip()
                file_path = match.group(3).strip()
                line_no = int(match.group(4))
                module = _extract_js_module(file_path)
                stack_frames.append(StackFrame(
                    frame_number=frame_num,
                    address="",
                    function=function,
                    file=file_path,
                    line=line_no,
                    module=module,
                    library_type="app",
                    layer="arkts",
                    language="arkts",
                    raw_log_line=raw_log_line,
                ))
            except Exception as e:
                logger.warning(f"解析ArkTS/JS堆栈帧时出错: {e}")
            continue

        match = js_stack_plain_pattern.match(line)
        if match:
            try:
                function = match.group(1).strip()
                file_path = match.group(2).strip()
                line_no = int(match.group(3))
                module = _extract_js_module(file_path)
                stack_frames.append(StackFrame(
                    frame_number=len(stack_frames),
                    address="",
                    function=function,
                    file=file_path,
                    line=line_no,
                    module=module,
                    library_type="app",
                    layer="arkts",
                    language="arkts",
                    raw_log_line=raw_log_line,
                ))
            except Exception as e:
                logger.warning(f"解析纯JS/ArkTS堆栈帧时出错: {e}")
            continue

        match = js_stack_anonymous_pattern.match(line)
        if match:
            try:
                file_path = match.group(1).strip()
                line_no = int(match.group(2))
                module = _extract_js_module(file_path)
                stack_frames.append(StackFrame(
                    frame_number=len(stack_frames),
                    address="",
                    function="(anonymous)",
                    file=file_path,
                    line=line_no,
                    module=module,
                    library_type="app",
                    layer="arkts",
                    language="arkts",
                    raw_log_line=raw_log_line,
                ))
            except Exception as e:
                logger.warning(f"解析匿名JS堆栈帧时出错: {e}")
            continue

        match = asan_pattern.match(stripped)
        if match:
            try:
                frame_num = int(match.group(1))
                # ASan 行形如：
                #   #0 0x7d1e8310f4  (/.../libapp_BaiduMapBaselib.so+0x4b10f4)
                # 这里更关注 so 内偏移量（+0x4b10f4），用于符号解析与对齐 SDK 发布符号，
                # 因此将 address 取为偏移量，而不是绝对 PC 值。
                absolute_pc = match.group(2)
                module = _extract_module_name(match.group(3))
                offset = match.group(4)
                address = offset
                stack_frames.append(StackFrame(
                    frame_number=frame_num,
                    address=address,
                    module=module,
                    offset=offset,
                    stack_type=current_stack_type,
                    library_type=_classify_library(module),
                    layer="native",
                    language="cpp",
                    raw_log_line=raw_log_line,
                ))
            except Exception as e:
                logger.warning(f"解析AddressSanitizer堆栈帧时出错: {e}")
            continue

    if not stack_frames:
        generic_patterns = [
            r'([0-9a-fA-F]{8,16})\s+([^\s]+)\s+([^\s]+)',
            r'([0-9a-fA-F]{8,16})\s+([^\s]+)',
        ]

        for pattern in generic_patterns:
            matches = re.finditer(pattern, content, re.MULTILINE)
            for i, match in enumerate(matches):
                try:
                    address = match.group(1)
                    function = match.group(2) if len(match.groups()) >= 2 else "unknown"
                    if function and function != "===" and not function.isdigit():
                        match_raw_line = (
                            base_raw_log_line + content.count("\n", 0, match.start())
                        )
                        stack_frames.append(StackFrame(
                            frame_number=i,
                            address=address,
                            function=function,
                            library_type=_classify_library(None),
                            layer="native",
                            raw_log_line=match_raw_line,
                        ))
                except Exception as e:
                    logger.warning(f"解析通用堆栈帧时出错: {e}")
                    continue

    unique_frames: List[StackFrame] = []
    seen_addresses = set()

    for frame in stack_frames:
        if frame.layer is None:
            if getattr(frame, "language", None) == "java":
                frame.layer = "java"
            else:
                frame.layer = "native" if (frame.address and not frame.file) else "arkts"
        if frame.language is None:
            if frame.layer == "native":
                frame.language = "cpp"
            elif frame.layer == "arkts":
                frame.language = "arkts"
            elif frame.layer == "objc":
                frame.language = "objc"
            elif frame.layer == "java":
                frame.language = "java"
        unique_key = "|".join([
            frame.address or "",
            frame.function or "",
            frame.file or "",
            str(frame.line or ""),
            frame.module or "",
            str(frame.raw_log_line or ""),
        ])
        if unique_key not in seen_addresses:
            seen_addresses.add(unique_key)
            fn_clean = sanitize_stack_symbol(frame.function)
            if fn_clean and fn_clean != frame.function:
                frame = replace(frame, function=fn_clean)
            frame.frame_number = len(unique_frames)
            unique_frames.append(frame)

    return unique_frames
