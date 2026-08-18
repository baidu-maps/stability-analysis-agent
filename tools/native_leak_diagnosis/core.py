#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic HarmonyOS/OpenHarmony native leak analysis.

This module absorbs the useful analysis model from Huawei DFX's native leak
skill while exposing JSON-safe, daemon-friendly functions.  It intentionally
does not deserialize pickle/dill files and opens trace databases read-only.
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

from .knowledge import LEAK_FIX_DIRECTIONS, classify_dma_label


_BUNDLE_PATTERNS = {
    "sample": re.compile(r"memleak-native-(?P<process>[0-9A-Za-z_.-]+)-(?P<pid>\d+)-sample\.txt$"),
    "smaps": re.compile(r"memleak-native-(?P<process>[0-9A-Za-z_.-]+)-(?P<pid>\d+)-smaps\.txt$"),
    "profile": re.compile(r"memleak-native-(?P<process>[0-9A-Za-z_.-]+)-(?P<pid>\d+)-(?P<ts>\d+)\.(?:txt|db)$"),
    "kernel": re.compile(r"memleak-kernel-(?P<process>[0-9A-Za-z_.-]+)-0-(?P<ts>\d+)\.txt$"),
}

_SAMPLE_COLUMNS = {
    "RSS(KB)": "rss_kb",
    "PSS(KB)": "pss_kb",
    "SwapPSS(KB)": "swap_pss_kb",
    "TotalPSS(KB)": "total_pss_kb",
    "ION(KB)": "dma_kb",
    "DMA(KB)": "dma_kb",
    "GPU(KB)": "gpu_kb",
    "TotalMem(KB)": "total_mem_kb",
    "RunningTime(s)": "running_time_s",
    "Realtime": "realtime",
}

_TRACE_EVENT_TYPES = {
    "malloc": "AllocEvent",
    "mmap": "MmapEvent",
    "js_heap": "JS_Alloc",
    "arkts_heap": "ARKTS_Alloc",
    "dart_heap": "DART_HEAP_Alloc",
    "kmp_heap": "KMP_Alloc",
    "so": "SO_Alloc",
    "fd": "FD_Open",
    "thread": "Thread_Create",
    "gpu_vk": "GPU_VK_Alloc",
    "gpu_gles": "GPU_GLES_Alloc",
}

_TRACE_TYPE_IDS = {
    0: "AllocEvent",
    1: "MmapEvent",
    3: "ARKTS_Alloc",
    4: "KMP_Alloc",
    5: "JS_Alloc",
    6: "DART_HEAP_Alloc",
    7: "RN_HERMES_HEAP_Alloc",
    8: "ARK_GLOBAL_HANDLE_Alloc",
    9: "ARK_LOCAL_HANDLE_Alloc",
    10: "SO_Alloc",
}

_RUNTIME_FRAME_RE = re.compile(
    r"(?:^|::)(?:malloc|calloc|realloc|operator new|mmap)$|libc(?:\+|\.so)|libclang_rt",
    re.IGNORECASE,
)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _number(value: Any) -> Optional[float]:
    text = str(value or "").strip().replace("*", "").replace(",", "")
    if not text or text in {"~", "-", "N/A", "null", "NULL"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _int_number(value: Any) -> int:
    number = _number(value)
    return int(number) if number is not None else 0


def discover_native_leak_bundle(path: str) -> Dict[str, Any]:
    """Discover related sample/smaps/profile/kernel files without overwriting siblings."""
    root = Path(path).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"native leak input does not exist: {root}")
    files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
    bundles: Dict[Tuple[str, str], Dict[str, Any]] = {}
    kernels: Dict[str, List[str]] = defaultdict(list)
    unrecognized: List[str] = []
    for file_path in files:
        matched = False
        for kind, pattern in _BUNDLE_PATTERNS.items():
            match = pattern.search(file_path.name)
            if not match:
                continue
            matched = True
            process = match.group("process")
            if kind == "kernel":
                kernels[process].append(str(file_path))
                break
            pid = match.group("pid")
            bundle = bundles.setdefault(
                (process, pid),
                {"process_name": process, "pid": pid, "sample": [], "smaps": [], "profile": [], "kernel": []},
            )
            bundle[kind].append(str(file_path))
            break
        if not matched and file_path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
            unrecognized.append(str(file_path))

    for (process, _pid), bundle in bundles.items():
        bundle["kernel"] = sorted(kernels.get(process, []))
        for kind in ("sample", "smaps", "profile"):
            bundle[kind] = sorted(bundle[kind])

    ordered = sorted(
        bundles.values(),
        key=lambda item: (
            int(bool(item["sample"])) + int(bool(item["smaps"])) + int(bool(item["profile"])),
            len(item["profile"]),
        ),
        reverse=True,
    )
    standalone_kernel = sorted(path for values in kernels.values() for path in values)
    return {
        "root": str(root),
        "scenario": "managed" if ordered else ("kernel_only" if standalone_kernel else "unknown"),
        "bundles": ordered,
        "selected": ordered[0] if ordered else {
            "process_name": next(iter(kernels), ""),
            "pid": "0",
            "sample": [],
            "smaps": [],
            "profile": [],
            "kernel": standalone_kernel,
        },
        "unrecognized_databases": unrecognized,
        "warnings": [] if ordered or standalone_kernel else ["No recognized native leak files were found."],
    }


def _parse_datetime(text: str) -> Optional[datetime]:
    cleaned = (text or "").strip().replace("*", "")
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d_%H-%M-%S",
        "%m-%d %H:%M:%S.%f",
    ):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


