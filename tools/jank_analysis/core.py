#!/usr/bin/env python3
"""Normalize existing trace-analyzer output into stable jank evidence."""
from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


def classify_trace_artifact(path: str) -> Dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {"artifact_type": "missing", "path": str(target), "confidence": 1.0}
    if target.is_dir():
        files = sorted(p for p in target.iterdir() if p.is_file())
        traces = [p for p in files if p.suffix.lower() in {".htrace", ".trace", ".ftrace", ".pb"}]
        reports = [p for p in files if p.suffix.lower() in {".json", ".csv"}]
        kind = "trace" if traces else ("analysis_report" if reports else "directory")
        return {"artifact_type": kind, "path": str(target), "files": [str(p) for p in files], "trace_count": len(traces), "report_count": len(reports), "confidence": 0.95}
    suffix = target.suffix.lower()
    if suffix in {".htrace", ".trace", ".ftrace", ".pb"}:
        kind = "trace"
    elif suffix in {".json", ".csv"}:
        kind = "analysis_report"
    else:
        kind = "unknown"
    return {"artifact_type": kind, "path": str(target), "confidence": 0.95 if kind != "unknown" else 0.3}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def _boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "jank", "dropped"}


def _value(row: Mapping[str, Any], *names: str) -> Any:
    lowered = {str(key).lower().replace(" ", "_"): value for key, value in row.items()}
    for name in names:
        value = row.get(name, lowered.get(name.lower().replace(" ", "_")))
        if value not in (None, ""):
            return value
    return None


def _load_rows(path: Path) -> List[Dict[str, Any]]:
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        for key in ("frames", "jank_events", "events", "rows", "data"):
            if isinstance(data.get(key), list):
                return [item for item in data[key] if isinstance(item, dict)]
        return [data] if isinstance(data, dict) else []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _fault_modes(frame: Mapping[str, Any]) -> List[Dict[str, Any]]:
    text = " ".join(str(frame.get(key) or "") for key in ("stage", "function", "category", "reason", "name")).lower()
    rules = [
        ("JANK-FM-01", "主线程业务函数耗时过长", ("main", "ui", "business", "handler"), "Application"),
        ("JANK-FM-02", "ArkUI Build 阶段耗时", ("build", "component"), "ArkUI"),
        ("JANK-FM-03", "Layout/Measure 阶段耗时", ("layout", "measure"), "ArkUI"),
        ("JANK-FM-04", "Render/Draw 阶段耗时", ("render", "draw", "paint"), "Render"),
        ("JANK-FM-05", "Fence 或 Display 阻塞", ("fence", "display", "vsync"), "System"),
        ("JANK-FM-06", "GC 暂停", ("gc", "garbage collection"), "Runtime"),
        ("JANK-FM-07", "I/O 等待", ("io", "i/o", "disk", "file"), "Application"),
        ("JANK-FM-08", "线程调度或 CPU 抢占", ("sched", "preempt", "runnable", "cpu"), "System"),
    ]
    result = []
    for ident, name, hints, owner in rules:
        if any(hint in text for hint in hints):
            result.append({"id": ident, "name": name, "owner": owner, "confidence": 0.78, "evidence": [f"matched={hint}" for hint in hints if hint in text]})
    return result


def _normalize_frame(row: Mapping[str, Any], index: int, deadline_ms: float) -> Dict[str, Any]:
    start = _number(_value(row, "start_ns", "start", "ts", "timestamp"))
    end = _number(_value(row, "end_ns", "end"))
    duration = _number(_value(row, "duration_ms", "duration", "frame_duration_ms"))
    if duration <= 0 and end > start:
        duration = (end - start) / (1_000_000 if max(start, end) > 100_000 else 1)
    dropped = _boolean(_value(row, "dropped", "is_jank", "jank")) or duration > deadline_ms
    modes = _fault_modes(row)
    function = _value(row, "function", "name")
    process_name = _value(row, "process", "process_name")
    item = {
        "frame_id": str(_value(row, "frame_id", "id", "frame") or f"frame-{index}"),
        "start_ns": int(start) if start else None,
        "end_ns": int(end) if end else None,
        "duration_ms": round(duration, 3),
        "deadline_ms": deadline_ms,
        "dropped": dropped,
        "thread_name": _value(row, "thread", "thread_name"),
        "process_name": process_name,
        "stage": _value(row, "stage", "phase", "category"),
        "function": function,
        "fault_modes": modes,
        "joint_root_cause": {
            "level_2": modes[0]["name"] if modes else (str(_value(row, "stage", "phase", "category") or "unknown stage")),
            "level_3": str(function or process_name or _value(row, "thread", "thread_name") or "unknown component"),
        },
    }
    return item


