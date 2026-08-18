#!/usr/bin/env python3
"""CLI for unified API/BusinessError diagnosis."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.api_fault.core import diagnose_api_fault


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Diagnose API errors and BusinessError")
    parser.add_argument("input", type=Path, nargs="?")
    parser.add_argument("--error-code")
    parser.add_argument("--error-name")
    parser.add_argument("--message")
    parser.add_argument("--api")
    parser.add_argument("--module")
    parser.add_argument("--raw-log", type=Path)
    parser.add_argument("--project-root")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    payload = json.loads(args.input.read_text(encoding="utf-8")) if args.input else {}
    payload.update({key: value for key, value in {"error_code": args.error_code, "error_name": args.error_name, "message": args.message, "api": args.api, "module": args.module, "project_root": args.project_root}.items() if value})
    if args.raw_log:
        payload["raw_log"] = args.raw_log.read_text(encoding="utf-8")
    result = diagnose_api_fault(payload, project_root=args.project_root)
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
