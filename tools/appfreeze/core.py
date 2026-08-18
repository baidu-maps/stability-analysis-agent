#!/usr/bin/env python3
"""Normalize existing ANR/EventHandler/Binder evidence for AppFreeze cases."""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


SYSTEM_FRAME_RE = re.compile(
    r"libc\.|libutils|libeventhandler|EventHandler::|libace|libark|libuv|"
    r"pthread_|__libc|libbinder|android::|system_server|libhwbinder",
    re.I,
)
BINDER_TRANS_RE = re.compile(
    r"(?P<src_pid>\d+):(?P<src_tid>\d+) to (?P<dst_pid>\d+):(?P<dst_tid>\d+) "
    r"code (?P<code>\S+) wait:(?P<wait>[\d.]+) s"
)
GENERIC_WAIT_RE = re.compile(
    r"(?P<waiter>\S+)\s+(?:waiting on|waiting for|blocked on|held by)\s+(?P<holder>\S+)",
    re.I,
)
LOCK_RE = re.compile(r"pthread_mutex|futex|std::mutex|Monitor::Wait|__psynch_mutex|os_unfair_lock", re.I)


def classify_freeze_type(data: Mapping[str, Any], raw_content: str = "") -> Dict[str, Any]:
    text = " ".join([str(raw_content or ""), str(data.get("freeze_type") or ""), str(data.get("freeze_reason") or ""), str(data.get("reason") or "")]).upper()
    patterns = [("INPUT_BLOCK", 5000, ("INPUT_BLOCK", "INPUT BLOCK")), ("LIFECYCLE_TIMEOUT", 5000, ("LIFECYCLE_TIMEOUT", "LIFECYCLE TIMEOUT")), ("RENDER_SERVICE_TIMEOUT", 6000, ("RENDER_SERVICE", "RENDER SERVICE")), ("FFRT_TIMEOUT", 6000, ("FFRT", "FFRT_TIMEOUT")), ("THREAD_BLOCK_20S", 20000, ("THREAD_BLOCK_20S", "20S")), ("THREAD_BLOCK_6S", 6000, ("THREAD_BLOCK_6S", "6S")), ("THREAD_BLOCK_3S", 3000, ("THREAD_BLOCK_3S", "3S")), ("APPFREEZE", 6000, ("APPFREEZE", "APP FREEZE", "ANR"))]
    for kind, timeout, hints in patterns:
        if any(hint in text for hint in hints):
            return {"freeze_type": kind, "reason": kind, "timeout_threshold_ms": timeout, "confidence": 0.92, "evidence": [hint for hint in hints if hint in text]}
    return {"freeze_type": "UNKNOWN", "reason": "UNKNOWN", "timeout_threshold_ms": None, "confidence": 0.2, "evidence": []}


def _frame_signature(frame: Mapping[str, Any]) -> str:
    return str(frame.get("function") or frame.get("symbol") or frame.get("name") or frame.get("module") or "<unknown>").strip()


def _frames(sample: Mapping[str, Any]) -> List[str]:
    values = sample.get("frames") or sample.get("stack") or sample.get("symbols") or []
    if isinstance(values, str):
        return [line.strip() for line in values.splitlines() if line.strip()]
    return [_frame_signature(item) if isinstance(item, Mapping) else str(item) for item in values]


def cluster_stack_samples(samples: Sequence[Mapping[str, Any]], prefix_depth: int = 5) -> List[Dict[str, Any]]:
    groups: Dict[Tuple[str, ...], List[Mapping[str, Any]]] = defaultdict(list)
    for sample in samples:
        frames = _frames(sample)
        if frames:
            groups[tuple(frames[:prefix_depth])].append(sample)
    total = len(samples) or 1
    result = []
    for index, (prefix, items) in enumerate(sorted(groups.items(), key=lambda pair: (-len(pair[1]), pair[0]))):
        result.append({"cluster_id": f"freeze-stack-{index + 1}", "sample_count": len(items), "sample_ratio": round(len(items) / total, 3), "stable_prefix": list(prefix), "timestamps": [item.get("timestamp") or item.get("at") for item in items], "confidence": round(min(0.99, 0.55 + len(items) / total * 0.44), 3)})
    return result


