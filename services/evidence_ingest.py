"""Stage-wise evidence ingest helpers for the harness EvidenceStore."""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from services.evidence_store import EvidenceStore


def _json_blob(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def ingest_parse(store: EvidenceStore, parse_result: Any, *, round_index: int = 0) -> None:
    if not parse_result:
        return
    store.add_dict({
        "kind": "crash_log",
        "content": _json_blob(parse_result),
        "source": "crash_log_parser",
        "layer": "fact",
        "relevance": 0.96,
        "round": round_index,
    })


def ingest_symbolize(
    store: EvidenceStore,
    resolved: Any,
    memory_maps: Optional[Any] = None,
    *,
    round_index: int = 0,
) -> None:
    if resolved:
        store.add_dict({
            "kind": "resolved_stack",
            "content": _json_blob(resolved),
            "source": "add2line_resolver",
            "layer": "fact",
            "relevance": 1.0,
            "round": round_index,
        })
    if memory_maps:
        store.add_dict({
            "kind": "memory_maps",
            "content": _json_blob(memory_maps),
            "source": "maps_extractor",
            "layer": "fact",
            "relevance": 0.9,
            "round": round_index,
        })


def ingest_diagnosis(store: EvidenceStore, crash_diagnosis: Any, *, round_index: int = 0) -> None:
    if not crash_diagnosis:
        return
    store.add_dict({
        "kind": "crash_diagnosis",
        "content": _json_blob(crash_diagnosis),
        "source": "deterministic_diagnosis",
        "layer": "inference",
        "relevance": 1.0,
        "round": round_index,
    })
    if not isinstance(crash_diagnosis, dict):
        return
    compass = crash_diagnosis.get("evidence_compass")
    if not isinstance(compass, dict):
        return
    ceiling = compass.get("confidence_ceiling")
    if ceiling is not None:
        store.add_dict({
            "kind": "evidence_compass_ceiling",
            "content": str(ceiling),
            "source": "evidence_compass",
            "layer": "inference",
            "relevance": 0.95,
            "round": round_index,
        })
    layers = compass.get("layers")
    if isinstance(layers, dict):
        for layer_name, layer_info in layers.items():
            if not isinstance(layer_info, dict):
                continue
            store.add_dict({
                "kind": f"evidence_layer:{layer_name}",
                "content": _json_blob(layer_info),
                "source": "evidence_compass",
                "layer": "inference",
                "relevance": 0.92 if layer_info.get("available") else 0.5,
                "round": round_index,
            })
    missing = compass.get("missing_evidence")
    if isinstance(missing, list):
        for item in missing:
            if not item:
                continue
            store.add_dict({
                "kind": "missing_evidence",
                "content": _json_blob(item),
                "source": "evidence_compass",
                "layer": "missing",
                "relevance": 0.88,
                "round": round_index,
            })
    note = compass.get("confidence_note_zh")
    if note:
        store.add_dict({
            "kind": "confidence_note",
            "content": str(note),
            "source": "evidence_compass",
            "layer": "inference",
            "relevance": 0.9,
            "round": round_index,
        })
    order = compass.get("analysis_order_zh")
    if order:
        store.add_dict({
            "kind": "analysis_order",
            "content": str(order),
            "source": "evidence_compass",
            "layer": "inference",
            "relevance": 0.85,
            "round": round_index,
        })
    if isinstance(layers, dict):
        pc_layer = layers.get("pc_vs_fault")
        if isinstance(pc_layer, dict):
            summary = pc_layer.get("summary_zh") or pc_layer.get("finding_zh") or pc_layer.get("summary")
            store.add_dict({
                "kind": "pc_vs_fault",
                "content": str(summary or _json_blob(pc_layer))[:4000],
                "source": "evidence_compass",
                "layer": "inference",
                "relevance": 0.93 if pc_layer.get("available") else 0.55,
                "round": round_index,
            })


def ingest_code_context(store: EvidenceStore, code_context: Any, *, round_index: int = 0) -> None:
    if not code_context:
        return
    store.add_dict({
        "kind": "source_code",
        "content": _json_blob(code_context),
        "source": "code_content_provider",
        "layer": "fact",
        "relevance": 0.98,
        "round": round_index,
    })


def ingest_memory_context(store: EvidenceStore, memory_context: str, *, round_index: int = 0) -> None:
    text = str(memory_context or "").strip()
    if not text:
        return
    store.add_dict({
        "kind": "memory",
        "content": text,
        "source": "vector_memory_retriever",
        "layer": "inference",
        "relevance": 0.8,
        "round": round_index,
    })


def ingest_pipeline_stages(
    store: EvidenceStore,
    *,
    parse_result: Any = None,
    resolved: Any = None,
    memory_maps: Any = None,
    crash_diagnosis: Any = None,
    code_context: Any = None,
    memory_context: str = "",
    round_index: int = 0,
) -> None:
    """Ingest all available pipeline artifacts (deduped by EvidenceStore)."""
    ingest_parse(store, parse_result, round_index=round_index)
    ingest_symbolize(store, resolved, memory_maps, round_index=round_index)
    ingest_diagnosis(store, crash_diagnosis, round_index=round_index)
    ingest_code_context(store, code_context, round_index=round_index)
    ingest_memory_context(store, memory_context, round_index=round_index)


def normalize_diagnosis_for_evaluation(diagnosis: Any) -> Dict[str, Any]:
    """Flatten nested 04a structure for evaluation comparisons."""
    if not isinstance(diagnosis, dict):
        return {}
    out = dict(diagnosis)
    classification = diagnosis.get("crash_classification")
    if isinstance(classification, dict):
        out.setdefault("category", classification.get("primary_pattern") or classification.get("category"))
        out.setdefault("fault_mode", classification.get("primary_pattern") or classification.get("fault_mode"))
        if classification.get("summary_zh") and not out.get("summary"):
            out["summary"] = classification.get("summary_zh")
    stack = diagnosis.get("stack_summary")
    if isinstance(stack, dict):
        out.setdefault("file", stack.get("crash_file") or stack.get("file") or stack.get("source_file"))
        out.setdefault("function", stack.get("crash_function") or stack.get("function") or stack.get("function_name"))
    compass = diagnosis.get("evidence_compass")
    if isinstance(compass, dict):
        missing = compass.get("missing_evidence")
        if isinstance(missing, list):
            out["missing_evidence"] = missing
        layers = compass.get("layers")
        if isinstance(layers, dict):
            out["evidence_layers_available"] = sum(
                1 for info in layers.values()
                if isinstance(info, dict) and info.get("available")
            )
            out["evidence_layers_total"] = len(layers)
        if compass.get("confidence_ceiling") is not None:
            out["confidence_ceiling"] = compass.get("confidence_ceiling")
    return out
