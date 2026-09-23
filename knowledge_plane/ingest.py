"""将 Knowledge Pack 文档幂等写入现有 Stability Memory 向量库。"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Sequence

from .api import _iter_pack_manifests, resolve_pack_dirs
from .chunk import iter_pack_chunks


_CONFIDENCE_SCORE = {"high": 0.9, "medium": 0.7, "low": 0.4}


def ingest_pack_dirs(
    *,
    vector_db_path: str,
    pack_dirs: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    from rag.vector_database_integration import AIStabilityAnalyzerWithVectorDB

    analyzer = AIStabilityAnalyzerWithVectorDB(vector_db_path=vector_db_path)
    roots = resolve_pack_dirs(pack_dirs)
    stats: Dict[str, Any] = {
        "success": True,
        "vector_db_path": vector_db_path,
        "pack_dirs": [str(path) for path in roots],
        "packs": 0,
        "chunks": 0,
        "failed": [],
    }
    for pack_root, manifest in _iter_pack_manifests(roots):
        stats["packs"] += 1
        pack_source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
        for chunk in iter_pack_chunks(pack_root, manifest):
            metadata = chunk["metadata"]
            confidence = str(metadata.get("confidence") or "medium").lower()
            symbols = metadata.get("symbols") or []
            if isinstance(symbols, str):
                symbols = [symbols]
            pattern = {
                "pattern_id": chunk["chunk_id"],
                "pattern_summary": f"{chunk['title']}\n{chunk['body']}",
                "crash_signature": " ".join(str(item) for item in symbols),
                "platform_scope": {"platforms": metadata.get("platforms") or []},
                "crash_category": str(metadata.get("domain") or "codebase"),
                "evidence_requirements": ["codebase_knowledge"],
                "confidence_score": _CONFIDENCE_SCORE.get(confidence, 0.6),
                "validation_state": "verified" if confidence == "high" else "draft",
                "source_type": "knowledge_pack_chunk",
                "pack_id": chunk["pack_id"],
                "doc_path": chunk["doc_path"],
                "kb_layer": str(metadata.get("kb_layer") or "code"),
                "provenance": str(metadata.get("source_type") or "curated_playbook"),
                "codebase_id": str(pack_source.get("codebase_id") or chunk["pack_id"]),
                "source_commit": str(metadata.get("source_commit") or pack_source.get("source_commit") or ""),
                "engine_version": str(metadata.get("engine_version") or ""),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            if not analyzer.add_pattern(pattern):
                stats["failed"].append(chunk["chunk_id"])
                continue
            analyzer.add_evidence(
                {
                    "pattern_id": chunk["chunk_id"],
                    "evidence_type": "knowledge_pack_chunk",
                    "raw_content": chunk["body"],
                    "normalized_features": {
                        "pack_id": chunk["pack_id"],
                        "doc_path": chunk["doc_path"],
                        "symbols": symbols,
                    },
                    "reliability_score": pattern["confidence_score"],
                }
            )
            stats["chunks"] += 1
    stats["success"] = not stats["failed"]
    return stats


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Index Knowledge Packs into the existing vector DB")
    parser.add_argument("--vector-db-path", default="./vector_db")
    parser.add_argument("--pack-dir", action="append", default=[])
    args = parser.parse_args(argv)
    result = ingest_pack_dirs(vector_db_path=args.vector_db_path, pack_dirs=args.pack_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
