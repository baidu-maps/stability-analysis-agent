#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Standalone CLI for deterministic native leak analysis."""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.native_leak_diagnosis import analyze_native_leak_bundle, collect_source_search_queries


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def render_native_leak_report(result: Dict[str, Any]) -> str:
    overview = result.get("overview") or {}
    lines: List[str] = [
        "# Native 内存泄漏分析报告",
        "",
        "## 分析概览",
        "",
        f"- 场景: {overview.get('scenario') or 'unknown'}",
        f"- 进程: {overview.get('process_name') or 'unknown'}",
        f"- PID: {overview.get('pid') or 'unknown'}",
        f"- 输入: {overview.get('input_root') or ''}",
        "",
        "## 内存增长趋势",
        "",
        "| 指标 | 起始 KB | 结束 KB | 峰值 KB | 增长 | 增长率 | 趋势 |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for name, trend in (result.get("sample") or {}).get("trends", {}).items():
        pct = trend.get("growth_percent")
        lines.append(
            f"| {name} | {trend.get('start_kb')} | {trend.get('end_kb')} | {trend.get('peak_kb')} | "
            f"{trend.get('growth_kb')} KB | {pct if pct is not None else 'N/A'}% | {trend.get('trend')} |"
        )
    if not (result.get("sample") or {}).get("trends"):
        lines.append("| 无有效 sample 趋势 | - | - | - | - | - | - |")

    lines.extend(["", "## PSS 分类", "", "| 类型 | PSS KB | 占比 | 映射数 |", "|---|---:|---:|---:|"])
    for item in (result.get("smaps") or {}).get("categories") or []:
        lines.append(f"| {item.get('type')} | {item.get('pss_kb')} | {float(item.get('share') or 0):.2%} | {item.get('mapping_count')} |")
    if not (result.get("smaps") or {}).get("categories"):
        lines.append("| 无有效 smaps 分类 | - | - | - |")

    nmd = (result.get("smaps") or {}).get("nmd") or {}
    if nmd.get("top_growth"):
        lines.extend(["", "## NMD 增长最大的 Size Class", "", "| Size B | 当前分配 B | 增长 B |", "|---:|---:|---:|"])
        for item in nmd["top_growth"]:
            lines.append(f"| {item.get('size_bytes')} | {item.get('allocated_bytes')} | {item.get('growth_bytes')} |")

    chains = (result.get("native_hook") or {}).get("results") or []
    lines.extend(["", "## 未释放分配调用栈", ""])
    if not chains:
        lines.append("未提供可解析的 native_hook SQLite，或未发现未释放分配记录。")
    for index, chain in enumerate(chains, 1):
        lines.append(
            f"### {index}. {chain.get('outstanding_bytes')} B / {chain.get('percentage')}% / "
            f"{chain.get('allocation_count')} 次"
        )
        lines.append("")
        for frame in reversed(chain.get("frames") or []):
            symbol = frame.get("symbol") or f"0x{int(frame.get('ip') or 0):x}"
            module = frame.get("file") or ""
            lines.append(f"- {symbol}" + (f" [{module}]" if module else ""))
        lines.append("")

    buffers = (result.get("kernel_dma") or {}).get("top_buffers") or []
    if buffers:
        lines.extend(["## DMA Top Buffers", "", "| 大小 B | 进程 | buf_name | leak_type | 组件 |", "|---:|---|---|---|---|"])
        for item in buffers:
            knowledge = item.get("knowledge") or {}
            lines.append(
                f"| {item.get('size_bytes')} | {item.get('process_name')} | {item.get('buf_name') or ''} | "
                f"{item.get('leak_type') or ''} | {knowledge.get('component') or ''} |"
            )
        lines.append("")

    kernel = result.get("kernel_memory") or {}
    gpu_categories = (kernel.get("gpu") or {}).get("top_categories") or []
    if gpu_categories:
        lines.extend(["## GPU 内存分类", "", "| 类型 | Bytes |", "|---|---:|"])
        for item in gpu_categories:
            lines.append(f"| {item.get('name')} | {item.get('bytes')} |")
        lines.append("")
    slab_caches = (kernel.get("slab") or {}).get("top_caches") or []
    if slab_caches:
        lines.extend(["## Kernel SLAB Top Caches", "", "| Cache | Active Bytes | Objects |", "|---|---:|---:|"])
        for item in slab_caches:
            lines.append(f"| {item.get('name')} | {item.get('active_bytes')} | {item.get('active_objects')} |")
        lines.append("")

    lines.extend(["## 三级根因候选", ""])
    for mode in result.get("fault_mode_matches") or []:
        lines.append(
            f"- **{mode.get('root_cause_l1')} / {mode.get('root_cause_l2')}**: "
            f"{'; '.join(mode.get('root_cause_l3_candidates') or [])}（置信度: {mode.get('confidence')}）"
        )
    if not result.get("fault_mode_matches"):
        lines.append("- 证据不足，无法形成故障模式候选。")

    lines.extend(["", "## 修复与验证方向", ""])
    for advice in result.get("fix_directions") or []:
        lines.append(f"- {advice}")
    lines.extend(["", "## 证据边界", ""])
    for limitation in result.get("limitations") or []:
        lines.append(f"- {limitation}")
    for warning in result.get("warnings") or []:
        lines.append(f"- 警告: {warning}")
    code_search = result.get("code_search") or []
    if code_search:
        lines.extend(["", "## 源码定位", ""])
        for item in code_search:
            lines.append(f"- `{item.get('query')}`")
            for match in (item.get("definitions") or item.get("matches") or [])[:5]:
                if isinstance(match, dict):
                    lines.append(
                        f"  - {match.get('file_path') or match.get('path') or ''}:"
                        f"{match.get('line') or match.get('line_number') or ''}"
                    )
    return "\n".join(lines).rstrip() + "\n"


def handle_native_leak_command(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze HarmonyOS native memory leak evidence")
    parser.add_argument("--input", "--native-leak-dir", dest="input_path", required=True, help="采集包目录或单个文件")
    parser.add_argument("--trace-db", default="", help="trace_streamer 生成的 native_hook SQLite 数据库")
    parser.add_argument("--max-callchains", type=int, default=5)
    parser.add_argument("--min-callchain-percentage", type=float, default=0.0)
    parser.add_argument("--output-dir", default="", help="报告输出目录；默认写入 reports/native_leak_<时间>")
    parser.add_argument("--code-root", action="append", default=[], help="源码目录，可重复指定；用于定位分配函数和成对释放 API")
    parser.add_argument("--json", action="store_true", help="在标准输出打印完整 JSON")
    args = parser.parse_args(argv)
    try:
        result = analyze_native_leak_bundle(
            args.input_path,
            trace_db=args.trace_db,
            max_callchains=max(1, args.max_callchains),
            min_callchain_percentage=max(0.0, args.min_callchain_percentage),
        )
        if args.code_root:
            from tools.repo_search_tool import RepoSearchTool
            search_tool = RepoSearchTool()
            code_search = []
            for query in collect_source_search_queries(result):
                found = search_tool.execute({
                    "code_roots": args.code_root,
                    "mode": "find_symbol",
                    "query": query,
                    "symbol_name": query,
                    "max_matches": 20,
                })
                if isinstance(found, dict) and found.get("success"):
                    found["query"] = query
                    code_search.append(found)
            result["code_search"] = code_search
    except Exception as exc:
        print(f"错误: Native leak 分析失败: {exc}", file=sys.stderr)
        return 1
    stamp = datetime.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    if args.output_dir:
        report_dir = Path(args.output_dir).expanduser().resolve()
    else:
        from cli.report_paths import ensure_reports_migrated, report_root

        ensure_reports_migrated(Path.cwd())
        report_dir = report_root() / f"native_leak_{stamp}"
    report_dir.mkdir(parents=True, exist_ok=True)
    _write_json(report_dir / "00_native_leak_request.json", {
        "input": str(Path(args.input_path).expanduser().resolve()),
        "trace_db": args.trace_db,
        "code_roots": [str(Path(root).expanduser().resolve()) for root in args.code_root],
    })
    _write_json(report_dir / "04d_native_leak_diagnosis.json", result)
    markdown = render_native_leak_report(result)
    (report_dir / "native_leak_report.md").write_text(markdown, encoding="utf-8")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(markdown)
    print(f"报告目录: {report_dir}", file=sys.stderr)
    return 0 if result.get("analyzed") else 2