def detect_dependency_cycles(edges: Sequence[Mapping[str, Any]]) -> List[List[str]]:
    graph: Dict[str, List[str]] = defaultdict(list)
    for edge in edges:
        source = str(edge.get("source") or edge.get("from") or edge.get("waiter") or "")
        target = str(edge.get("target") or edge.get("to") or edge.get("dependency") or "")
        if source and target:
            graph[source].append(target)
    cycles: List[List[str]] = []

    def visit(node: str, path: List[str], active: set) -> None:
        if node in active:
            cycle = path[path.index(node):]
            body = cycle[:-1]
            rotations = [body[offset:] + body[:offset] for offset in range(len(body))]
            canonical = min(rotations)
            normalized = canonical + [canonical[0]]
            if normalized not in cycles:
                cycles.append(normalized)
            return
        active.add(node)
        for child in graph.get(node, []):
            visit(child, path + [child], active.copy())

    for node in graph:
        visit(node, [node], set())
    return cycles


def parse_system_load(raw: str) -> Dict[str, Any]:
    text = raw or ""
    cpu_match = re.search(r"(?:cpu(?:\s+usage)?|CPU Usage)\s*[:=]?\s*(\d+(?:\.\d+)?)\s*%", text, re.I)
    mem_match = re.search(r"(?:MemAvailable|avail(?:able)?\s*mem(?:ory)?|Device Memory\(kB\))\s*[:=]?\s*(\d+(?:\.\d+)?)\s*(MB|KB|kB)?", text, re.I)
    cpu = float(cpu_match.group(1)) if cpu_match else None
    mem_mb = None
    if mem_match:
        value = float(mem_match.group(1))
        unit = (mem_match.group(2) or "MB").upper()
        mem_mb = value / 1024.0 if unit.startswith("K") else value
    thermal = bool(re.search(r"thermal throttling|low memory and thermal|hot_level\s*[:=]\s*[1-9]", text, re.I))
    note = ""
    note_match = re.search(r"(?im)^\s*NOTE:\s*(.+)$", text)
    if note_match:
        note = note_match.group(1).strip()
        if re.search(r"low memory|thermal", note, re.I):
            thermal = True
    stressed = bool((cpu is not None and cpu >= 85.0) or (mem_mb is not None and mem_mb < 800) or thermal)
    return {
        "cpu_percent": cpu,
        "available_memory_mb": round(mem_mb, 2) if mem_mb is not None else None,
        "thermal": thermal,
        "note": note,
        "system_stressed": stressed,
        "early_exit": stressed,
        "reason": "CPU/内存/热节流达到系统噪声门禁，优先作为环境证据而不是业务根因" if stressed else "",
    }


def parse_event_handler(raw: str) -> Dict[str, Any]:
    text = raw or ""
    running = ""
    running_match = re.search(r"Current Running:\s*(.+)", text)
    if running_match:
        running = running_match.group(1).strip()
    depth = None
    depth_match = re.search(r"(?:Total event size|pending(?:_count)?|queue(?:_depth| size))\s*[:=]\s*(\d+)", text, re.I)
    if depth_match:
        depth = int(depth_match.group(1))
    running_seconds = None
    duration_match = re.search(r"Current Running:.*?(\d+(?:\.\d+)?)\s*s", text, re.I)
    if duration_match:
        running_seconds = float(duration_match.group(1))
    if not running and depth is None:
        return {}
    return {"current_running": running, "queue_depth": depth, "running_seconds": running_seconds, "blocked": bool(running_seconds and running_seconds > 3)}


def parse_binder_text(raw: str) -> Dict[str, Any]:
    text = raw or ""
    edges: List[Dict[str, Any]] = []
    ipc_full = False
    for match in BINDER_TRANS_RE.finditer(text):
        src = f"{match.group('src_pid')}:{match.group('src_tid')}"
        dst = f"{match.group('dst_pid')}:{match.group('dst_tid')}"
        edges.append({
            "source": src,
            "target": dst,
            "code": match.group("code"),
            "wait_seconds": float(match.group("wait")),
            "ipc_full": match.group("dst_tid") == "0",
        })
        if match.group("dst_tid") == "0":
            ipc_full = True
    for match in GENERIC_WAIT_RE.finditer(text):
        waiter, holder = match.group("waiter"), match.group("holder")
        if waiter and holder and waiter != holder:
            edges.append({"source": waiter, "target": holder, "code": "wait", "wait_seconds": 0.0, "ipc_full": False})
    if re.search(r"IPC FULL|ready\s+0\b|binder thread pool", text, re.I):
        ipc_full = True
    cycles = detect_dependency_cycles(edges)
    return {"edges": edges, "cycles": cycles, "deadlock_cycles": cycles, "ipc_full": ipc_full} if edges or ipc_full else {}


