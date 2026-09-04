#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Batch evaluation runner for harness regression manifests."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.evaluation import evaluate_suite, summarize_matrix


def main() -> int:
    parser = argparse.ArgumentParser(description="Run evaluation matrix from a manifest JSON file")
    parser.add_argument("--manifest", required=True, help="Path to evaluation_manifest.json")
    parser.add_argument("--report-root", default="", help="Override report root for all cases")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--mode", choices=("offline", "llm-replay"), default="offline",
                        help="offline=fixture/report evaluation; llm-replay=reserved for recorded LLM replay manifests")
    parser.add_argument("--output", default="", help="Optional output file path")
    args = parser.parse_args()

    manifest = Path(args.manifest).expanduser().resolve()
    if not manifest.is_file():
        print(f"manifest not found: {manifest}", file=sys.stderr)
        return 2

    report_root = Path(args.report_root).expanduser().resolve() if args.report_root else None
    results = evaluate_suite(manifest, report_root=report_root)
    summary = summarize_matrix(results)

    if args.format == "markdown":
        lines = [
            f"# Evaluation matrix: {manifest.name}",
            "",
            f"- total_cases: {summary['total_cases']}",
            f"- diagnosis_correct: {summary['diagnosis_correct']}",
            f"- repair_valid: {summary['repair_valid']}",
            f"- context_loop_valid_rate_avg: {summary.get('context_loop_valid_rate_avg', 1.0)}",
            f"- decision_match_rate: {summary.get('decision_match_rate')}",
            f"- judge_match_rate: {summary.get('judge_match_rate')}",
            "",
            "| case | category | repair | run_status |",
            "| --- | --- | --- | --- |",
        ]
        for item in summary["cases"]:
            lines.append(
                "| {case_id} | {category} | {repair} | {run_status} |".format(
                    case_id=item.get("case_id"),
                    category=(item.get("diagnosis") or {}).get("category"),
                    repair=(item.get("repair") or {}).get("verification"),
                    run_status=(item.get("repair") or {}).get("run_status"),
                )
            )
        payload = "\n".join(lines) + "\n"
    else:
        payload = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"

    if args.output:
        Path(args.output).expanduser().write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
