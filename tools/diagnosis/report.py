#!/usr/bin/env python3
"""Machine-friendly report manifest helpers."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .models import normalize_diagnosis_result
from .repair_gate import evaluate_repair_gate


def build_report_manifest(report_dir: Path, *, request: Optional[Mapping[str, Any]] = None, result: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    artifacts = {}
    if report_dir.is_dir():
        for path in sorted(report_dir.iterdir()):
            if path.is_file() and path.name != "report_manifest.json" and path.suffix.lower() == ".json":
                artifacts[path.stem] = path.name
    normalized = normalize_diagnosis_result(result or {}, domain=str((result or {}).get("problem_type") or "unknown")) if result else {}
    gate = evaluate_repair_gate(normalized) if normalized else None
    return {"schema_version": "1.0", "report_dir": str(report_dir), "request": dict(request or {}), "artifacts": artifacts, "diagnosis": normalized, "repair_gate": gate.to_dict() if gate else None, "generated_by": "stability-analysis-agent"}


def write_report_manifest(report_dir: Path, *, request: Optional[Mapping[str, Any]] = None, result: Optional[Mapping[str, Any]] = None) -> Path:
    path = report_dir / "report_manifest.json"
    path.write_text(json.dumps(build_report_manifest(report_dir, request=request, result=result), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
