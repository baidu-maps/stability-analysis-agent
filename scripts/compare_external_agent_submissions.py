#!/usr/bin/env python3
"""Build comparison_report.json/.md after external-agent submissions are added."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from services.external_agent_evaluation import build_external_agent_comparison


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report_dir", help="Crash Agent report directory")
    args = parser.parse_args()
    result = build_external_agent_comparison(Path(args.report_dir))
    print(json.dumps({"submission_count": len(result["external_submissions"]),
                      "comparison": str(Path(args.report_dir).resolve() / "external_agent_evaluation" / "comparison_report.md")},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