def _cpu_state_stats(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, float]]:
    aliases = {
        "running": "running", "run": "running", "runnable": "runnable", "ready": "runnable",
        "sleeping": "sleeping", "sleep": "sleeping", "blocked": "blocked", "block": "blocked",
        "io_wait": "io_wait", "i/o wait": "io_wait", "iowait": "io_wait", "preempted": "preempted", "preempt": "preempted",
    }
    result: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in rows:
        thread = str(_value(row, "thread", "thread_name") or "unknown")
        state = aliases.get(str(_value(row, "state", "status", "sched_state") or "").strip().lower())
        if not state:
            continue
        duration = _number(_value(row, "duration_ms", "duration", "duration_ns"))
        if str(_value(row, "duration_ns") or "").strip() and not _value(row, "duration_ms", "duration"):
            duration /= 1_000_000
        result[thread][state] += duration
    return {thread: {state: round(value, 3) for state, value in states.items()} for thread, states in result.items()}


def analyze_jank_artifact(path: str, *, mode: str = "frame", deadline_ms: float = 16.67, top_n: int = 20) -> Dict[str, Any]:
    classification = classify_trace_artifact(path)
    target = Path(path)
    if classification["artifact_type"] == "missing":
        return {"status": "error", "classification": classification, "message": "trace artifact does not exist"}
    if classification["artifact_type"] == "trace":
        return {"status": "unsupported", "classification": classification, "message": "binary trace requires a configured external trace analyzer adapter", "mode": mode}
    files = [target] if target.is_file() else [Path(value) for value in classification.get("files", [])]
    reports = [file for file in files if file.suffix.lower() in {".json", ".csv"}]
    if not reports:
        return {"status": "insufficient_evidence", "classification": classification, "message": "no JSON/CSV analysis report found"}
    rows: List[Dict[str, Any]] = []
    for report in reports:
        rows.extend(_load_rows(report))
    frame_rows = [row for row in rows if not _value(row, "state", "status", "sched_state")]
    frames = [_normalize_frame(row, index, deadline_ms) for index, row in enumerate(frame_rows)]
    frames.sort(key=lambda item: (-item["duration_ms"], item["frame_id"]))
    jank_events = [item for item in frames if item["dropped"]][:max(1, int(top_n))]
    thread_stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"frame_count": 0, "jank_count": 0, "total_duration_ms": 0.0, "max_duration_ms": 0.0})
    for frame in frames:
        name = str(frame.get("thread_name") or "unknown")
        stat = thread_stats[name]
        stat["frame_count"] += 1
        stat["jank_count"] += int(frame["dropped"])
        stat["total_duration_ms"] += frame["duration_ms"]
        stat["max_duration_ms"] = max(stat["max_duration_ms"], frame["duration_ms"])
    fault_modes: List[Dict[str, Any]] = []
    seen = set()
    for frame in jank_events:
        for mode_item in frame["fault_modes"]:
            if mode_item["id"] not in seen:
                seen.add(mode_item["id"])
                fault_modes.append(mode_item)
    completion = _completion_latency(rows, mode)
    status = "success" if frames else "insufficient_evidence"
    framework = mode if mode in {"arkui", "flutter", "web", "pmu", "cpu"} else "frame"
    joint = [item["joint_root_cause"] for item in jank_events if item.get("joint_root_cause")]
    return {
        "status": status,
        "mode": mode,
        "framework": framework,
        "classification": classification,
        "summary": {"frame_count": len(frames), "jank_count": len(jank_events), "jank_rate": round(len(jank_events) / len(frames), 4) if frames else 0.0, "deadline_ms": deadline_ms},
        "frames": frames[:max(1, int(top_n))],
        "jank_events": jank_events,
        "thread_stats": dict(thread_stats),
        "cpu_state_stats": _cpu_state_stats(rows),
        "fault_modes": fault_modes,
        "joint_root_causes": joint,
        "completion_latency": completion,
    }


def _completion_latency(rows: Sequence[Mapping[str, Any]], mode: str) -> Dict[str, Any]:
    if mode != "completion_latency":
        return {"status": "not_requested"}
    starts = [row for row in rows if str(_value(row, "event", "tag", "name") or "").lower() in {"touch", "input", "start"}]
    ends = [row for row in rows if str(_value(row, "event", "tag", "name") or "").lower() in {"complete", "completed", "end"}]
    if not starts or not ends:
        return {"status": "insufficient_evidence", "reason": "未检测到触摸开始或完成事件", "suggestion": "请使用 --completion-latency-tags 指定开始和结束标记"}
    start = _number(_value(starts[0], "ts", "timestamp", "start"))
    end = _number(_value(ends[0], "ts", "timestamp", "end"))
    delta = end - start
    if max(start, end) > 100_000:
        delta /= 1_000_000
    return {"status": "success", "start": start, "end": end, "completion_latency_ms": round(delta, 3)}
