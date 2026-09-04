"""Stage artifact helpers for checkpoint replay and resume."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional


ANALYZE_ARTIFACT_NAME = "stage_analyze_result.json"


def analyze_artifact_path(report_dir: Path) -> Path:
    return Path(report_dir).expanduser().resolve() / "artifacts" / ANALYZE_ARTIFACT_NAME


def save_analyze_artifact(report_dir: Path, payload: Dict[str, Any]) -> Path:
    path = analyze_artifact_path(report_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_analyze_artifact(report_dir: Path) -> Optional[Dict[str, Any]]:
    path = analyze_artifact_path(report_dir)
    if not path.is_file():
        alt = Path(report_dir).expanduser().resolve() / "artifacts" / ANALYZE_ARTIFACT_NAME
        if not alt.is_file():
            return None
        path = alt
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def save_analyze_round_artifact(report_dir: Path, round_index: int, payload: Dict[str, Any]) -> Path:
    root = Path(report_dir).expanduser().resolve() / "artifacts"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"analyze_round_{int(round_index)}.json"
    value = dict(payload or {})
    value.setdefault("schema_version", 2)
    value.setdefault("round", int(round_index))
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def save_decide_artifact(report_dir: Path, payload: Dict[str, Any]) -> Path:
    path = Path(report_dir).expanduser().resolve() / "10_decide.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def save_judge_artifact(report_dir: Path, payload: Dict[str, Any]) -> Path:
    path = Path(report_dir).expanduser().resolve() / "11_judge.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_judge_artifact(report_dir: Path) -> Optional[Dict[str, Any]]:
    path = Path(report_dir).expanduser().resolve() / "11_judge.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def save_feedback_analyze_artifact(report_dir: Path, payload: Dict[str, Any]) -> Path:
    root = Path(report_dir).expanduser().resolve() / "artifacts"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "feedback_analyze_0.json"
    value = dict(payload or {})
    value.setdefault("schema_version", 1)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def save_repair_edit_round_artifact(report_dir: Path, round_index: int, payload: Dict[str, Any]) -> Path:
    root = Path(report_dir).expanduser().resolve() / "artifacts"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"repair_edit_round_{int(round_index)}.json"
    value = dict(payload or {})
    value.setdefault("schema_version", 1)
    value.setdefault("round_index", int(round_index))
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_analyze_round_artifact(report_dir: Path, round_index: int) -> Optional[Dict[str, Any]]:
    path = Path(report_dir).expanduser().resolve() / "artifacts" / f"analyze_round_{int(round_index)}.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def hydrate_problem_from_artifact(problem: Dict[str, Any], artifact: Dict[str, Any]) -> Dict[str, Any]:
    """Merge analyze artifact fields into a replay problem dict."""
    out = dict(problem)
    for key in (
        "parse_result", "resolved_stack", "code_context", "crash_diagnosis",
        "analysis", "final_tip", "agent_rounds", "memory_maps", "status",
    ):
        if key in artifact and artifact.get(key) is not None:
            out[f"_hydrated_{key}"] = artifact.get(key)
    out["_hydrated_analyze"] = artifact
    return out


def hydrate_problem_from_round_artifact(
    problem: Dict[str, Any],
    artifact: Dict[str, Any],
    *,
    round_index: int,
) -> Dict[str, Any]:
    out = hydrate_problem_from_artifact(problem, artifact)
    out["_hydrated_round_index"] = int(round_index)
    if isinstance(artifact.get("analysis"), str):
        out["_hydrated_analysis"] = artifact.get("analysis")
    return out
