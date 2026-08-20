#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build and commit crash-fix cases into the vector memory store from report artifacts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from rag.feature_extractor import build_pattern_query
from rag.vector_store_config import VectorStoreHandle, get_vector_store


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _first_existing(report_dir: Path, names: List[str]) -> Optional[Path]:
    for name in names:
        p = report_dir / name
        if p.is_file():
            return p
    return None


def _applied_files_summary(apply_fix: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for item in apply_fix.get("applied") or []:
        if not isinstance(item, dict):
            continue
        if item.get("status") == "applied" and item.get("file"):
            out.append(str(item.get("file")))
    return out


def build_case_record_from_report(report_dir: Path) -> Optional[Dict[str, Any]]:
    """Build pattern + evidence payload from a finished report directory."""
    report_dir = Path(report_dir).expanduser().resolve()
    apply_path = _first_existing(
        report_dir,
        ["08_apply_ai_fixes.json", "07_apply_ai_fixes.json"],
    )
    apply_fix = _read_json(apply_path) if apply_path else None
    if not apply_fix or not apply_fix.get("success"):
        return None

    parse_path = _first_existing(report_dir, ["01_crash_log_parser.json"])
    resolved_path = _first_existing(
        report_dir,
        ["03_add2line_resolver.json", "02_add2line_resolver.json"],
    )
    code_path = _first_existing(
        report_dir,
        ["04b_code_content_provider.json", "03_code_content_provider.json"],
    )
    if parse_path is None or resolved_path is None:
        return None

    parsed_data = _read_json(parse_path) or {}
    resolved_data = _read_json(resolved_path) or {}
    code_context = _read_json(code_path) if code_path else {}
    prompt_data: Dict[str, Any] = dict(code_context or {})

    from tools.resolve_stack_errors import flatten_resolved_frames_from_stack

    frames = flatten_resolved_frames_from_stack(resolved_data)
    if not frames:
        return None

    ai_analysis = ""
    for candidate in (
        report_dir / "final_output.md",
        report_dir / "round_0" / "06_ai_gen_res.md",
    ):
        if candidate.is_file():
            ai_analysis = candidate.read_text(encoding="utf-8", errors="ignore").strip()
            if ai_analysis:
                break

    invalid_patterns = [
        "AI分析跳过",
        "跳过AI分析",
        "AI分析失败",
        "分析失败",
        "未配置密钥",
        "只运行了前三个工具",
    ]
    if ai_analysis and any(p in ai_analysis for p in invalid_patterns):
        ai_analysis = ""

    crash_info = parsed_data.get("crash_info", {}) if isinstance(parsed_data, dict) else {}
    meta_info = parsed_data.get("meta_info", {}) if isinstance(parsed_data, dict) else {}
    crash_reason = crash_info.get("crash_reason", "unknown") if isinstance(crash_info, dict) else "unknown"
    signal = crash_info.get("signal", "") if isinstance(crash_info, dict) else ""
    os_type = meta_info.get("os_type", "unknown") if isinstance(meta_info, dict) else "unknown"
    platform = meta_info.get("platform") if isinstance(meta_info, dict) else None

    _query_text, signature = build_pattern_query(parsed_data, resolved_data, prompt_data)
    pattern_summary = (
        prompt_data.get("crash_summary")
        or (ai_analysis.splitlines()[0] if ai_analysis else "")
        or signature
        or _query_text[:200]
    )
    pattern_summary = str(pattern_summary).strip()

    crash_category = "unknown"
    if "SIGSEGV" in str(signal) or "segmentation" in str(crash_reason).lower():
        crash_category = "memory"
    elif "deadlock" in str(crash_reason).lower() or "lock" in str(crash_reason).lower():
        crash_category = "concurrency"

    evidence_requirements: List[str] = ["stack_trace"]
    if prompt_data.get("code_contexts") or prompt_data.get("contexts"):
        evidence_requirements.append("code_snippet")
    if parsed_data.get("raw_content"):
        evidence_requirements.append("log_fragment")
    evidence_requirements.append("fix_applied")

    pattern_id = (
        "pattern_"
        + hashlib.md5((pattern_summary + signature + datetime.now().isoformat()).encode()).hexdigest()
    )
    pattern = {
        "pattern_id": pattern_id,
        "pattern_summary": pattern_summary,
        "crash_signature": signature,
        "platform_scope": {"os": os_type, "platform": platform},
        "crash_category": crash_category,
        "evidence_requirements": evidence_requirements,
        "confidence_score": 0.6,
        "validation_state": "draft",
        "source_type": "internal_case",
        "created_at": datetime.now().isoformat(),
        "fix_files": _applied_files_summary(apply_fix),
        "report_dir": str(report_dir),
    }

    evidence_list: List[Dict[str, Any]] = []
    for frame in frames[:5]:
        evidence_list.append(
            {
                "evidence_id": f"evidence_{hashlib.md5((pattern_id + str(frame)).encode()).hexdigest()}",
                "pattern_id": pattern_id,
                "evidence_type": "stack_trace",
                "raw_content": json.dumps(frame, ensure_ascii=False),
                "normalized_features": {
                    "function": frame.get("function"),
                    "module": frame.get("module"),
                },
                "reliability_score": 0.7,
                "created_at": datetime.now().isoformat(),
            }
        )
    if apply_fix:
        evidence_list.append(
            {
                "evidence_id": f"evidence_{hashlib.md5((pattern_id + 'apply_fix').encode()).hexdigest()}",
                "pattern_id": pattern_id,
                "evidence_type": "fix_applied",
                "raw_content": json.dumps(apply_fix, ensure_ascii=False)[:8000],
                "normalized_features": {"files": pattern.get("fix_files") or []},
                "reliability_score": 0.8,
                "created_at": datetime.now().isoformat(),
            }
        )

    return {
        "pattern": pattern,
        "evidence": evidence_list,
        "summary": {
            "signal": signal,
            "crash_reason": crash_reason,
            "top_frame": frames[0] if frames else {},
            "fix_files": pattern.get("fix_files") or [],
        },
    }


def commit_case_record(record: Dict[str, Any], store: VectorStoreHandle) -> Dict[str, Any]:
    if store.analyzer is None:
        return {"ok": False, "error": "vector store analyzer not initialized"}
    pattern = record.get("pattern")
    if not isinstance(pattern, dict):
        return {"ok": False, "error": "missing pattern"}
    evidence_list = record.get("evidence") or []
    ok = bool(store.analyzer.add_pattern(pattern))
    if ok:
        for ev in evidence_list:
            if isinstance(ev, dict):
                store.analyzer.add_evidence(ev)
    return {
        "ok": ok,
        "pattern_id": pattern.get("pattern_id"),
        "vector_db_path": store.local_path,
    }


def commit_from_report_dir(
    report_dir: Path,
    *,
    vector_db_path: Optional[str] = None,
    vector_db_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    record = build_case_record_from_report(report_dir)
    if record is None:
        return {
            "ok": False,
            "skipped": True,
            "skipped_reason": "report not eligible (missing parse/symbolize or apply fix failed)",
        }
    try:
        store = get_vector_store(vector_db_config, cli_path=vector_db_path)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    result = commit_case_record(record, store)
    result["summary"] = record.get("summary")
    return result


def write_commit_audit(report_dir: Path, payload: Dict[str, Any]) -> Path:
    report_dir = Path(report_dir).expanduser().resolve()
    out = report_dir / "09_vector_db_commit.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out