def _elapsed_seconds(rows: Sequence[Dict[str, Any]], index: int) -> Optional[float]:
    row = rows[index]
    running = _number(row.get("running_time_s"))
    if running is not None:
        return running
    dt = _parse_datetime(str(row.get("realtime") or ""))
    if dt is None:
        return None
    start = _parse_datetime(str(rows[0].get("realtime") or ""))
    return (dt - start).total_seconds() if start else None


def _linear_r_squared(values: Sequence[float]) -> float:
    if len(values) < 3:
        return 0.0
    xs = list(range(len(values)))
    x_mean = sum(xs) / len(xs)
    y_mean = sum(values) / len(values)
    ss_x = sum((x - x_mean) ** 2 for x in xs)
    ss_y = sum((y - y_mean) ** 2 for y in values)
    if ss_x <= 0 or ss_y <= 0:
        return 0.0
    covariance = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, values))
    return max(0.0, min(1.0, covariance * covariance / (ss_x * ss_y)))


def _analyze_metric_trend(rows: Sequence[Dict[str, Any]], metric: str) -> Optional[Dict[str, Any]]:
    points: List[Tuple[int, float]] = []
    for index, row in enumerate(rows):
        value = _number(row.get(metric))
        if value is not None:
            points.append((index, value))
    if not points:
        return None
    values = [value for _, value in points]
    growth = values[-1] - values[0]
    growth_pct = (growth / values[0] * 100.0) if values[0] else None
    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
    positive = [delta for delta in deltas if delta > 0]
    fastest: Optional[Dict[str, Any]] = None
    if deltas:
        fastest_i = max(range(len(deltas)), key=lambda i: deltas[i])
        row_a = rows[points[fastest_i][0]]
        row_b = rows[points[fastest_i + 1][0]]
        fastest = {
            "delta_kb": deltas[fastest_i],
            "from": row_a.get("realtime") or row_a.get("running_time_s"),
            "to": row_b.get("realtime") or row_b.get("running_time_s"),
        }

    duration_s = None
    if len(points) >= 2:
        start_s = _elapsed_seconds(rows, points[0][0])
        end_s = _elapsed_seconds(rows, points[-1][0])
        if start_s is not None and end_s is not None and end_s > start_s:
            duration_s = end_s - start_s
    rate_kb_min = growth / duration_s * 60.0 if duration_s else None

    if len(values) < 3:
        shape = "insufficient_samples"
    elif growth <= 0:
        shape = "stable_or_decreasing"
    else:
        near_zero = sum(1 for delta in deltas if abs(delta) <= max(1.0, abs(growth) * 0.02))
        max_delta = max(positive) if positive else 0.0
        if max_delta >= growth * 0.6:
            shape = "sudden_jump"
        elif near_zero >= max(1, math.ceil(len(deltas) * 0.5)) and len(positive) >= 1:
            shape = "stepwise_growth"
        elif _linear_r_squared(values) >= 0.85 and sum(1 for d in deltas if d >= 0) >= len(deltas) * 0.8:
            shape = "continuous_linear_growth"
        else:
            shape = "fluctuating_growth"
    return {
        "unit": "KB",
        "samples": len(values),
        "start_kb": values[0],
        "end_kb": values[-1],
        "peak_kb": max(values),
        "growth_kb": growth,
        "growth_percent": round(growth_pct, 2) if growth_pct is not None else None,
        "growth_rate_kb_per_min": round(rate_kb_min, 2) if rate_kb_min is not None else None,
        "trend": shape,
        "linear_r_squared": round(_linear_r_squared(values), 3),
        "fastest_interval": fastest,
    }


def parse_sample_file(path: str) -> Dict[str, Any]:
    file_path = Path(path).expanduser().resolve()
    lines = _read_text(file_path).splitlines()
    process = ""
    pid = ""
    threshold_kb: Optional[float] = None
    for line in lines:
        match = re.search(r"processName:\s*([^\s]+)", line)
        if match:
            process = match.group(1)
        match = re.search(r"pid:\s*(\d+)", line)
        if match:
            pid = match.group(1)
        match = re.search(r"SoftThreshold:\s*([\d.]+)\((\w+)\)", line, re.IGNORECASE)
        if match:
            threshold_kb = float(match.group(1)) * (1024.0 if match.group(2).upper() == "MB" else 1.0)

    header_index = -1
    column_names: List[str] = []
    for index, line in enumerate(lines):
        if "RSS(KB)" in line and "PSS(KB)" in line:
            header_index = index
            normalized = line.replace("Running Time", "RunningTime")
            column_names = re.split(r"\s+", normalized.strip())
            break
    rows: List[Dict[str, Any]] = []
    warnings: List[str] = []
    if header_index < 0:
        warnings.append("Sample table header was not found.")
    else:
        mapped = [
            (
                idx,
                _SAMPLE_COLUMNS.get(
                    "RunningTime(s)" if name in {"RunningTime", "RunningTime(s)"} else name
                ),
            )
            for idx, name in enumerate(column_names)
        ]
        for line in lines[header_index + 1:]:
            if not line.strip() or re.match(r"^[*=\-]{3,}", line.strip()):
                continue
            values = re.split(r"\s{2,}", line.strip())
            if len(values) < 3:
                values = re.split(r"\s+", line.strip())
            if not values or _number(values[0]) is None:
                continue
            row: Dict[str, Any] = {"is_trigger": "*" in line}
            for idx, field in mapped:
                if not field or idx >= len(values):
                    continue
                raw = values[idx].replace("*", "")
                row[field] = raw if field == "realtime" else _number(raw)
            rows.append(row)

    trends = {
        metric: trend
        for metric in ("total_pss_kb", "dma_kb", "gpu_kb", "total_mem_kb")
        if (trend := _analyze_metric_trend(rows, metric)) is not None
    }
    growths = {
        "pss": max(0.0, float((trends.get("total_pss_kb") or {}).get("growth_kb") or 0.0)),
        "dma": max(0.0, float((trends.get("dma_kb") or {}).get("growth_kb") or 0.0)),
        "gpu": max(0.0, float((trends.get("gpu_kb") or {}).get("growth_kb") or 0.0)),
    }
    growth_total = sum(growths.values())
    leak_types: List[Dict[str, Any]] = []
    if growth_total > 0:
        for name, growth in sorted(growths.items(), key=lambda item: item[1], reverse=True):
            share = growth / growth_total
            if growth > 0 and (share >= 0.2 or not leak_types):
                leak_types.append({"type": name, "share_of_growth": round(share, 4), "evidence": "net_growth"})
    elif rows:
        trigger = next((row for row in reversed(rows) if row.get("is_trigger")), rows[-1])
        total = _number(trigger.get("total_mem_kb")) or sum(
            _number(trigger.get(key)) or 0.0 for key in ("total_pss_kb", "dma_kb", "gpu_kb")
        )
        if total:
            ratios = {
                "pss": (_number(trigger.get("total_pss_kb")) or 0.0) / total,
                "dma": (_number(trigger.get("dma_kb")) or 0.0) / total,
                "gpu": (_number(trigger.get("gpu_kb")) or 0.0) / total,
            }
            best = max(ratios, key=ratios.get)
            leak_types.append({"type": best, "share_at_trigger": round(ratios[best], 4), "evidence": "snapshot_only"})
            warnings.append("No positive time-series growth was available; type classification uses a low-confidence snapshot.")
    return {
        "path": str(file_path),
        "process_name": process,
        "pid": pid,
        "soft_threshold_kb": threshold_kb,
        "sample_count": len(rows),
        "trigger_indices": [index for index, row in enumerate(rows) if row.get("is_trigger")],
        "trends": trends,
        "leak_types": leak_types,
        "rows": rows,
        "warnings": warnings,
    }


