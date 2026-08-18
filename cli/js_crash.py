#!/usr/bin/env python3
"""CLI for deterministic JS/ArkTS crash diagnosis from parser report JSON."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.js_crash.core import diagnose_js_crash


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Diagnose HarmonyOS JS/ArkTS crash report JSON")
    parser.add_argument("input", type=Path, help="01_crash_log_parser.json or compatible JSON")
    parser.add_argument("--output", type=Path, help="Write diagnosis JSON to this path")
    args = parser.parse_args(argv)
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    if isinstance(payload.get("parse_result"), dict):
        payload = payload["parse_result"]
    result = diagnose_js_crash(payload)
    output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
