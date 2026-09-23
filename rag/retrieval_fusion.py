"""规则、Code KB 与历史案例的统一排序、去重和预算裁剪。"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, Sequence, Tuple


_CONFIDENCE = {"high": 0.9, "medium": 0.7, "low": 0.4}
_SOURCE_RANK = {
    "rule": 4,
    "pack_document": 3,
    "knowledge_pack_chunk": 2,
    "internal_case": 1,
}
_PROVENANCE_RANK = {
    "code": 4,
    "code_and_curated_document": 4,
    "code_and_crash_evidence": 4,
    "verified_historical_case": 3,
    "historical_case": 2,
    "curated_playbook": 2,
    "model_inferred": 1,
}


def _confidence(hit: Dict[str, Any]) -> float:
    raw = hit.get("confidence_score")
    if isinstance(raw, (int, float)):
        return float(raw)
    return _CONFIDENCE.get(str(hit.get("confidence") or "medium").lower(), 0.6)


def _fingerprint(hit: Dict[str, Any]) -> str:
    if hit.get("pattern_id"):
        return f"pattern:{hit['pattern_id']}"
    if hit.get("pack_id") and hit.get("path"):
        return f"doc:{hit['pack_id']}:{hit['path']}"
    text = str(hit.get("excerpt") or hit.get("summary") or "")
    return "text:" + hashlib.sha1(text.strip().lower().encode("utf-8")).hexdigest()


def _normalize(
    rule_hits: Sequence[Dict[str, Any]],
    pack_hits: Sequence[Dict[str, Any]],
    vector_hits: Sequence[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    candidates: List[Dict[str, Any]] = []
    filtered: List[Dict[str, Any]] = []
    for item in rule_hits:
        payload = item.get("conclusion_payload") or {}
        candidates.append(
            {
                **item,
                "backend": "rule",
                "layer": "concept",
                "title": item.get("rule_name") or item.get("rule_id"),
                "excerpt": payload.get("hint") or payload.get("pattern") or str(payload),
            }
        )
    for item in pack_hits:
        candidates.append(dict(item))
    for item in vector_hits:
        candidates.append(
            {
                **item,
                "backend": str(item.get("source_type") or "internal_case"),
                "layer": str(item.get("kb_layer") or "crash"),
                "title": item.get("pattern_summary") or item.get("pattern_id"),
                "excerpt": item.get("crash_signature") or "",
            }
        )

    accepted: List[Dict[str, Any]] = []
    for item in candidates:
        confidence = _confidence(item)
        provenance = str(item.get("provenance") or item.get("source_type") or "")
        if confidence <= 0.4 and provenance == "model_inferred":
            filtered.append({"reason": "low_confidence_model_inferred", "hit": item})
            continue
        item["_confidence_numeric"] = confidence
        accepted.append(item)
    accepted.sort(
        key=lambda hit: (
            5
            if str(hit.get("backend") or "") == "rule"
            else _PROVENANCE_RANK.get(
                str(hit.get("provenance") or hit.get("source_type") or ""), 0
            ),
            _SOURCE_RANK.get(str(hit.get("backend") or ""), 0),
            # Pack 文档用检索分决定相关性；勿让 frontmatter confidence 盖过命中分。
            float(hit.get("score") or 0),
            float(hit.get("_confidence_numeric") or 0),
        ),
        reverse=True,
    )
    return accepted, filtered


def fuse_retrieval_hits(
    *,
    rule_hits: Sequence[Dict[str, Any]],
    pack_hits: Sequence[Dict[str, Any]],
    vector_hits: Sequence[Dict[str, Any]],
    max_chars: int = 4500,
    max_hits: int = 6,
    layer_limits: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """融合命中，返回唯一参考文本和完整审计轨迹。"""
    limits = {"code": 2, "crash": 2, "concept": 2, **(layer_limits or {})}
    # 领域 Pack 已命中时，丢掉明显不相关的通用向量案例（如无信号却召回 SIGFPE）。
    filtered_vector: List[Dict[str, Any]] = list(vector_hits or [])
    prefiltered: List[Dict[str, Any]] = []
    if pack_hits:
        kept: List[Dict[str, Any]] = []
        for item in filtered_vector:
            category = str(item.get("crash_category") or "").strip().lower()
            pattern_id = str(item.get("pattern_id") or "").strip().lower()
            signature = str(item.get("crash_signature") or "").strip().lower()
            unrelated = category in {"arithmetic"} or any(
                token in pattern_id or token in signature
                for token in ("divide_by_zero", "sigfpe", "aba_problem")
            )
            if unrelated:
                prefiltered.append({"reason": "pack_prefer_drop_unrelated_vector", "hit": item})
                continue
            kept.append(item)
        filtered_vector = kept
    candidates, filtered = _normalize(rule_hits, pack_hits, filtered_vector)
    filtered = list(prefiltered) + list(filtered)
    selected: List[Dict[str, Any]] = []
    deduplicated: List[Dict[str, Any]] = []
    seen = set()
    layer_counts: Dict[str, int] = {}
    used_chars = 0
    for hit in candidates:
        if len(selected) >= max(1, int(max_hits)):
            filtered.append({"reason": "hit_budget", "hit": hit})
            continue
        key = _fingerprint(hit)
        if key in seen:
            deduplicated.append({"reason": "duplicate", "fingerprint": key, "hit": hit})
            continue
        layer = str(hit.get("layer") or "crash")
        if layer_counts.get(layer, 0) >= int(limits.get(layer, 2)):
            filtered.append({"reason": "layer_budget", "hit": hit})
            continue
        excerpt = str(hit.get("excerpt") or "").strip()
        remaining = max(0, int(max_chars) - used_chars)
        if remaining <= 0:
            filtered.append({"reason": "character_budget", "hit": hit})
            continue
        if len(excerpt) > remaining:
            excerpt = excerpt[:remaining].rstrip() + "…"
        selected_hit = {**hit, "excerpt": excerpt}
        selected_hit.pop("_confidence_numeric", None)
        selected.append(selected_hit)
        seen.add(key)
        layer_counts[layer] = layer_counts.get(layer, 0) + 1
        used_chars += len(excerpt)

    lines: List[str] = []
    for hit in selected:
        source = hit.get("path") or hit.get("pattern_id") or hit.get("rule_id") or ""
        lines.append(
            f"- [{hit.get('layer', 'crash')}/{hit.get('backend', 'unknown')}] "
            f"{hit.get('title') or source}: {hit.get('excerpt') or ''}"
        )
    return {
        "context": "\n".join(lines).strip(),
        "selected_hits": selected,
        "filtered_hits": filtered,
        "deduplicated_hits": deduplicated,
        "budget": {
            "max_chars": max_chars,
            "max_hits": max_hits,
            "used_chars": used_chars,
            "layer_limits": limits,
            "layer_counts": layer_counts,
        },
    }