def _smaps_category(name: str) -> str:
    lower = (name or "").lower()
    if "jemalloc" in lower or "native_heap" in lower or "[heap]" in lower:
        return "jemalloc"
    if any(token in lower for token in ("arkts", "jsvm", "flutter", "dart", "hermes", "v8", "kmp")):
        return "arkts"
    if "ashmem" in lower:
        return "ashmem"
    if lower in {"[anon]", "anon", "anonymous", ""} or "anon:" in lower:
        return "anon"
    if any(token in lower for token in (".so", ".hap", ".db", ".sqlite", ".ttf", ".otf", "/data/", "/system/")):
        return "file_mapping"
    return "other"


def _parse_standard_smaps(lines: Sequence[str]) -> List[Dict[str, Any]]:
    mappings: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    header = re.compile(r"^[0-9a-fA-F]+-[0-9a-fA-F]+\s+\S+\s+\S+\s+\S+\s+\S+\s*(.*)$")
    for line in lines:
        match = header.match(line.strip())
        if match:
            if current:
                mappings.append(current)
            current = {"name": match.group(1).strip(), "pss_kb": 0, "swap_pss_kb": 0, "size_kb": 0}
            continue
        if current is None:
            continue
        metric = re.match(r"^(Size|Pss|SwapPss):\s+(\d+)\s+kB", line.strip(), re.IGNORECASE)
        if metric:
            key = {"size": "size_kb", "pss": "pss_kb", "swappss": "swap_pss_kb"}[metric.group(1).lower()]
            current[key] = int(metric.group(2))
    if current:
        mappings.append(current)
    return mappings


def _parse_huawei_smaps_table(lines: Sequence[str]) -> List[Dict[str, Any]]:
    header_index = -1
    columns: List[str] = []
    for index, line in enumerate(lines):
        if re.search(r"\bSize\s+Rss\s+Pss\b", line) and "Name" in line:
            header_index = index
            columns = re.split(r"\s{2,}", line.strip())
            break
    if header_index < 0:
        return []
    indices = {name.lower(): idx for idx, name in enumerate(columns)}
    mappings: List[Dict[str, Any]] = []
    for line in lines[header_index + 1:]:
        values = re.split(r"\s{2,}", line.strip())
        if len(values) < 3 or not values[0].replace(",", "").isdigit():
            continue
        def value_for(*names: str) -> int:
            for name in names:
                idx = indices.get(name.lower())
                if idx is not None and idx < len(values):
                    return _int_number(values[idx])
            return 0
        name_idx = indices.get("name")
        mappings.append({
            "name": values[name_idx] if name_idx is not None and name_idx < len(values) else values[-1],
            "size_kb": value_for("size"),
            "pss_kb": value_for("pss"),
            "swap_pss_kb": value_for("swappss", "swap pss"),
        })
    return mappings


