#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对比 stability_trials 目录下多轮 AI 改码结果。"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_fix_plan(tdir: Path) -> Optional[Dict[str, Any]]:
    """从合并后的 07b_fix_extract_debug.json.json 读取 fix_plan；兼容旧 trial 的 06_fix_plan_debug.json。"""
    extract = _load_json(tdir / "07b_fix_extract_debug.json.json")
    if isinstance(extract, dict):
        plan = extract.get("fix_plan")
        if isinstance(plan, dict):
            return plan
    legacy = _load_json(tdir / "06_fix_plan_debug.json")
    return legacy if isinstance(legacy, dict) else None


def _normalize_edit_key(ed: Dict[str, Any]) -> str:
    f = str(ed.get("file") or "")
    sig = str(ed.get("function_signature") or "")[:120]
    return f"{Path(f).name}|{sig}"


def _fingerprint_plan(plan: Optional[Dict[str, Any]]) -> str:
    if not plan:
        return ""
    parts: List[str] = []
    for ed in plan.get("edits") or []:
        if not isinstance(ed, dict):
            continue
        code = str(ed.get("replacement_code") or "")
        norm = re.sub(r"\s+", " ", code).strip()
        parts.append(_normalize_edit_key(ed) + "|" + hashlib.sha256(norm.encode()).hexdigest()[:16])
    return ";".join(sorted(parts))


def _summarize_apply(apply: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not apply:
        return {"success": None, "applied": [], "skipped_reason": None}
    applied = []
    for item in apply.get("applied") or []:
        if not isinstance(item, dict):
            continue
        applied.append(
            {
                "file": Path(str(item.get("file") or "")).name,
                "signature": str(item.get("function_signature") or "")[:80],
                "status": item.get("status"),
                "error": str(item.get("error") or "")[:120] or None,
            }
        )
    return {
        "success": apply.get("success"),
        "skipped_reason": apply.get("skipped_reason"),
        "error": (str(apply.get("error") or "")[:200] or None),
        "summary": str(apply.get("summary") or "")[:120],
        "applied": applied,
    }


def main() -> int:
    base = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    trials: List[Dict[str, Any]] = []
    for i in range(1, 4):
        tdir = base / f"trial_{i}"
        apply = _load_json(tdir / "07_apply_ai_fixes.json")
        plan = _load_fix_plan(tdir)
        report = (tdir / "report_dir.txt").read_text(encoding="utf-8").strip() if (tdir / "report_dir.txt").is_file() else ""
        trials.append(
            {
                "trial": i,
                "report": report,
                "apply": _summarize_apply(apply),
                "plan_fingerprint": _fingerprint_plan(plan),
                "plan_edit_count": len(plan.get("edits") or []) if plan else 0,
                "plan_files": sorted(
                    {
                        Path(str(ed.get("file") or "")).name
                        for ed in (plan.get("edits") or [])
                        if isinstance(ed, dict)
                    }
                ),
            }
        )

    fp_set = {t["plan_fingerprint"] for t in trials if t["plan_fingerprint"]}
    success_vals = [t["apply"]["success"] for t in trials]
    stable_fp = len(fp_set) == 1 and all(trials[0]["plan_fingerprint"] for _ in trials)
    stable_success = len(set(success_vals)) == 1

    lines = [
        "# AI 改码稳定性对比（3 轮）",
        "",
        f"- **fix_plan 指纹一致**: {'是' if stable_fp else '否'}（{len(fp_set)} 种不同方案）",
        f"- **apply success 一致**: {'是' if stable_success else '否'} → {success_vals}",
        "",
    ]
    for t in trials:
        lines.append(f"## 第 {t['trial']} 轮")
        lines.append(f"- 报告: `{t['report']}`")
        lines.append(f"- plan 编辑数: {t['plan_edit_count']}, 涉及文件: {', '.join(t['plan_files']) or '无'}")
        lines.append(f"- plan 指纹: `{t['plan_fingerprint'][:80]}...`" if t['plan_fingerprint'] else "- plan 指纹: (空)")
        a = t["apply"]
        lines.append(f"- **success**: `{a['success']}`" + (f", reason=`{a['skipped_reason']}`" if a.get("skipped_reason") else ""))
        if a.get("error"):
            lines.append(f"- error: {a['error']}")
        lines.append("")
        lines.append("| 文件 | 函数 | status | error |")
        lines.append("|------|------|--------|-------|")
        for row in a["applied"]:
            lines.append(
                f"| {row['file']} | {row['signature']} | {row['status']} | {row['error'] or ''} |"
            )
        lines.append("")

    if not stable_fp:
        lines.append("## 差异说明")
        lines.append("各轮 `07b_fix_extract_debug.json.json` 中 fix_plan 的 replacement 哈希不同，LLM 输出存在波动。")
    if stable_fp and stable_success:
        lines.append("## 结论")
        lines.append("三轮 fix_plan 与 apply 结果一致，当前链路下 AI 改码方案**稳定**。")
    elif stable_success and not stable_fp:
        lines.append("## 结论")
        lines.append("最终 apply 门禁结果一致，但 LLM 生成的补丁正文仍有差异（可能被同一规则拒绝/通过）。")
    else:
        lines.append("## 结论")
        lines.append("apply 结果不一致，需继续收紧 prompt/门禁或降低 LLM 温度。")

    out = base / "summary.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(out.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
