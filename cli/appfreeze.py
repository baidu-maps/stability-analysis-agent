#!/usr/bin/env python3
"""CLI for evidence-first AppFreeze diagnosis."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.appfreeze.core import analyze_appfreeze


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Diagnose HarmonyOS AppFreeze/ANR report JSON")
    parser.add_argument("input", type=Path)
    parser.add_argument("--raw-content", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    raw = args.raw_content.read_text(encoding="utf-8") if args.raw_content else ""
    result = analyze_appfreeze(payload, raw_content=raw)
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