def _parse_nmd_sections(lines: Sequence[str]) -> Dict[str, Any]:
    snapshots: List[Dict[int, int]] = []
    for index, line in enumerate(lines):
        if "LOGGER_MEMCHECK_SAMPLE_NMD_INFO" not in line:
            continue
        snapshot: Dict[int, int] = {}
        for data_line in lines[index + 1:]:
            if re.search(r"\*+\s*endl\s*\*+", data_line):
                break
            fields = re.split(r"\s{2,}", data_line.strip())
            if len(fields) >= 2 and fields[0].isdigit() and fields[1].replace(",", "").isdigit():
                snapshot[int(fields[0])] = int(fields[1].replace(",", ""))
        if snapshot:
            snapshots.append(snapshot)

    jemalloc: Dict[int, int] = {}
    in_bins = False
    for line in lines:
        if re.search(r"(?:bins|large):\s+size", line):
            in_bins = True
            continue
        if in_bins and re.search(r"(?:large|extents):\s+size", line):
            in_bins = "large:" in line
            continue
        if in_bins:
            fields = re.split(r"\s+", line.strip())
            if len(fields) >= 3 and fields[0].isdigit() and fields[2].replace(",", "").isdigit():
                jemalloc[int(fields[0])] = int(fields[2].replace(",", ""))

    latest = snapshots[-1] if snapshots else {}
    previous = snapshots[-2] if len(snapshots) >= 2 else {}
    source = latest or jemalloc
    top_allocated = sorted(source.items(), key=lambda item: item[1], reverse=True)[:5]
    deltas = {size: allocated - previous.get(size, 0) for size, allocated in latest.items()} if latest else {}
    top_growth = sorted(deltas.items(), key=lambda item: item[1], reverse=True)[:5]
    selected_sizes = sorted({size for size, value in top_allocated + top_growth if value > 0})
    return {
        "snapshot_count": len(snapshots),
        "top_allocated": [{"size_bytes": size, "allocated_bytes": value} for size, value in top_allocated],
        "top_growth": [
            {"size_bytes": size, "allocated_bytes": latest.get(size, 0), "growth_bytes": value}
            for size, value in top_growth if value > 0
        ],
        "selected_sizes": selected_sizes,
    }


def parse_smaps_file(path: str) -> Dict[str, Any]:
    file_path = Path(path).expanduser().resolve()
    lines = _read_text(file_path).splitlines()
    mappings = _parse_huawei_smaps_table(lines) or _parse_standard_smaps(lines)
    categories: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"pss_kb": 0, "mapping_count": 0})
    for mapping in mappings:
        mapping["category"] = _smaps_category(str(mapping.get("name") or ""))
        combined = _int_number(mapping.get("pss_kb")) + _int_number(mapping.get("swap_pss_kb"))
        mapping["combined_pss_kb"] = combined
        categories[mapping["category"]]["pss_kb"] += combined
        categories[mapping["category"]]["mapping_count"] += 1
    total_pss = sum(int(value["pss_kb"]) for value in categories.values())
    category_rows = []
    for name, value in sorted(categories.items(), key=lambda item: item[1]["pss_kb"], reverse=True):
        category_rows.append({
            "type": name,
            "pss_kb": value["pss_kb"],
            "mapping_count": value["mapping_count"],
            "share": round(value["pss_kb"] / total_pss, 4) if total_pss else 0.0,
        })
    dominant = []
    if category_rows:
        dominant = [row for row in category_rows if row["share"] >= 0.2]
        if not dominant:
            dominant = category_rows[:1]
    return {
        "path": str(file_path),
        "mapping_count": len(mappings),
        "total_classified_pss_kb": total_pss,
        "categories": category_rows,
        "dominant_types": dominant,
        "top_mappings": sorted(mappings, key=lambda item: item.get("combined_pss_kb", 0), reverse=True)[:10],
        "nmd": _parse_nmd_sections(lines),
        "warnings": [] if mappings else ["No supported smaps table or /proc smaps mappings were found."],
    }


def _sqlite_tables(conn: sqlite3.Connection) -> Dict[str, set]:
    tables: Dict[str, set] = {}
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        tables[name] = {row[1] for row in conn.execute(f'PRAGMA table_info("{name}")')}
    return tables


def _dict_lookup(conn: sqlite3.Connection) -> Dict[int, str]:
    try:
        return {int(row[0]): str(row[1] or "") for row in conn.execute("SELECT id, data FROM data_dict")}
    except sqlite3.Error:
        return {}


