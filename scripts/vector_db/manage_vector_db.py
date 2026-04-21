#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
向量数据库管理脚本：导出、导入、清空。

用法（在仓库根目录执行）：
  python3 scripts/vector_db/manage_vector_db.py export
  python3 scripts/vector_db/manage_vector_db.py import /path/to/vector_db_snapshot.json
  python3 scripts/vector_db/manage_vector_db.py clear

可选参数：
  --db-path       运行时向量数据库目录（默认: ./vector_db）
  --output-file   export 输出文件（默认: scripts/vector_db/vector_db_snapshot_latest.json）
  --no-timestamp  export 时不生成带时间戳副本
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SCRIPT_DIR = Path(__file__).resolve().parent


def _get_analyzer(db_path: str):
    try:
        from rag.vector_database_integration import AIStabilityAnalyzerWithVectorDB
    except ImportError:
        from stability_analyzer_agent.rag.vector_database_integration import (  # type: ignore
            AIStabilityAnalyzerWithVectorDB,
        )

    analyzer = AIStabilityAnalyzerWithVectorDB(vector_db_path=db_path)
    if getattr(analyzer, "memory", None) is None:
        raise RuntimeError("向量数据库未初始化（可能缺少依赖或数据库目录无效）")
    return analyzer


def cmd_export(db_path: str, output_file: str, no_timestamp: bool) -> int:
    analyzer = _get_analyzer(db_path)
    snapshot = analyzer.export_snapshot()
    content = json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n"

    latest_path = Path(output_file).resolve()
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.write_text(content, encoding="utf-8")
    print(f"已写入: {latest_path}")

    if not no_timestamp:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        ts_path = latest_path.with_name(f"{latest_path.stem}_{ts}{latest_path.suffix or '.json'}")
        ts_path.write_text(content, encoding="utf-8")
        print(f"已写入: {ts_path}")

    stats = snapshot.get("stats", {})
    print(
        "统计: "
        f"rules={stats.get('rules', 0)}, "
        f"patterns={stats.get('patterns', 0)}, "
        f"evidence={stats.get('evidence', 0)}, "
        f"strategies={stats.get('strategies', 0)}, "
        f"guidance_blocks={stats.get('guidance_blocks', 0)}"
    )
    return 0


def cmd_import(db_path: str, snapshot_path: str) -> int:
    path = Path(snapshot_path)
    if not path.exists():
        print(f"错误: 文件不存在 {path}", file=sys.stderr)
        return 1
    payload = json.loads(path.read_text(encoding="utf-8"))

    analyzer = _get_analyzer(db_path)
    counts = analyzer.import_snapshot(payload)
    print("导入完成:")
    print(json.dumps(counts, indent=2, ensure_ascii=False))
    return 0


def cmd_clear(db_path: str) -> int:
    analyzer = _get_analyzer(db_path)
    analyzer.clear_all()
    print("向量数据库已清空。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="向量数据库管理：导出、导入、清空")
    parser.add_argument(
        "--db-path",
        type=str,
        default=str(PROJECT_ROOT / "vector_db"),
        help="运行时向量数据库目录路径（默认: ./vector_db）",
    )
    parser.add_argument(
        "--output-file",
        type=str,
        default=str(SCRIPT_DIR / "vector_db_snapshot_latest.json"),
        help="export 输出文件路径",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_export = sub.add_parser("export", help="导出向量库到 JSON")
    p_export.add_argument("--no-timestamp", action="store_true", help="不生成带时间戳副本")

    p_import = sub.add_parser("import", help="从 JSON 快照导入（合并）")
    p_import.add_argument("snapshot_file", help="快照 JSON 文件路径")

    sub.add_parser("clear", help="清空向量数据库")

    args = parser.parse_args()
    db_path = args.db_path

    try:
        if args.command == "export":
            return cmd_export(db_path, args.output_file, getattr(args, "no_timestamp", False))
        if args.command == "import":
            return cmd_import(db_path, args.snapshot_file)
        if args.command == "clear":
            return cmd_clear(db_path)
    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
