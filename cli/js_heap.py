#!/usr/bin/env python3
"""CLI for deterministic JS/ArkTS heap snapshot analysis."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.js_heap.core import analyze_js_heap


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Analyze V8/HarmonyOS .heapsnapshot artifacts")
    parser.add_argument("path")
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--baseline", help="Baseline .heapsnapshot for growth comparison")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = analyze_js_heap(args.path, top_n=args.top_n, baseline=args.baseline)
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0 if result.get("status") == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