def analyze_native_hook_database(
    path: str,
    *,
    sizes: Optional[Sequence[int]] = None,
    leak_type: str = "",
    max_results: int = 5,
    min_percentage: float = 0.0,
) -> Dict[str, Any]:
    """Analyze outstanding allocations from a trace_streamer SQLite DB read-only."""
    file_path = Path(path).expanduser().resolve()
    if not file_path.is_file():
        raise FileNotFoundError(f"native hook database does not exist: {file_path}")
    uri = f"file:{quote(str(file_path))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = _sqlite_tables(conn)
        dictionary = _dict_lookup(conn)
        event_filter = _TRACE_EVENT_TYPES.get(leak_type.lower(), leak_type or "")
        records: List[Dict[str, Any]] = []
        source_table = ""
        total_outstanding: Optional[int] = None
        total_allocation_count: Optional[int] = None
        size_filter = {int(value) for value in (sizes or []) if int(value) > 0}
        native_cols = tables.get("native_hook", set())
        if {"callchain_id", "heap_size", "end_ts"}.issubset(native_cols):
            clauses = ["(end_ts IS NULL OR end_ts = 0)"]
            params: List[Any] = []
            if event_filter and "event_type" in native_cols:
                clauses.append("event_type = ?")
                params.append(event_filter)
            total_row = conn.execute(
                "SELECT COALESCE(SUM(heap_size), 0), COUNT(*) FROM native_hook WHERE "
                + " AND ".join(clauses),
                params,
            ).fetchone()
            total_outstanding = int(total_row[0] or 0)
            total_allocation_count = int(total_row[1] or 0)
            if size_filter:
                placeholders = ",".join("?" for _ in size_filter)
                clauses.append(f"heap_size IN ({placeholders})")
                params.extend(sorted(size_filter))
            query = (
                "SELECT callchain_id, "
                + ("ipid" if "ipid" in native_cols else "0")
                + " AS pid, "
                + ("itid" if "itid" in native_cols else "0")
                + " AS tid, heap_size AS outstanding_bytes, "
                + ("event_type" if "event_type" in native_cols else "''")
                + " AS event_type FROM native_hook WHERE "
                + " AND ".join(clauses)
            )
            records = [dict(row) for row in conn.execute(query, params)]
            source_table = "native_hook"
        stat_cols = tables.get("native_hook_statistic", set())
        if not records and {"callchain_id", "apply_size", "release_size", "type"}.issubset(stat_cols):
            clauses = ["apply_size > release_size"]
            params = []
            reverse_types = {name: type_id for type_id, name in _TRACE_TYPE_IDS.items()}
            if event_filter in reverse_types:
                clauses.append("type = ?")
                params.append(reverse_types[event_filter])
            query = (
                "SELECT callchain_id, "
                + ("ipid" if "ipid" in stat_cols else "0")
                + " AS pid, 0 AS tid, MAX(apply_size - release_size) AS outstanding_bytes, "
                "type FROM native_hook_statistic WHERE "
                + " AND ".join(clauses)
                + " GROUP BY callchain_id, type"
            )
            for row in conn.execute(query, params):
                item = dict(row)
                item["event_type"] = _TRACE_TYPE_IDS.get(int(item.pop("type")), "unknown")
                records.append(item)
            source_table = "native_hook_statistic"

        if not records:
            return {
                "path": str(file_path),
                "source_table": source_table or None,
                "total_outstanding_bytes": 0,
                "total_outstanding_allocations": 0,
                "results": [],
                "warnings": ["No outstanding allocation records were found in a supported trace table."],
            }

        frame_map: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        frame_cols = tables.get("native_hook_frame", set())
        callchain_ids = sorted({int(item["callchain_id"]) for item in records})
        if callchain_ids and {"callchain_id", "depth"}.issubset(frame_cols):
            for offset in range(0, len(callchain_ids), 500):
                chunk = callchain_ids[offset:offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                select = ["callchain_id", "depth"]
                for col in ("ip", "symbol_id", "file_id", "symbol_offset"):
                    select.append(col if col in frame_cols else f"0 AS {col}")
                query = f"SELECT {', '.join(select)} FROM native_hook_frame WHERE callchain_id IN ({placeholders}) ORDER BY callchain_id, depth"
                for row in conn.execute(query, chunk):
                    frame = dict(row)
                    frame["symbol"] = dictionary.get(int(frame.get("symbol_id") or 0), "")
                    frame["file"] = dictionary.get(int(frame.get("file_id") or 0), "")
                    frame_map[int(frame["callchain_id"])].append(frame)

        total = total_outstanding
        if total is None:
            total = sum(max(0, _int_number(item.get("outstanding_bytes"))) for item in records)
        grouped: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
        for item in records:
            frames = frame_map.get(int(item["callchain_id"]), [])
            signature = tuple((frame.get("symbol_id"), frame.get("file_id"), frame.get("ip")) for frame in frames[1:] or frames)
            key = (item.get("tid"), item.get("event_type"), signature or (item["callchain_id"],))
            current = grouped.setdefault(key, {
                "callchain_id": item["callchain_id"],
                "pid": item.get("pid"),
                "tid": item.get("tid"),
                "event_type": item.get("event_type"),
                "outstanding_bytes": 0,
                "allocation_count": 0,
                "block_sizes": [],
                "frames": frames,
            })
            outstanding = max(0, _int_number(item.get("outstanding_bytes")))
            current["outstanding_bytes"] += outstanding
            current["allocation_count"] += 1
            current["block_sizes"].append(outstanding)

        results = []
        for item in grouped.values():
            item["percentage"] = round(item["outstanding_bytes"] / total * 100.0, 2) if total else 0.0
            item["stack_type"] = "managed" if any(
                re.search(r"ark|js|hermes|dart|flutter|v8", " ".join((str(f.get("symbol") or ""), str(f.get("file") or ""))), re.IGNORECASE)
                for f in item["frames"][:3]
            ) else "native"
            app_frame = next((frame for frame in item["frames"] if frame.get("symbol") and not _RUNTIME_FRAME_RE.search(str(frame.get("symbol")))), None)
            item["suspected_frame"] = app_frame
            if item["percentage"] >= min_percentage:
                results.append(item)
        results.sort(key=lambda item: item["outstanding_bytes"], reverse=True)
        warnings = []
        if source_table == "native_hook_statistic" and size_filter:
            warnings.append("Size filtering is unavailable for statistic-only traces; call chains use outstanding aggregate bytes.")
        return {
            "path": str(file_path),
            "source_table": source_table,
            "total_outstanding_bytes": total,
            "total_outstanding_allocations": total_allocation_count if total_allocation_count is not None else len(records),
            "results": results[:max(1, int(max_results))],
            "warnings": warnings,
        }
    finally:
        conn.close()


def parse_kernel_dma_file(path: str) -> Dict[str, Any]:
    file_path = Path(path).expanduser().resolve()
    lines = _read_text(file_path).splitlines()
    memory_name = ""
    for line in lines:
        match = re.search(r"memoryName:\s*([^\s]+)", line, re.IGNORECASE)
        if match:
            memory_name = match.group(1).lower()
            break
    header_index = -1
    columns: List[str] = []
    for index, line in enumerate(lines):
        if re.search(r"\b(?:Process|process)\b.*\bpid\b.*\b(?:size_bytes|size)\b", line):
            header_index = index
            columns = re.split(r"\t|\s{2,}", line.strip())
            if len(columns) < 3:
                columns = re.split(r"\s+", line.strip())
            break
    aliases = {
        "process": "process_name", "process name": "process_name", "pid": "pid", "process id": "pid",
        "size_bytes": "size_bytes", "size": "size_bytes", "ino": "magic", "magic": "magic",
        "buf_name": "buf_name", "buf_type": "buf_type", "leak_type": "leak_type", "is_reclaim": "is_reclaim",
    }
    indices = {aliases[name.lower()]: idx for idx, name in enumerate(columns) if name.lower() in aliases}
    records: List[Dict[str, Any]] = []
    if header_index >= 0 and "size_bytes" in indices:
        for line in lines[header_index + 1:]:
            if re.match(r"^[*=-]{5,}", line.strip()):
                break
            fields = re.split(r"\t|\s{2,}", line.strip())
            if len(fields) < len(columns):
                fields = re.split(r"\s+", line.strip())
            if len(fields) <= indices["size_bytes"] or "Total" in fields:
                continue
            record = {key: fields[idx].strip() for key, idx in indices.items() if idx < len(fields)}
            record["size_bytes"] = _int_number(record.get("size_bytes"))
            if record["size_bytes"] <= 0:
                continue
            record["knowledge"] = classify_dma_label(
                str(record.get("buf_name") or ""),
                str(record.get("leak_type") or ""),
                str(record.get("buf_type") or ""),
            )
            records.append(record)

    # A buffer can appear in several processes. Attribute it once, preferring an app process.
    system_names = {"render_service", "surfaceflinger", "allocator_host", "composer_host", "camera_service", "media_service"}
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for index, record in enumerate(records):
        groups[str(record.get("magic") or f"row-{index}")].append(record)
    owned: List[Dict[str, Any]] = []
    for group in groups.values():
        candidates = [row for row in group if str(row.get("process_name") or "") not in system_names]
        owned.append((candidates or group)[0])
    process_totals: Dict[Tuple[str, str], int] = defaultdict(int)
    for record in owned:
        process_totals[(str(record.get("process_name") or "unknown"), str(record.get("pid") or ""))] += record["size_bytes"]
    top_processes = [
        {"process_name": key[0], "pid": key[1], "dma_bytes": value}
        for key, value in sorted(process_totals.items(), key=lambda item: item[1], reverse=True)[:5]
    ]
    return {
        "path": str(file_path),
        "memory_name": memory_name or ("dma" if records else "unknown"),
        "record_count": len(records),
        "unique_buffer_count": len(owned),
        "total_unique_dma_bytes": sum(record["size_bytes"] for record in owned),
        "top_processes": top_processes,
        "top_buffers": sorted(owned, key=lambda item: item["size_bytes"], reverse=True)[:10],
        "warnings": [] if records else ["No supported DMA buffer table was found."],
    }


def parse_kernel_memory_file(path: str) -> Dict[str, Any]:
    """Parse kernel DMA/GPU/SLAB/ashmem evidence with graceful partial support."""
    file_path = Path(path).expanduser().resolve()
    lines = _read_text(file_path).splitlines()
    text = "\n".join(lines)
    match = re.search(r"memoryName:\s*([^\s]+)", text, re.IGNORECASE)
    kind = (match.group(1).lower() if match else "") or ("dma" if "dma" in text.lower() or "ion" in text.lower() else "unknown")
    result: Dict[str, Any] = {
        "path": str(file_path),
        "memory_name": kind,
        "kind": "dma" if kind == "ion" else kind,
        "warnings": [],
    }
    if kind in {"ion", "dma"} or "size_bytes" in text:
        result.update(parse_kernel_dma_file(path))
        result["kind"] = "dma"
        return result

    if kind == "gpu" or re.search(r"Total\s+[UAP]\s*\(device\)", text, re.IGNORECASE):
        categories: Dict[str, int] = defaultdict(int)
        for line in lines:
            category = re.search(r"C:([^:]+):\s*(\d+)", line)
            if category:
                categories[category.group(1).strip()] += int(category.group(2))
        totals = {}
        for label, pattern in {
            "used_device_bytes": r"Total\s+U\s*\(device\):\s*(\d+)",
            "allocated_device_bytes": r"Total\s+A\s*\(device\):\s*(\d+)",
            "purgeable_device_bytes": r"Total\s+P\s*\(device\):\s*(\d+)",
        }.items():
            total_match = re.search(pattern, text, re.IGNORECASE)
            if total_match:
                totals[label] = int(total_match.group(1))
        result["gpu"] = {
            **totals,
            "top_categories": [
                {"name": name, "bytes": value}
                for name, value in sorted(categories.items(), key=lambda item: item[1], reverse=True)[:10]
            ],
        }
        if not totals and not categories:
            result["warnings"].append("GPU leak type was declared but no supported GPU totals were found.")
        result["kind"] = "gpu"
        return result

    if kind == "slab" or "slabinfo - version" in text:
        entries: List[Dict[str, Any]] = []
        in_table = False
        for line in lines:
            if "slabinfo - version" in line or line.strip().startswith("# name"):
                in_table = True
                continue
            if not in_table:
                continue
            fields = line.split()
            if len(fields) >= 4 and fields[1].isdigit() and fields[2].isdigit() and fields[3].isdigit():
                active, count, obj_size = int(fields[1]), int(fields[2]), int(fields[3])
                entries.append({
                    "name": fields[0],
                    "active_objects": active,
                    "object_count": count,
                    "object_size_bytes": obj_size,
                    "active_bytes": active * obj_size,
                    "allocated_bytes": count * obj_size,
                })
            elif entries and re.match(r"^[*=-]{3,}", line.strip()):
                break
        result["slab"] = {
            "total_active_bytes": sum(item["active_bytes"] for item in entries),
            "top_caches": sorted(entries, key=lambda item: item["active_bytes"], reverse=True)[:10],
        }
        if not entries:
            result["warnings"].append("SLAB leak type was declared but no slabinfo rows were parsed.")
        result["kind"] = "slab"
        return result

    if kind == "ashmem" or "Process_name Virtual_size Physical_size" in text:
        entries = []
        header_index = next((i for i, line in enumerate(lines) if "Process_name Virtual_size Physical_size" in line), -1)
        if header_index >= 0:
            for line in lines[header_index + 1:]:
                fields = line.split()
                if len(fields) < 3 or not fields[1].isdigit() or not fields[2].isdigit():
                    if entries:
                        break
                    continue
                entries.append({"process_name": fields[0], "virtual_kb": int(fields[1]), "physical_kb": int(fields[2])})
        result["ashmem"] = {"top_processes": sorted(entries, key=lambda item: item["physical_kb"], reverse=True)[:10]}
        if not entries:
            result["warnings"].append("Ashmem leak type was declared but no process overview was parsed.")
        result["kind"] = "ashmem"
        return result

    result["warnings"].append("Kernel leak file type is not yet recognized by a deterministic parser.")
    return result


def _build_fault_modes(sample: Dict[str, Any], smaps: Dict[str, Any], trace: Dict[str, Any], kernel: Dict[str, Any]) -> List[Dict[str, Any]]:
    modes: List[Dict[str, Any]] = []
    pss_trend = (sample.get("trends") or {}).get("total_pss_kb") or {}
    pss_growth = float(pss_trend.get("growth_kb") or 0.0)
    for category in smaps.get("dominant_types") or []:
        subtype = str(category.get("type") or "other")
        candidates = {
            "jemalloc": ["unpaired allocation", "lifecycle error", "unbounded cache", "shared_ptr cycle"],
            "arkts": ["managed object retained by GC root", "cross-language reference cycle"],
            "ashmem": ["shared memory or PixelMap handle not released"],
            "anon": ["mmap not paired with munmap", "thread stack accumulation"],
            "file_mapping": ["database/resource mapping retained", "excess shared-library or font mappings"],
        }.get(subtype, ["memory category requires further evidence"])
        direct_trace = bool(trace.get("results")) and subtype in {"jemalloc", "anon"}
        modes.append({
            "root_cause_l1": "Process PSS leak" if pss_growth > 0 else "High process PSS",
            "root_cause_l2": subtype,
            "root_cause_l3_candidates": candidates,
            "confidence": "high" if pss_growth > 0 and direct_trace else ("medium" if pss_growth > 0 else "low"),
            "evidence": {
                "pss_growth_kb": pss_growth,
                "category_pss_kb": category.get("pss_kb"),
                "category_share": category.get("share"),
                "outstanding_trace_available": direct_trace,
            },
        })
    if kernel.get("top_buffers"):
        modes.append({
            "root_cause_l1": "DMA memory leak or retention",
            "root_cause_l2": (kernel.get("top_buffers") or [{}])[0].get("knowledge", {}).get("component", "DMA buffer owner"),
            "root_cause_l3_candidates": [
                (kernel.get("top_buffers") or [{}])[0].get("knowledge", {}).get("suspect", "DMA buffer not released")
            ],
            "confidence": "medium",
            "evidence": {"total_unique_dma_bytes": kernel.get("total_unique_dma_bytes"), "unique_buffers": kernel.get("unique_buffer_count")},
        })
    if (kernel.get("gpu") or {}).get("top_categories"):
        modes.append({
            "root_cause_l1": "GPU memory leak or retention",
            "root_cause_l2": "GPU resource lifecycle",
            "root_cause_l3_candidates": ["EGL context retained", "GLES/Vulkan texture or buffer not destroyed", "render cache not bounded"],
            "confidence": "medium",
            "evidence": kernel.get("gpu"),
        })
    if (kernel.get("slab") or {}).get("top_caches"):
        modes.append({
            "root_cause_l1": "Kernel SLAB growth",
            "root_cause_l2": "Kernel object cache accumulation",
            "root_cause_l3_candidates": ["kernel object lifecycle leak", "driver cache or queue grows without reclamation"],
            "confidence": "medium",
            "evidence": kernel.get("slab"),
        })
    return modes


def _build_evidence_chain(sample: Dict[str, Any], smaps: Dict[str, Any], trace: Dict[str, Any], kernel: Dict[str, Any]) -> List[Dict[str, Any]]:
    evidence: List[Dict[str, Any]] = []
    for metric, trend in (sample.get("trends") or {}).items():
        evidence.append({
            "type": "memory_trend",
            "finding": f"{metric}: {trend.get('start_kb')} KB -> {trend.get('end_kb')} KB ({trend.get('trend')})",
            "raw_values": trend,
            "strength": "strong" if trend.get("samples", 0) >= 3 else "weak",
        })
    if smaps.get("dominant_types"):
        evidence.append({
            "type": "pss_breakdown",
            "finding": "Dominant PSS categories were calculated from Pss + SwapPss.",
            "raw_values": smaps.get("dominant_types"),
            "strength": "strong",
        })
    nmd = smaps.get("nmd") or {}
    if nmd.get("top_growth"):
        evidence.append({"type": "nmd_diff", "finding": "NMD size classes with the largest allocation growth.", "raw_values": nmd["top_growth"], "strength": "strong"})
    if trace.get("results"):
        evidence.append({
            "type": "outstanding_callchains",
            "finding": "Outstanding allocations were grouped by native hook call chain.",
            "raw_values": trace.get("results"),
            "strength": "direct",
        })
    if kernel.get("top_buffers"):
        evidence.append({"type": "dma_ownership", "finding": "Unique DMA buffers were attributed once to a likely owner.", "raw_values": kernel.get("top_buffers"), "strength": "strong"})
    if (kernel.get("gpu") or {}).get("top_categories"):
        evidence.append({"type": "gpu_breakdown", "finding": "GPU memory categories were ranked by observed bytes.", "raw_values": kernel.get("gpu"), "strength": "strong"})
    if (kernel.get("slab") or {}).get("top_caches"):
        evidence.append({"type": "slab_breakdown", "finding": "Kernel slab caches were ranked by active object bytes.", "raw_values": kernel.get("slab"), "strength": "strong"})
    return evidence


def _build_prompt_section(result: Dict[str, Any]) -> str:
    lines = ["## Native 内存泄漏确定性诊断", ""]
    overview = result.get("overview") or {}
    lines.append(f"- 场景: {overview.get('scenario') or 'unknown'}")
    if overview.get("process_name"):
        lines.append(f"- 进程: {overview.get('process_name')} (PID {overview.get('pid')})")
    for leak_type in (result.get("sample") or {}).get("leak_types") or []:
        lines.append(f"- 增长归因候选: {leak_type.get('type')} ({leak_type.get('evidence')})")
    lines.append("")
    lines.append("### 证据链")
    for item in (result.get("evidence_chain") or [])[:8]:
        lines.append(f"- [{item.get('strength')}] {item.get('finding')}")
    lines.append("")
    lines.append("### 根因边界")
    lines.append("- 趋势和占比可确认内存增长发生在哪一类；只有未释放调用栈或代码生命周期证据才能确认具体泄漏点。")
    lines.append("- 未达到上述证据条件的三级根因均为候选，不得表述为唯一已确认根因。")
    return "\n".join(lines).rstrip() + "\n"


def collect_source_search_queries(result: Dict[str, Any], max_queries: int = 8) -> List[str]:
    """Collect application symbols and platform APIs worth locating in source."""
    queries: List[str] = []
    for chain in (result.get("native_hook") or {}).get("results") or []:
        frame = chain.get("suspected_frame") or {}
        symbol = re.sub(r"\(.*$", "", str(frame.get("symbol") or "")).strip()
        if symbol and symbol not in queries:
            queries.append(symbol)
    for buffer in (result.get("kernel_dma") or {}).get("top_buffers") or []:
        for term in (buffer.get("knowledge") or {}).get("search_terms") or []:
            term = str(term or "").strip()
            if term and term not in queries:
                queries.append(term)
    return queries[:max(1, int(max_queries))]


def analyze_native_leak_bundle(
    path: str,
    *,
    trace_db: str = "",
    max_callchains: int = 5,
    min_callchain_percentage: float = 0.0,
) -> Dict[str, Any]:
    discovery = discover_native_leak_bundle(path)
    selected = discovery["selected"]
    warnings = list(discovery.get("warnings") or [])
    sample: Dict[str, Any] = {}
    smaps: Dict[str, Any] = {}
    trace: Dict[str, Any] = {}
    kernel: Dict[str, Any] = {}

    if selected.get("sample"):
        sample = parse_sample_file(selected["sample"][-1])
        warnings.extend(sample.get("warnings") or [])
    if selected.get("smaps"):
        smaps = parse_smaps_file(selected["smaps"][-1])
        warnings.extend(smaps.get("warnings") or [])
    nmd_sizes = ((smaps.get("nmd") or {}).get("selected_sizes") or [])
    database = trace_db
    if not database:
        candidates = [p for p in selected.get("profile") or [] if Path(p).suffix.lower() in {".db", ".sqlite", ".sqlite3"}]
        candidates += list(discovery.get("unrecognized_databases") or [])
        database = candidates[-1] if candidates else ""
    if database:
        trace = analyze_native_hook_database(
            database,
            sizes=nmd_sizes or None,
            leak_type="malloc" if nmd_sizes else "",
            max_results=max_callchains,
            min_percentage=min_callchain_percentage,
        )
        warnings.extend(trace.get("warnings") or [])
    elif selected.get("profile"):
        warnings.append("A text profiler trace was found but no trace_streamer SQLite database was supplied; the bundled executable is intentionally not invoked.")
    if selected.get("kernel"):
        kernel = parse_kernel_memory_file(selected["kernel"][-1])
        warnings.extend(kernel.get("warnings") or [])

    fault_modes = _build_fault_modes(sample, smaps, trace, kernel)
    evidence = _build_evidence_chain(sample, smaps, trace, kernel)
    dominant_types = [str(item.get("type")) for item in smaps.get("dominant_types") or []]
    if kernel.get("top_buffers"):
        dominant_types.append("dma")
    if (kernel.get("gpu") or {}).get("top_categories"):
        dominant_types.append("gpu")
    fix_directions = []
    for leak_type in dominant_types:
        fix_directions.extend(LEAK_FIX_DIRECTIONS.get(leak_type, []))
    result = {
        "schema_version": 1,
        "analyzed": bool(evidence),
        "skill": "native-memleak-analysis",
        "overview": {
            "scenario": discovery.get("scenario"),
            "process_name": selected.get("process_name"),
            "pid": selected.get("pid"),
            "input_root": discovery.get("root"),
            "files": selected,
        },
        "sample": sample,
        "smaps": smaps,
        "native_hook": trace,
        "kernel_memory": kernel,
        "kernel_dma": kernel,
        "fault_mode_matches": fault_modes,
        "evidence_chain": evidence,
        "fix_directions": list(dict.fromkeys(fix_directions)),
        "warnings": list(dict.fromkeys(warnings)),
        "limitations": [
            "OOM or high memory is not by itself proof of a leak.",
            "A level-3 root cause is confirmed only when allocation stack and code lifecycle evidence agree.",
            "ArkTS object retention requires a heap snapshot and retainer-chain analysis.",
        ],
    }
    result["prompt_section_zh"] = _build_prompt_section(result)
    return result
