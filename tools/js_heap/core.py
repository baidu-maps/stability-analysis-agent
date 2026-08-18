#!/usr/bin/env python3
"""Deterministic analysis for V8/HarmonyOS heap snapshots.

The module deliberately emits compact JSON-friendly summaries instead of passing
the complete object graph to an LLM.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


ROOT_HINTS = {
    "globalhandler": ("JS-FM-01", "GlobalHandler 长期持有对象", "ROOT_GLOBAL_HANDLE"),
    "global": ("JS-FM-01", "全局对象长期持有对象", "ROOT_GLOBAL_HANDLE"),
    "global_env": ("JS-FM-01", "全局环境长期持有对象", "ROOT_VM"),
    "sourcetextmodule": ("JS-FM-01", "模块顶层长期持有对象", "ROOT_VM"),
    "source_text_module": ("JS-FM-01", "模块顶层长期持有对象", "ROOT_VM"),
    "listener": ("JS-FM-02", "事件监听器未解除", "ROOT_GLOBAL_HANDLE"),
    "timer": ("JS-FM-03", "定时器或异步任务长期持有对象", "ROOT_FRAME"),
    "promise": ("JS-FM-04", "Promise/异步链长期持有对象", "ROOT_FRAME"),
    "napi": ("JS-FM-05", "N-API 引用未释放", "ROOT_LOCAL_HANDLE"),
    "localhandle": ("JS-FM-05", "Native LocalHandle 未释放", "ROOT_LOCAL_HANDLE"),
    "frame": ("JS-FM-06", "栈帧局部变量长期持有对象", "ROOT_FRAME"),
    "function": ("JS-FM-06", "函数闭包长期持有对象", "ROOT_FRAME"),
}


def classify_heap_artifact(path: str) -> Dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {"artifact_type": "missing", "path": str(target), "confidence": 1.0}
    if target.is_dir():
        files = sorted(p for p in target.iterdir() if p.is_file())
        snapshots = [p for p in files if p.suffix.lower() in {".heapsnapshot", ".json"}]
        rawheaps = [p for p in files if p.suffix.lower() == ".rawheap"]
        kind = "heap_snapshot_series" if len(snapshots) > 1 else ("heapsnapshot" if snapshots else ("rawheap" if rawheaps else "directory"))
        return {"artifact_type": kind, "path": str(target), "files": [str(p) for p in files], "snapshot_count": len(snapshots), "confidence": 0.95}
    suffix = target.suffix.lower()
    kind = "heapsnapshot" if suffix == ".heapsnapshot" else ("rawheap" if suffix == ".rawheap" else "unknown")
    return {"artifact_type": kind, "path": str(target), "confidence": 0.95 if kind != "unknown" else 0.3}


def _node_rows(snapshot: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Tuple[int, int]]]:
    meta = snapshot.get("snapshot", {}).get("meta", {})
    node_fields = meta.get("node_fields") or ["type", "name", "id", "self_size", "edge_count", "trace_node_id"]
    node_types = meta.get("node_types", [[]])[0] if meta.get("node_types") else []
    strings = snapshot.get("strings", [])
    raw = snapshot.get("nodes", [])
    width = len(node_fields)
    rows: List[Dict[str, Any]] = []
    for offset in range(0, len(raw), width):
        values = raw[offset:offset + width]
        if len(values) < width:
            break
        row = dict(zip(node_fields, values))
        if isinstance(row.get("type"), int) and row["type"] < len(node_types):
            row["type"] = node_types[row["type"]]
        if isinstance(row.get("name"), int) and 0 <= row["name"] < len(strings):
            row["name"] = strings[row["name"]]
        row["index"] = len(rows)
        row["self_size"] = int(row.get("self_size") or 0)
        rows.append(row)
    edge_fields = meta.get("edge_fields") or ["type", "name_or_index", "to_node"]
    edge_width = len(edge_fields)
    edge_raw = snapshot.get("edges", [])
    edges: List[Tuple[int, int]] = []
    node_offset = 0
    for row in rows:
        count = int(row.get("edge_count") or 0)
        for pos in range(count):
            offset = (node_offset + pos) * edge_width
            values = edge_raw[offset:offset + edge_width]
            if len(values) < edge_width:
                break
            edge = dict(zip(edge_fields, values))
            to_node = int(edge.get("to_node") or 0)
            target = to_node // width if to_node >= width else to_node
            if 0 <= target < len(rows):
                edges.append((row["index"], target))
        node_offset += count
    return rows, edges


def _fault_mode(root_name: str, root_type: str) -> Dict[str, Any]:
    text = f"{root_name} {root_type}".lower()
    for hint, (mode, name, root_kind) in ROOT_HINTS.items():
        if hint in text:
            return {"id": mode, "name": name, "owner": "ArkTS" if mode != "JS-FM-05" else "Native", "confidence": 0.78, "evidence": [f"root_hint={hint}"], "root_kind": root_kind}
    return {"id": "JS-FM-Unknown", "name": "无法匹配标准故障模式", "owner": "Unknown", "confidence": 0.25, "evidence": ["root node type/name is insufficient"], "root_kind": "UNKNOWN"}


def _distances(rows: Sequence[Mapping[str, Any]], incoming: Mapping[int, List[int]]) -> Dict[int, int]:
    roots = [row["index"] for row in rows if not incoming.get(row["index"])]
    if not roots:
        roots = [0] if rows else []
    distance = {index: 0 for index in roots}
    queue = list(roots)
    outgoing: Dict[int, List[int]] = defaultdict(list)
    for child, parents in incoming.items():
        for parent in parents:
            outgoing[parent].append(child)
    while queue:
        current = queue.pop(0)
        for child in outgoing.get(current, []):
            if child not in distance:
                distance[child] = distance[current] + 1
                queue.append(child)
    return distance


# --- Union-Find reference chain clustering (inspired by Huawei jsleak-analysis) ---

class _UnionFind:
    """Lightweight disjoint-set for merging similar reference chains."""

    def __init__(self, n: int) -> None:
        self._parent = list(range(n))
        self._rank = [0] * n

    def find(self, x: int) -> int:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, x: int, y: int) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return
        if self._rank[rx] < self._rank[ry]:
            rx, ry = ry, rx
        self._parent[ry] = rx
        if self._rank[rx] == self._rank[ry]:
            self._rank[rx] += 1

    def groups(self) -> Dict[int, List[int]]:
        result: Dict[int, List[int]] = defaultdict(list)
        for i in range(len(self._parent)):
            result[self.find(i)].append(i)
        return result


def _extract_reference_chain(
    rows: Sequence[Mapping[str, Any]],
    node_idx: int,
    incoming: Mapping[int, List[int]],
    max_depth: int = 10,
) -> List[Dict[str, Any]]:
    """Walk incoming edges from node to root, returning the retention path."""
    chain: List[Dict[str, Any]] = []
    visited: set = set()
    current = node_idx
    for _ in range(max_depth):
        if current in visited:
            break
        visited.add(current)
        if current < len(rows):
            row = rows[current]
            chain.append({"name": row.get("name"), "type": row.get("type"), "index": current})
        parents = incoming.get(current, [])
        if not parents:
            break
        current = parents[0]  # Follow first incoming edge
    return list(reversed(chain))


def _chain_names(chain: List[Dict[str, Any]]) -> List[str]:
    """Extract normalized name sequence from a reference chain."""
    return [str(item.get("name") or item.get("type") or "") for item in chain]


def _is_contiguous_subsequence(short: List[str], long: List[str]) -> bool:
    """Check if short is a contiguous subsequence of long."""
    if not short or len(short) > len(long):
        return False
    for start in range(len(long) - len(short) + 1):
        if long[start:start + len(short)] == short:
            return True
    return False


def _chain_similarity(chain_a: List[str], chain_b: List[str]) -> float:
    """Subsequence-based similarity (Huawei approach).

    Returns 1.0 if one chain is a contiguous subsequence of the other.
    Otherwise returns LCS length / max(len_a, len_b).
    """
    if not chain_a or not chain_b:
        return 0.0
    if _is_contiguous_subsequence(chain_a, chain_b) or _is_contiguous_subsequence(chain_b, chain_a):
        return 1.0
    # LCS (dynamic programming)
    m, n = len(chain_a), len(chain_b)
    if m > 50 or n > 50:  # Guard against large chains
        return 0.0
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if chain_a[i - 1] == chain_b[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])
    lcs_len = dp[m][n]
    return lcs_len / max(m, n)


def _union_find_merge_clusters(
    clusters: List[Dict[str, Any]],
    similarity_threshold: float = 0.7,
) -> List[Dict[str, Any]]:
    """Merge clusters with similar reference chains using Union-Find.

    After merging, each group keeps the representative with the largest
    retained_size, accumulates total retained_size, and counts instances.
    """
    n = len(clusters)
    if n <= 1:
        return clusters

    # Extract chain name sequences
    chain_names_list = [_chain_names(c.get("reference_chain") or []) for c in clusters]

    uf = _UnionFind(n)
    for i in range(n):
        for j in range(i + 1, n):
            if uf.find(i) == uf.find(j):
                continue
            sim = _chain_similarity(chain_names_list[i], chain_names_list[j])
            if sim >= similarity_threshold:
                uf.union(i, j)

    # Build merged clusters
    merged: List[Dict[str, Any]] = []
    for _root, members in sorted(uf.groups().items(), key=lambda x: -len(x[1])):
        # Pick representative with largest retained_size
        rep_idx = max(members, key=lambda idx: int(clusters[idx].get("retained_size") or 0))
        rep = dict(clusters[rep_idx])
        total_retained = sum(int(clusters[idx].get("retained_size") or 0) for idx in members)
        rep["instance_count"] = len(members)
        rep["total_retained_size"] = total_retained
        rep["avg_retained_size"] = round(total_retained / len(members), 1) if members else 0
        # Use longest chain as representative
        longest_idx = max(members, key=lambda idx: len(clusters[idx].get("reference_chain") or []))
        rep["representative_chain"] = _chain_names(clusters[longest_idx].get("reference_chain") or [])
        merged.append(rep)

    merged.sort(key=lambda c: (-c.get("total_retained_size", 0), c.get("object_name", "")))
    return merged


def analyze_heap_clusters_deep(
    snapshots: List[Dict[str, Any]],
    *,
    top_n: int = 20,
    similarity_threshold: float = 0.7,
) -> Dict[str, Any]:
    """Enhanced multi-snapshot analysis with Union-Find chain clustering.

    1. Analyze each snapshot independently
    2. Extract and merge similar reference chains via Union-Find
    3. Compute cross-snapshot frequency and growth trends
    """
    snapshot_count = len(snapshots)
    if snapshot_count == 0:
        return {"status": "no_data", "snapshot_count": 0, "merged_clusters": []}

    # Analyze all snapshots
    per_snapshot: List[Dict[str, Any]] = []
    for snap in snapshots:
        result = _analyze_snapshot(snap, top_n * 2)  # Get more clusters for merging
        per_snapshot.append(result)

    # Collect all clusters across snapshots with snapshot index
    all_clusters: List[Dict[str, Any]] = []
    for snap_idx, result in enumerate(per_snapshot):
        for cluster in result.get("clusters") or []:
            enriched = dict(cluster)
            enriched["_snapshot_idx"] = snap_idx
            all_clusters.append(enriched)

    if not all_clusters:
        return {"status": "success", "snapshot_count": snapshot_count, "merged_clusters": []}

    # Merge similar clusters via Union-Find
    merged = _union_find_merge_clusters(all_clusters, similarity_threshold)

    # Compute cross-snapshot occurrence and growth
    for cluster in merged:
        # Count unique snapshots this cluster's members appeared in
        snap_indices = set()
        for idx, orig in enumerate(all_clusters):
            # Check if this original cluster matches the merged one
            if orig.get("object_name") == cluster.get("object_name") and orig.get("object_type") == cluster.get("object_type"):
                snap_indices.add(orig.get("_snapshot_idx", 0))
        cluster["occurrence_count"] = len(snap_indices)
        cluster["occurrence_ratio"] = round(len(snap_indices) / snapshot_count, 3) if snapshot_count else 0

        # Growth trend: compare retained sizes from first vs last snapshot
        first_sizes = [int(c.get("retained_size") or 0) for c in all_clusters
                       if c.get("object_name") == cluster.get("object_name") and c.get("_snapshot_idx") == 0]
        last_sizes = [int(c.get("retained_size") or 0) for c in all_clusters
                      if c.get("object_name") == cluster.get("object_name") and c.get("_snapshot_idx") == snapshot_count - 1]
        first_max = max(first_sizes) if first_sizes else 0
        last_max = max(last_sizes) if last_sizes else 0
        if last_max > first_max * 1.2:
            cluster["growth_trend"] = "growing"
        elif last_max < first_max * 0.8:
            cluster["growth_trend"] = "shrinking"
        else:
            cluster["growth_trend"] = "stable"

        # Remove internal fields
        cluster.pop("_snapshot_idx", None)

    # Remove internal fields from output
    for cluster in merged:
        cluster.pop("_incoming", None)
        cluster.pop("_rows", None)

    # Ranking views
    frequency_ranking = sorted(merged, key=lambda c: (-c.get("occurrence_ratio", 0), -c.get("total_retained_size", 0)))[:top_n]
    growth_ranking = sorted(
        [c for c in merged if c.get("growth_trend") == "growing"],
        key=lambda c: -c.get("total_retained_size", 0),
    )[:top_n]

    return {
        "status": "success",
        "snapshot_count": snapshot_count,
        "merged_clusters": merged[:top_n],
        "frequency_ranking": frequency_ranking,
        "growth_ranking": growth_ranking,
    }


def _cluster_index(clusters: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    for item in clusters:
        key = f"{item.get('object_name')}|{item.get('object_type')}"
        old = index.get(key)
        if old is None or int(item.get("retained_size") or 0) > int(old.get("retained_size") or 0):
            index[key] = dict(item)
    return index


def compare_heap_clusters(current: Sequence[Mapping[str, Any]], baseline: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    now = _cluster_index(current)
    before = _cluster_index(baseline)
    new_objects = []
    grown = []
    for key, item in now.items():
        previous = before.get(key)
        if previous is None:
            new_objects.append(item)
        elif int(item.get("retained_size") or 0) > int(previous.get("retained_size") or 0):
            grown.append({**item, "baseline_retained_size": previous.get("retained_size"), "delta": int(item.get("retained_size") or 0) - int(previous.get("retained_size") or 0)})
    new_objects.sort(key=lambda item: (-int(item.get("retained_size") or 0), item.get("object_name")))
    grown.sort(key=lambda item: (-int(item.get("delta") or 0), item.get("object_name")))
    return {"status": "success", "new_objects": new_objects[:20], "grown_objects": grown[:20], "new_count": len(new_objects), "grown_count": len(grown)}


def _analyze_snapshot(snapshot: Dict[str, Any], top_n: int) -> Dict[str, Any]:
    rows, edges = _node_rows(snapshot)
    outgoing: Dict[int, List[int]] = defaultdict(list)
    incoming: Dict[int, List[int]] = defaultdict(list)
    for source, target_index in edges:
        outgoing[source].append(target_index)
        incoming[target_index].append(source)
    distances = _distances(rows, incoming)
    clusters: List[Dict[str, Any]] = []
    for row in rows:
        retained = row["self_size"]
        stack = [row["index"]]
        seen = set(stack)
        while stack:
            current = stack.pop()
            for child in outgoing.get(current, []):
                if child not in seen:
                    seen.add(child)
                    retained += rows[child]["self_size"]
                    stack.append(child)
        if retained <= 0:
            continue
        roots = [rows[parent] for parent in incoming.get(row["index"], [])]
        root = roots[0] if roots else {}
        mode = _fault_mode(str(root.get("name") or row.get("name") or ""), str(root.get("type") or row.get("type") or ""))
        distance = distances.get(row["index"], 0)
        # Build reference chain by walking up incoming edges
        ref_chain = _extract_reference_chain(rows, row["index"], incoming)
        clusters.append({
            "cluster_id": f"node-{row['index']}",
            "object_name": str(row.get("name") or "<anonymous>"),
            "object_type": str(row.get("type") or "unknown"),
            "shallow_size": row["self_size"],
            "retained_size": retained,
            "distance": distance,
            "root_type": root.get("type") or row.get("type"),
            "root_name": root.get("name") or row.get("name"),
            "root_kind": mode.get("root_kind"),
            "reference_chain": ref_chain or [{"name": row.get("name"), "type": row.get("type"), "distance": distance}],
            "fault_mode": mode,
        })
    clusters.sort(key=lambda item: (-item["retained_size"], -item["shallow_size"], item["object_name"]))
    top = clusters[:max(1, int(top_n))]
    return {"node_count": len(rows), "edge_count": len(edges), "clusters": top, "fault_modes": [item["fault_mode"] for item in top], "_incoming": incoming, "_rows": rows}


def analyze_js_heap(path: str, *, top_n: int = 20, baseline: Optional[str] = None) -> Dict[str, Any]:
    classification = classify_heap_artifact(path)
    target = Path(path)
    files = [target] if target.is_file() else [Path(p) for p in classification.get("files", []) if str(p).endswith(".heapsnapshot")]
    files = [p for p in files if p.suffix.lower() == ".heapsnapshot"]
    if not files:
        return {"status": "unsupported", "classification": classification, "clusters": [], "fault_modes": [], "message": "需要 .heapsnapshot；.rawheap 请先通过外部 translator 转换"}
    snapshots: List[Dict[str, Any]] = []
    for file in files:
        with file.open("r", encoding="utf-8") as handle:
            snapshots.append(json.load(handle))
    current = _analyze_snapshot(snapshots[0], top_n)
    comparison: Dict[str, Any] = {"status": "not_requested"}
    baseline_path = Path(baseline) if baseline else None
    if baseline_path and baseline_path.is_file():
        with baseline_path.open("r", encoding="utf-8") as handle:
            comparison = compare_heap_clusters(current["clusters"], _analyze_snapshot(json.load(handle), top_n)["clusters"])
            comparison["baseline"] = str(baseline_path)
    elif len(snapshots) > 1:
        comparison = compare_heap_clusters(_analyze_snapshot(snapshots[-1], top_n)["clusters"], current["clusters"])
        comparison["baseline"] = str(files[0])
        comparison["current"] = str(files[-1])
    # Deep analysis with Union-Find clustering for multi-snapshot scenarios
    deep_analysis: Dict[str, Any] = {"status": "not_applicable"}
    if len(snapshots) > 1:
        deep_analysis = analyze_heap_clusters_deep(snapshots, top_n=top_n)
    # Strip internal fields from clusters before returning
    clean_clusters = []
    for c in current["clusters"]:
        clean = {k: v for k, v in c.items() if not k.startswith("_")}
        clean_clusters.append(clean)
    return {
        "status": "success",
        "classification": classification,
        "snapshot": {"path": str(files[0]), "node_count": current["node_count"], "edge_count": current["edge_count"]},
        "clusters": clean_clusters,
        "fault_modes": current["fault_modes"],
        "comparison": comparison,
        "deep_analysis": deep_analysis,
        "series": [{"path": str(file), "node_count": len(_node_rows(snapshot)[0])} for file, snapshot in zip(files, snapshots)],
    }
