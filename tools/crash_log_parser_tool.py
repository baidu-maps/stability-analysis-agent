#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
崩溃日志提取工具（CLI / Tool 入口）。

解析实现已渐进式拆至 ``tools/crash_parser/`` 子包；本文件保留原有 import 路径兼容。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, replace
from typing import Any, Dict, List, Optional

from tools.crash_parser import (
    CrashParseOptions,
    crash_parse_options_from_cli_args,
    detect_os_type,
    select_crash_parser,
)
from tools.crash_parser.types import (
    CrashAnalysisResult,
    CrashInfo,
    MetaInfo,
    StackFrame,
    ThreadStack,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 向后兼容：外部或测试可能直接 import 这些符号
from tools.crash_parser.core import parse_crash_core as _parse_crash_core  # noqa: E402
from tools.crash_parser.format_detect import (  # noqa: E402
    _detect_ios_pre_parsed_symbolized_crash,
    _detect_ios_mach_tool_export,
)
from tools.crash_parser.parsers import (  # noqa: E402
    BaseCrashParser,
    DefaultCrashParser,
    PARSERS,
)
from tools.crash_parser.stack_extract import extract_stack_frames  # noqa: E402
from tools.crash_parser.stack_lines import _try_parse_ios_pre_parsed_stack_line  # noqa: E402
from tools.crash_parser.meta import extract_crash_info, extract_meta_info  # noqa: E402

__all__ = [
    "BaseCrashParser",
    "CrashAnalysisResult",
    "CrashInfo",
    "CrashParseOptions",
    "CrashLogParserTool",
    "DefaultCrashParser",
    "MetaInfo",
    "PARSERS",
    "StackFrame",
    "ThreadStack",
    "_detect_ios_pre_parsed_symbolized_crash",
    "_parse_crash_core",
    "_try_parse_ios_pre_parsed_stack_line",
    "crash_log_parser",
    "crash_parse_options_from_cli_args",
    "detect_os_type",
    "extract_crash_info",
    "extract_meta_info",
    "extract_stack_frames",
]


def crash_log_parser(
    content: str,
    debug: bool = False,
    options: Optional[CrashParseOptions] = None,
) -> str:
    """
    崩溃日志提取工具（带解析器注册表）

    options: 解析参数；未传入时使用 CrashParseOptions 默认值（含 crash_segment_index=1）。
    """
    try:
        logger.info("开始解析崩溃日志...")
        content = content.replace("\x00", "")
        os_type = detect_os_type(content)

        opts = options if options is not None else CrashParseOptions()
        opts = replace(opts, crash_segment_index=max(1, int(opts.crash_segment_index)))

        parser = select_crash_parser(content, os_type)
        result = parser.parse(content, os_type=os_type, debug=debug, options=opts)

        def _strip_false_flags(obj: Any) -> Any:
            if isinstance(obj, dict):
                new_obj: Dict[str, Any] = {}
                for k, v in obj.items():
                    if k.startswith("has_") and v is False:
                        continue
                    new_obj[k] = _strip_false_flags(v)
                return new_obj
            if isinstance(obj, list):
                return [_strip_false_flags(i) for i in obj]
            return obj

        cleaned_dict = _strip_false_flags(asdict(result))
        logger.info("崩溃日志提取完成")
        return json.dumps(cleaned_dict, ensure_ascii=False, indent=2)

    except Exception as e:
        logger.error(f"解析崩溃日志时出错: {e}")
        return json.dumps({"error": str(e), "raw_content": content}, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    import sys

    if not sys.stdin.isatty():
        print(crash_log_parser(sys.stdin.read()))
    else:
        sample = "zsh: segmentation fault\n"
        print(crash_log_parser(sample))


# ==================== CrashLogParserTool (BaseTool wrapper) ====================

from tool_system.tool import BaseTool, ToolDefinition  # noqa: E402


class CrashLogParserTool(BaseTool):
    """崩溃日志解析工具 — 内置 Tool 实现。"""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="crash_log_parser",
            description=(
                "解析崩溃日志，从原始文本提取堆栈地址、异常类型、崩溃原因、关键线程等结构化信息。"
                "支持 iOS/Android/鸿蒙等平台。（对外唯一日志解析工具；历史名称 log_filter 已废弃）"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "log_content": {"type": "string", "description": "崩溃日志内容"},
                    "debug": {"type": "boolean", "description": "调试模式", "default": False},
                },
                "required": ["log_content"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "thread_type": {"type": "string"},
                    "crash_reason": {"type": "string"},
                    "signal": {"type": "string"},
                    "exception_type": {"type": "string"},
                    "stack_frames": {"type": "array"},
                },
            },
            category="parser",
            version="1.1.0",
            risk="read_only",
            side_effect=False,
            idempotent=True,
            requires_approval=False,
            cost_class="low",
        )

    def execute(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        log_content = input_data.get("log_content", "")
        debug = bool(input_data.get("debug", False))
        options = None
        if "options" in input_data:
            options = CrashParseOptions(**input_data["options"])
        if options is None:
            options = CrashParseOptions()
        result = crash_log_parser(log_content, debug=debug, options=options)
        try:
            return json.loads(result)
        except Exception:
            return {"raw_result": result}

    def validate_input(self, input_data: Dict[str, Any]) -> tuple[bool, Optional[str]]:
        if "log_content" not in input_data:
            return False, "缺少 required 字段: log_content"
        return True, None
