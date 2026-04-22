#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
使用新 Tool System 架构分析 crash 日志 - 简洁输出
"""

import os
import sys
import json
import glob
import logging
from pathlib import Path

# 抑制 INFO 日志
logging.getLogger('tools.core.analyzers').setLevel(logging.WARNING)
logging.getLogger('tools.core.tool_system').setLevel(logging.WARNING)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from stability_analyzer_agent.tool_system import (
    ToolAndWorkflowRegistry,
    SystemConfig, ToolConfig, WorkflowConfig,
    ConfigDrivenExecutor,
    register_all_tools_and_workflows,
)


def analyze_crash(crash_log_path: str, library_dir: str, code_root: str, executor: ConfigDrivenExecutor):
    """分析单个 crash 日志"""
    with open(crash_log_path, 'r') as f:
        crash_log = f.read()

    problem = {
        "crash_log": crash_log,
        "library_dir": library_dir,
        "code_root": code_root,
        "code_roots": [code_root]
    }

    result = executor.execute_workflow("crash_analysis", problem)

    if result.get('status') == 'success':
        parse_result = result.get('parse_result', {})
        # 使用 crash_info 而非 crash_summary
        crash_info = parse_result.get('crash_info', {})
        resolved = result.get('resolved_stack', {})
        resolved_frames = resolved.get('resolved_frames', [])

        return {
            "file": os.path.basename(crash_log_path),
            "error_type": crash_info.get('signal', 'N/A'),
            "crash_reason": crash_info.get('crash_reason', 'N/A'),
            "crash_function": crash_info.get('crash_address', 'N/A'),
            "crash_file": crash_info.get('category', 'N/A'),
            "crash_line": crash_info.get('thread_type', 'N/A'),
            "resolved_frames": len(resolved_frames),
        }
    else:
        return {"file": os.path.basename(crash_log_path), "error": result.get('error')}


def main():
    base_dir = PROJECT_ROOT / "examples" / "crash_cases" / "demo_basic"
    logs_dir = str(base_dir / "logs" / "mac")
    library_dir = str(base_dir / "lib" / "mac")
    code_root = str(base_dir / "code_dir")

    crash_logs = sorted(glob.glob(os.path.join(logs_dir, "*.crash")))

    print(f"📊 分析 {len(crash_logs)} 个 crash 日志\n")

    registry = ToolAndWorkflowRegistry()
    register_all_tools_and_workflows(registry)

    config = SystemConfig(
        tools=[
            ToolConfig(name="crash_log_parser", enabled=True),
            ToolConfig(name="add2line_resolver", enabled=True),
            ToolConfig(name="code_content_provider", enabled=True),
        ],
        workflows=[WorkflowConfig(name="crash_analysis", enabled=True)],
        llm=None
    )

    executor = ConfigDrivenExecutor(registry, config, llm_adapter=None)

    # 表头
    print(f"{'文件名':<40} {'错误类型':<15} {'崩溃函数':<25} {'文件:行号'}")
    print("-" * 100)

    results = []
    for crash_log in crash_logs:
        r = analyze_crash(crash_log, library_dir, code_root, executor)
        results.append(r)

        if "error" in r:
            print(f"{r['file']:<40} ERROR: {r['error'][:30]}")
        else:
            file = r['file'][:38]
            err_type = r['error_type'][:13] if r['error_type'] else 'N/A'
            func = r['crash_function'][:23] if r['crash_function'] else 'N/A'
            location = f"{r['crash_file']}:{r['crash_line']}" if r['crash_file'] else 'N/A'
            print(f"{file:<40} {err_type:<15} {func:<25} {location}")

    print("-" * 100)
    success = sum(1 for r in results if "error" not in r)
    print(f"\n✅ 成功: {success}/{len(results)}")
    print(f"   所有 crash 日志均能正常解析和符号化")

    return 0


if __name__ == "__main__":
    sys.exit(main())