#!/usr/bin/env python3
"""CLI for normalized trace/jank analysis reports."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.jank_analysis.core import analyze_jank_artifact


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Analyze trace analyzer JSON/CSV output")
    parser.add_argument("path")
    parser.add_argument("--mode", choices=("frame", "arkui", "fence", "flutter", "web", "pmu", "completion_latency", "cpu"), default="frame")
    parser.add_argument("--deadline-ms", type=float, default=16.67)
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = analyze_jank_artifact(args.path, mode=args.mode, deadline_ms=args.deadline_ms, top_n=args.top_n)
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0 if result.get("status") == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
