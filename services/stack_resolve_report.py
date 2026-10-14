#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 03_add2line_resolver 结果生成 parse_stack_only 可读 Markdown 报告。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


def _cell(text: Any) -> str:
    """转义 Markdown 表格单元格内容。"""
    value = str(text if text is not None else "").replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("|", "\\|").replace("\n", "<br>")
    return value.strip() or "—"


def format_addr2line_detail(frame: Dict[str, Any]) -> str:
    """把单帧格式化为接近 addr2line 原始输出的两行文本。

    典型形态::

        function_name
        /path/to/file.cpp:123
    """
    if not isinstance(frame, dict):
        return "??"

    function = str(
        frame.get("resolved_function")
        or frame.get("function")
        or ""
    ).strip()
    file_path = str(
        frame.get("resolved_file")
        or frame.get("file")
        or ""
    ).strip()
    line = frame.get("resolved_line")
    if line is None:
        line = frame.get("line")
    line_text = ""
    if line is not None and str(line).strip() not in {"", "None", "N/A"}:
        line_text = str(line).strip()

    location = ""
    if file_path and line_text:
        location = f"{file_path}:{line_text}"
    elif file_path:
        location = file_path
    elif line_text:
        location = f":{line_text}"

    kind = str(frame.get("resolution_kind") or "").strip()
    module = str(frame.get("module") or "").strip()

    if not function and not location:
        if kind == "unresolved":
            return "??"
        if module:
            return f"?? ({module})"
        return "??"

    lines: List[str] = []
    if function:
        lines.append(function)
    else:
        lines.append("??")
    if location:
        lines.append(location)
    elif module:
        lines.append(f"({module})")
    from tools.add2line_resolver_tool import format_inline_caller_note

    note = format_inline_caller_note(frame)
    if note:
        lines.append(note)
    return "\n".join(lines)


def _iter_thread_frames(resolved_stack: Dict[str, Any]) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """按崩溃线程优先顺序展开 (thread, frame)。"""
    threads = resolved_stack.get("resolved_threads")
    pairs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    if isinstance(threads, list) and threads:
        ordered = sorted(
            [t for t in threads if isinstance(t, dict)],
            key=lambda t: (0 if t.get("is_crash_thread") else 1, t.get("thread_index") or 0),
        )
        for thread in ordered:
            for frame in thread.get("frames") or []:
                if isinstance(frame, dict):
                    pairs.append((thread, frame))
        return pairs

    flat = resolved_stack.get("resolved_frames") or resolved_stack.get("frames") or []
    if isinstance(flat, list):
        for frame in flat:
            if isinstance(frame, dict):
                pairs.append(({}, frame))
    return pairs


def build_parse_stack_only_markdown(
    resolved_stack: Optional[Dict[str, Any]],
    *,
    crash_info: Optional[Dict[str, Any]] = None,
    max_frames: int = 200,
) -> str:
    """生成堆栈地址解析报告 Markdown（两列表格）。

    列含义：
    1. 原始堆栈地址（``address``）
    2. addr2line 解析结果（函数 + file:line）
    """
    # 标题用 ##/###，避免 #/## 在预览里相对正文字号过大。
    lines: List[str] = [
        "## 堆栈地址解析结果",
        "",
    ]
    info = crash_info if isinstance(crash_info, dict) else {}
    if info:
        lines.extend(
            [
                "### 崩溃信息",
                f"- **错误类型**: {info.get('signal', 'N/A')}",
                f"- **崩溃原因**: {info.get('crash_reason', 'N/A')}",
                f"- **崩溃地址**: {info.get('crash_address', 'N/A')}",
                f"- **分类**: {info.get('category', 'N/A')}",
                "",
            ]
        )

    stack = resolved_stack if isinstance(resolved_stack, dict) else {}
    pairs = _iter_thread_frames(stack)
    success = stack.get("success_count")
    total = stack.get("total_count")
    frame_total = stack.get("frame_count_total")
    tool_name = str(stack.get("tool_name") or "").strip()
    meta_bits: List[str] = []
    if success is not None and total is not None:
        meta_bits.append(f"成功符号化 {success}/{total} 帧")
    if frame_total is not None:
        meta_bits.append(f"日志总帧 {frame_total}")
    if tool_name:
        meta_bits.append(f"工具 `{tool_name}`")
    if meta_bits:
        lines.append("### 解析摘要")
        lines.append("- " + "；".join(meta_bits))
        lines.append("")

    lines.extend(
        [
            "### 符号化堆栈",
            "",
            "| 原始堆栈地址 | 解析结果（addr2line） |",
            "| --- | --- |",
        ]
    )

    if not pairs:
        lines.append("| — | （无可用符号化帧） |")
        lines.append("")
        return "\n".join(lines)

    shown = 0
    last_thread_key: Optional[str] = None
    for thread, frame in pairs:
        if shown >= max(1, int(max_frames or 200)):
            rest = len(pairs) - shown
            lines.append(f"| — | … 还有 {rest} 帧未列出 |")
            break
        thread_key = (
            f"{thread.get('tid')}|{thread.get('name')}|{thread.get('is_crash_thread')}"
            if thread
            else ""
        )
        if thread and thread_key != last_thread_key:
            last_thread_key = thread_key
            tag_parts: List[str] = []
            if thread.get("is_crash_thread"):
                tag_parts.append("crash")
            if thread.get("is_main_thread") is True:
                tag_parts.append("main")
            elif thread.get("is_main_thread") is False:
                tag_parts.append("bg")
            label = "/".join(tag_parts) or "thread"
            tid = thread.get("tid")
            name = thread.get("name")
            header = f"**[{label}]**"
            if tid is not None:
                header += f" `{tid}`"
            if name:
                header += f" ({name})"
            # 线程分隔行：两列合并语义用首列说明
            lines.append(f"| {_cell(header)} |  |")

        address = str(frame.get("address") or "").strip() or "—"
        detail = format_addr2line_detail(frame)
        lines.append(
            f"| `{_cell(address)}` "
            f"| {_cell(detail)} |"
        )
        shown += 1

    lines.append("")
    return "\n".join(lines)


def build_parse_stack_only_markdown_from_report_dir(report_dir: Any) -> Optional[str]:
    """从报告目录读取 03 JSON 并生成 Markdown；失败返回 None。"""
    from pathlib import Path
    import json

    root = Path(report_dir)
    path = root / "03_add2line_resolver.json"
    if not path.is_file():
        legacy = root / "02_add2line_resolver.json"
        path = legacy if legacy.is_file() else path
    if not path.is_file():
        return None
    try:
        stack = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    crash_info: Optional[Dict[str, Any]] = None
    parse_path = root / "01_crash_log_parser.json"
    if parse_path.is_file():
        try:
            parse_result = json.loads(parse_path.read_text(encoding="utf-8"))
            if isinstance(parse_result, dict):
                info = parse_result.get("crash_info")
                if isinstance(info, dict):
                    crash_info = info
        except (OSError, json.JSONDecodeError):
            pass
    return build_parse_stack_only_markdown(
        stack if isinstance(stack, dict) else {},
        crash_info=crash_info,
    )
