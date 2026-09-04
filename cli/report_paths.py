#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分析报告（``reports/``）与本地缓存的路径与占用工具。

提供设置菜单「清理本地缓存」所依赖的所有纯函数：

- :func:`report_root`             返回当前工作目录下报告根（``reports/``）
- :func:`format_bytes`            字节数 -> 人类可读字符串
- :func:`summarize_cli_reports`   统计报告目录下报告数与占用（历史函数名，兼容保留）
- :func:`clear_cli_reports`       清空报告目录中的内容
- :func:`print_cli_reports_overview`  控制台概览输出（设置菜单调用）

报告根目录解析顺序：环境变量 ``STABILITY_AGENT_REPORT_DIR``，否则当前工作目录下的 ``./reports``。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

REPORTS_DIR_NAME = "reports"
__all__ = [
    "REPORTS_DIR_NAME",
    "report_root",
    "format_bytes",
    "summarize_cli_reports",
    "summarize_reports",
    "clear_cli_reports",
    "clear_reports",
    "print_cli_reports_overview",
    "print_reports_overview",
]


def report_root() -> Path:
    """解析报告根目录（默认 ``./reports``）。"""
    override = os.environ.get("STABILITY_AGENT_REPORT_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path.cwd().resolve() / REPORTS_DIR_NAME).resolve()


def format_bytes(num_bytes: Optional[int]) -> str:
    """字节数格式化为 ``KiB / MiB / GiB``。空值返回 ``"-"``。"""
    if num_bytes is None or num_bytes < 0:
        return "-"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    size = float(num_bytes)
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    if idx == 0:
        return f"{int(size)} {units[idx]}"
    return f"{size:.2f} {units[idx]}"


def _safe_dir_size(directory: Path) -> int:
    """累加计算目录占用字节数；权限错误时静默返回 0。"""
    total = 0
    try:
        for path in directory.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
    except OSError as exc:
        logger.debug("无法读取目录 %s: %s", directory, exc)
        return 0
    return total


def _list_report_dirs(root: Path) -> List[Path]:
    """列出 ``root`` 下所有分析会话目录。"""
    if not root.exists() or not root.is_dir():
        return []
    items: List[Path] = []
    for child in root.iterdir():
        if child.is_dir():
            items.append(child.resolve())
    items.sort(key=lambda p: p.name)
    return items


def _summarize_session(session_dir: Path) -> Dict[str, Any]:
    """汇总单个分析会话目录的元数据。"""
    return {
        "name": session_dir.name,
        "path": str(session_dir),
        "bytes": _safe_dir_size(session_dir),
    }


def summarize_reports(*, preview_limit: int = 5, root: Optional[Path] = None) -> Dict[str, Any]:
    """汇总当前报告目录占用、最近若干会话等元数据。"""
    target = (root or report_root()).resolve()
    sessions = _list_report_dirs(target)
    by_session = [_summarize_session(p) for p in sessions]
    total_bytes = sum(int(item.get("bytes") or 0) for item in by_session)
    preview = by_session[-preview_limit:] if preview_limit > 0 else []
    return {
        "path": str(target),
        "exists": target.exists() and target.is_dir(),
        "report_count": len(by_session),
        "total_bytes": total_bytes,
        "preview": preview,
        "preview_limit": preview_limit,
    }


def clear_reports(
    *,
    root: Optional[Path] = None,
    only_preview: bool = False,
    preview_limit: int = 5,
) -> Dict[str, Any]:
    """清空报告目录下的会话子目录。"""
    target = (root or report_root()).resolve()
    stats = summarize_reports(root=target, preview_limit=preview_limit if preview_limit > 0 else 1)
    targets: List[Path]
    if not stats["exists"]:
        return {
            "path": str(target),
            "removed": 0,
            "freed_bytes": 0,
            "skipped": "目录不存在",
        }
    if only_preview:
        preview_paths = [Path(item["path"]).resolve() for item in stats["preview"]]
        targets = preview_paths
    else:
        targets = [s.resolve() for s in _list_report_dirs(target)]

    freed = 0
    removed = 0
    for item in targets:
        if not item.exists():
            continue
        freed += int(_safe_dir_size(item) or 0)
        try:
            shutil.rmtree(item)
            removed += 1
        except OSError as exc:
            logger.warning("删除 %s 失败: %s", item, exc)
    return {
        "path": str(target),
        "removed": removed,
        "freed_bytes": freed,
        "skipped": "" if removed or not targets else "无匹配目录",
    }


# 兼容旧函数名
summarize_cli_reports = summarize_reports
clear_cli_reports = clear_reports


def print_reports_overview() -> Dict[str, Any]:
    """在设置菜单中打印报告占用概览，返回统计数据便于后续清理确认。"""
    from cli.main import _yellow

    stats = summarize_reports()
    print("")
    print(_yellow(f"分析报告（{REPORTS_DIR_NAME}）"))
    print(f"- 目录: {stats['path']}")
    if not stats["exists"]:
        print("- 状态: 目录不存在（还没有生成过任何报告）")
        print("")
        return stats
    print(f"- 报告数: {stats['report_count']}")
    print(f"- 占用空间: {format_bytes(int(stats['total_bytes'] or 0))}")
    preview = stats.get("preview") or []
    if preview:
        print("- 最近报告:")
        for item in preview:
            name = str(item.get("name") or "")
            size_text = format_bytes(int(item.get("bytes") or 0))
            print(f"  · {name}（{size_text}）")
        extra = int(stats.get("report_count") or 0) - len(preview)
        if extra > 0:
            print(f"  · … 另有 {extra} 份未列出")
    print("")
    print("说明: 清理后将删除上述目录中的全部（或最近 N 份）报告，不可恢复。")
    return stats


print_cli_reports_overview = print_reports_overview