def analyze_sample_hotspots(samples: Sequence[Mapping[str, Any]], busy_ratio: float = 0.3) -> Dict[str, Any]:
    counts: Dict[str, int] = defaultdict(int)
    for sample in samples:
        business = [frame for frame in _frames(sample) if frame and not SYSTEM_FRAME_RE.search(frame)]
        if business:
            counts[business[0]] += 1
    total = len(samples) or 1
    hotspots = [
        {"frame": frame, "count": count, "ratio": round(count / total, 3), "busy": count / total >= busy_ratio}
        for frame, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    return {"hotspots": hotspots, "dominant": hotspots[0] if hotspots else None, "busy": bool(hotspots and hotspots[0]["busy"])}


def analyze_sample_hotspots_deep(
    samples: Sequence[Mapping[str, Any]],
    *,
    busy_ratio: float = 0.3,
    max_depth: int = 15,
) -> Dict[str, Any]:
    """Cross-snapshot full-depth business frame frequency analysis.

    Unlike analyze_sample_hotspots (which only checks frame[0]):
    - Scans all depths (up to max_depth) of each snapshot for business frames
    - Each snapshot counts a function at most once (presence/absence)
    - Outputs: per-function cross-snapshot frequency, position stats, representative chains
    - Finds "parent node": deepest business frame from the bottom of each snapshot

    Inspired by Huawei's sample_stack_analyzer.py approach.
    """
    total = len(samples)
    if not total:
        return {"total_snapshots": 0, "hotspots": [], "parent_nodes": [], "dominant_function": None}

    # Track per-function occurrence across snapshots
    func_snapshots: Dict[str, int] = defaultdict(int)  # function -> # snapshots it appears in
    func_positions: Dict[str, List[int]] = defaultdict(list)  # function -> position indices
    func_chains: Dict[str, List[str]] = {}  # function -> longest chain seen
    parent_counts: Dict[str, int] = defaultdict(int)  # parent node -> # snapshots

    for sample in samples:
        frames = _frames(sample)[:max_depth]
        # Extract business frames (non-system)
        seen_in_this_snapshot: set = set()
        business_frames_in_sample: List[str] = []

        for position, frame in enumerate(frames):
            if not frame or SYSTEM_FRAME_RE.search(frame):
                continue
            business_frames_in_sample.append(frame)
            if frame not in seen_in_this_snapshot:
                seen_in_this_snapshot.add(frame)
                func_snapshots[frame] += 1
                func_positions[frame].append(position)

        # Store longest chain for each function (representative)
        if business_frames_in_sample:
            chain = [f for f in frames if f and not SYSTEM_FRAME_RE.search(f)]
            for func in seen_in_this_snapshot:
                if func not in func_chains or len(chain) > len(func_chains[func]):
                    func_chains[func] = chain

        # Parent node: deepest business frame from bottom
        if frames:
            for frame in reversed(frames):
                if frame and not SYSTEM_FRAME_RE.search(frame):
                    parent_counts[frame] += 1
                    break

    # Build hotspot ranking
    hotspots = []
    for func, count in sorted(func_snapshots.items(), key=lambda x: (-x[1], x[0])):
        positions = func_positions[func]
        hotspots.append({
            "function": func,
            "occurrence_count": count,
            "occurrence_ratio": round(count / total, 3),
            "position_stats": {
                "min": min(positions),
                "max": max(positions),
                "avg": round(sum(positions) / len(positions), 1),
            },
            "representative_chain": func_chains.get(func, [func]),
            "busy": count / total >= busy_ratio,
        })

    # Build parent node ranking
    parent_nodes = [
        {"function": func, "count": count, "ratio": round(count / total, 3)}
        for func, count in sorted(parent_counts.items(), key=lambda x: (-x[1], x[0]))
    ]

    dominant = hotspots[0]["function"] if hotspots and hotspots[0]["occurrence_ratio"] >= busy_ratio else None

    return {
        "total_snapshots": total,
        "hotspots": hotspots,
        "parent_nodes": parent_nodes,
        "dominant_function": dominant,
    }


def compare_block_vs_busy(samples: Sequence[Mapping[str, Any]], prefix_depth: int = 3) -> Dict[str, Any]:
    groups: Dict[str, List[Tuple[str, ...]]] = {"3s": [], "6s": []}
    for sample in samples:
        stamp = str(sample.get("timestamp") or sample.get("at") or "").lower()
        frames = tuple(_frames(sample)[:prefix_depth])
        if not frames:
            continue
        if "3s" in stamp or stamp.endswith("3"):
            groups["3s"].append(frames)
        elif "6s" in stamp or stamp.endswith("6"):
            groups["6s"].append(frames)
    if not groups["3s"] or not groups["6s"]:
        return {"status": "insufficient_samples", "kind": None}
    top_3 = max(set(groups["3s"]), key=groups["3s"].count)
    top_6 = max(set(groups["6s"]), key=groups["6s"].count)
    kind = "BLOCKED" if top_3 == top_6 else "BUSY"
    return {"status": "success", "kind": kind, "prefix_3s": list(top_3), "prefix_6s": list(top_6), "same_top": top_3 == top_6}


def _lock_wait_frames(samples: Sequence[Mapping[str, Any]]) -> List[str]:
    found = []
    for sample in samples:
        for frame in _frames(sample):
            if LOCK_RE.search(frame) and frame not in found:
                found.append(frame)
    return found


def _fault_modes(freeze: Mapping[str, Any], event: Mapping[str, Any], binder: Mapping[str, Any], cycles: Sequence[Sequence[str]], samples: Sequence[Mapping[str, Any]], load: Mapping[str, Any], hotspots: Mapping[str, Any], block_busy: Mapping[str, Any], locks: Sequence[str]) -> List[Dict[str, Any]]:
    modes: List[Dict[str, Any]] = []
    if load.get("system_stressed"):
        modes.append({"id": "FREEZE-FM-11", "name": "系统高负载或 Thermal Throttling", "owner": "System", "confidence": 0.85, "evidence": [load.get("reason") or "system load marker"]})
    if cycles:
        modes.append({"id": "FREEZE-FM-06", "name": "FFRT/Binder 依赖环", "owner": "Application", "confidence": 0.9, "evidence": ["dependency cycle detected"]})
    if binder.get("cycles") or binder.get("deadlock_cycles"):
        modes.append({"id": "FREEZE-FM-05", "name": "Binder 环路死锁", "owner": "Application/System boundary", "confidence": 0.88, "evidence": ["binder cycle detected"]})
    if binder.get("ipc_full"):
        modes.append({"id": "FREEZE-FM-12", "name": "对端 Binder 线程耗尽 (IPC FULL)", "owner": "System boundary", "confidence": 0.8, "evidence": ["IPC FULL"]})
    running = str(event.get("current_running") or event.get("running_task") or "")
    if running:
        modes.append({"id": "FREEZE-FM-01", "name": "主线程同步耗时任务", "owner": "Application", "confidence": 0.72, "evidence": [running]})
    if event.get("queue_depth") or event.get("pending_count"):
        modes.append({"id": "FREEZE-FM-07", "name": "EventHandler 队列堆积", "owner": "Application", "confidence": 0.7, "evidence": ["pending event evidence"]})
    if locks:
        modes.append({"id": "FREEZE-FM-04", "name": "锁等待或互斥阻塞", "owner": "Application", "confidence": 0.76, "evidence": list(locks)[:3]})
    if block_busy.get("kind") == "BLOCKED":
        modes.append({"id": "FREEZE-FM-02", "name": "采样栈顶稳定，判定为阻塞", "owner": "Application", "confidence": 0.8, "evidence": ["3s/6s stack top unchanged"]})
    elif block_busy.get("kind") == "BUSY":
        modes.append({"id": "FREEZE-FM-03", "name": "采样栈顶变化，判定为繁忙", "owner": "Application", "confidence": 0.78, "evidence": ["3s/6s stack top changed"]})
    if hotspots.get("busy") and hotspots.get("dominant"):
        modes.append({"id": "FREEZE-FM-03", "name": "业务帧持续占热", "owner": "Application", "confidence": 0.77, "evidence": [hotspots["dominant"]["frame"]]})
    text = " ".join(str(sample) for sample in samples).lower()
    if any(token in text for token in ("gc", "garbage collection")):
        modes.append({"id": "FREEZE-FM-08", "name": "GC 长时间暂停", "owner": "Runtime", "confidence": 0.65, "evidence": ["GC marker in samples"]})
    if "thermal throttling" in text or "low memory" in text:
        modes.append({"id": "FREEZE-FM-11", "name": "系统高负载或 Thermal Throttling", "owner": "System", "confidence": 0.85, "evidence": ["system load marker"]})
    dedup: Dict[str, Dict[str, Any]] = {}
    for item in modes:
        old = dedup.get(item["id"])
        if old is None or item["confidence"] > old["confidence"]:
            dedup[item["id"]] = item
    return list(dedup.values())


def analyze_appfreeze(data: Mapping[str, Any], *, raw_content: str = "", samples: Optional[Sequence[Mapping[str, Any]]] = None, ffrt_edges: Optional[Sequence[Mapping[str, Any]]] = None) -> Dict[str, Any]:
    payload = data.get("parse_result") if isinstance(data.get("parse_result"), Mapping) else data
    raw = raw_content or str(payload.get("raw_content") or payload.get("raw_log") or "")
    freeze = classify_freeze_type(payload, raw)
    sample_items = list(samples or payload.get("samples") or payload.get("sampling_stacks") or [])
    parsed_event = parse_event_handler(raw)
    event = payload.get("event_handler") or payload.get("event_handler_analysis") or parsed_event or {}
    parsed_binder = parse_binder_text(raw)
    binder = payload.get("binder") or payload.get("binder_analysis") or parsed_binder or {}
    edges = list(ffrt_edges or payload.get("ffrt_edges") or payload.get("dependency_edges") or binder.get("edges") or [])
    load = parse_system_load(raw)
    clusters = cluster_stack_samples(sample_items)
    cycles = detect_dependency_cycles(edges)
    if not cycles and binder.get("cycles"):
        cycles = list(binder.get("cycles") or [])
    hotspots = analyze_sample_hotspots(sample_items)
    hotspots_deep = analyze_sample_hotspots_deep(sample_items)
    block_busy = compare_block_vs_busy(sample_items)
    locks = _lock_wait_frames(sample_items)
    modes = _fault_modes(freeze, event if isinstance(event, Mapping) else {}, binder if isinstance(binder, Mapping) else {}, cycles, sample_items, load, hotspots, block_busy, locks)
    # Boost FREEZE-FM-03 confidence if deep analysis finds dominant function > 50%
    if hotspots_deep.get("dominant_function"):
        for mode in modes:
            if mode["id"] == "FREEZE-FM-03":
                mode["confidence"] = max(mode["confidence"], 0.85)
                mode["evidence"].append(f"deep_dominant={hotspots_deep['dominant_function']}")
    missing: List[str] = []
    if freeze["freeze_type"] == "UNKNOWN":
        missing.append("Freeze 类型或 reason")
    if not sample_items:
        missing.append("多时间点采样栈")
    if not event:
        missing.append("EventHandler 队列证据")
    if not binder:
        missing.append("Binder 等待链证据")
    evidence_chain = [{"type": "freeze_type", "value": freeze["freeze_type"], "evidence": freeze["evidence"], "tier": 4}]
    if load.get("system_stressed"):
        evidence_chain.append({"type": "system_load", "value": load, "tier": 4})
    if clusters:
        evidence_chain.append({"type": "stack_cluster", "value": clusters[0]["stable_prefix"], "sample_count": clusters[0]["sample_count"], "tier": 3})
    if hotspots.get("dominant"):
        evidence_chain.append({"type": "sample_hotspot", "value": hotspots["dominant"], "tier": 4})
    if event:
        evidence_chain.append({"type": "event_handler", "value": event, "tier": 4})
    if binder:
        evidence_chain.append({"type": "binder", "value": binder, "tier": 3})
    if cycles:
        evidence_chain.append({"type": "dependency_cycle", "value": cycles, "tier": 3})
    if block_busy.get("kind"):
        evidence_chain.append({"type": "block_vs_busy", "value": block_busy, "tier": 3})
    confidence = max((float(item["confidence"]) for item in modes), default=freeze["confidence"])
    application = ["避免在主线程执行同步 I/O、长时间计算或同步 IPC。", "将耗时任务拆分到 Worker，并确保回调不会阻塞 UI 事件队列。"]
    if any(item["id"] in {"FREEZE-FM-05", "FREEZE-FM-06"} for item in modes):
        application.append("检查 Binder/FFRT 依赖环，解除互相等待的同步路径；不要把 IPC 框架本身当成根因。")
    if any(item["id"] == "FREEZE-FM-04" for item in modes):
        application.append("沿锁等待链找到持有锁且栈更深的业务线程，在持有侧缩短临界区。")
    return {
        "status": "success",
        "diagnosis_status": "confirmed" if confidence >= 0.85 else ("probable" if modes else "preliminary"),
        "freeze": freeze,
        "samples": sample_items,
        "stack_clusters": clusters,
        "sample_hotspots": hotspots,
        "sample_hotspots_deep": hotspots_deep,
        "block_vs_busy": block_busy,
        "system_load": load,
        "event_handler": event,
        "binder": binder,
        "ffrt": {"edges": edges, "cycles": cycles},
        "fault_modes": modes,
        "evidence_chain": evidence_chain,
        "root_cause": {"summary": modes[0]["name"] if modes else "证据不足，暂不能确定唯一根因", "confidence": round(confidence, 3)},
        "missing_evidence": missing,
        "repair_guidance": {"application": application, "system_observation": ["系统负载或 Thermal 信息仅作为环境证据，不直接作为源码修复目标。"]},
    }
