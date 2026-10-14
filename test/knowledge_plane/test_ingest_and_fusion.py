import json
import tempfile
import unittest
from pathlib import Path

from knowledge_plane.ingest import ingest_pack_dirs
from rag.memory_retriever import collect_memory_context
from rag.retrieval_fusion import format_pack_knowledge_context, fuse_retrieval_hits
from rag.vector_database_integration import AIStabilityAnalyzerWithVectorDB


class KnowledgeIngestAndFusionTest(unittest.TestCase):
    def test_pack_ingest_is_idempotent_and_filterable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            packs = root / "packs"
            pack = packs / "demo"
            pack.mkdir(parents=True)
            (pack / "pack.yaml").write_text(
                json.dumps({"id": "demo", "content_globs": ["*.md"]}), encoding="utf-8"
            )
            (pack / "render.md").write_text(
                """---
title: Render lifecycle
kb_layer: code
domain: render
confidence: high
source_type: code
symbols: [RenderEngine::Draw]
---
# Render lifecycle
RenderEngine::Draw stops before context release.
""",
                encoding="utf-8",
            )
            db = root / "db"
            first = ingest_pack_dirs(vector_db_path=str(db), pack_dirs=[str(packs)])
            second = ingest_pack_dirs(vector_db_path=str(db), pack_dirs=[str(packs)])
            analyzer = AIStabilityAnalyzerWithVectorDB(vector_db_path=str(db))
            hits = analyzer.retrieve_patterns(
                "RenderEngine::Draw context release",
                filters={"source_type": "knowledge_pack_chunk"},
            )
        self.assertTrue(first["success"])
        self.assertEqual(first["chunks"], second["chunks"])
        self.assertTrue(hits)
        self.assertEqual(hits[0]["pack_id"], "demo")

    def test_fusion_filters_low_model_and_deduplicates(self):
        pack_hit = {
            "backend": "pack_document",
            "layer": "code",
            "pack_id": "demo",
            "path": "render.md",
            "title": "Render",
            "excerpt": "verified",
            "confidence": "high",
            "provenance": "code",
        }
        result = fuse_retrieval_hits(
            rule_hits=[],
            pack_hits=[pack_hit, dict(pack_hit)],
            vector_hits=[
                {
                    "pattern_id": "low",
                    "pattern_summary": "guess",
                    "confidence_score": 0.4,
                    "provenance": "model_inferred",
                }
            ],
        )
        self.assertEqual(len(result["selected_hits"]), 1)
        self.assertEqual(len(result["deduplicated_hits"]), 1)
        self.assertEqual(result["filtered_hits"][0]["reason"], "low_confidence_model_inferred")

    def test_fusion_prefers_code_then_verified_history(self):
        result = fuse_retrieval_hits(
            rule_hits=[],
            pack_hits=[
                {
                    "backend": "pack_document",
                    "layer": "code",
                    "pack_id": "demo",
                    "path": "code.md",
                    "title": "code",
                    "excerpt": "code",
                    "confidence": "high",
                    "provenance": "code",
                },
                {
                    "backend": "pack_document",
                    "layer": "concept",
                    "pack_id": "demo",
                    "path": "playbook.md",
                    "title": "playbook",
                    "excerpt": "playbook",
                    "confidence": "high",
                    "provenance": "curated_playbook",
                },
            ],
            vector_hits=[
                {
                    "pattern_id": "verified",
                    "pattern_summary": "verified",
                    "confidence_score": 0.8,
                    "source_type": "internal_case",
                    "provenance": "verified_historical_case",
                }
            ],
        )
        self.assertEqual(
            [item["title"] for item in result["selected_hits"]],
            ["code", "verified", "playbook"],
        )

    def test_symbol_matched_pack_doc_fills_code_slot_before_overview(self):
        """函数符号命中应占住 code 层名额，而不是让更高分的路径概览挤掉它。"""
        def pack(title, path, score, terms):
            return {
                "backend": "pack_document",
                "layer": "code",
                "pack_id": "engine-dev",
                "path": path,
                "title": title,
                "excerpt": title,
                "confidence": "high",
                "provenance": "code",
                "score": score,
                "matched_terms": terms,
            }

        result = fuse_retrieval_hits(
            rule_hits=[],
            pack_hits=[
                pack("概览", "overview.md", 10.0, ["vmap", "engine-dev"]),
                pack("销毁顺序", "teardown.md", 10.0, ["CVMapControl::RemoveMapControl"]),
                pack("加载任务链表", "grid.md", 9.5, ["CGridLayer::CancelTask"]),
            ],
            vector_hits=[],
            layer_limits={"code": 2, "crash": 2, "concept": 2},
        )
        titles = [item["title"] for item in result["selected_hits"]]
        self.assertEqual(titles, ["销毁顺序", "加载任务链表"])
        self.assertIn("加载任务链表", format_pack_knowledge_context(result["selected_hits"]))
        dropped = [item["hit"]["title"] for item in result["filtered_hits"]]
        self.assertIn("概览", dropped)

    def test_domain_switch_excludes_pack_vectors(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "db"
            analyzer = AIStabilityAnalyzerWithVectorDB(vector_db_path=str(db))
            analyzer.add_pattern(
                {
                    "pattern_id": "pack",
                    "pattern_summary": "RenderEngine Draw lifecycle",
                    "crash_signature": "RenderEngine::Draw",
                    "confidence_score": 0.9,
                    "validation_state": "verified",
                    "source_type": "knowledge_pack_chunk",
                }
            )
            analyzer.add_pattern(
                {
                    "pattern_id": "case",
                    "pattern_summary": "RenderEngine Draw historical crash",
                    "crash_signature": "RenderEngine::Draw",
                    "confidence_score": 0.8,
                    "validation_state": "verified",
                    "source_type": "internal_case",
                }
            )
            result = collect_memory_context(
                parse_result={"crash_info": {"signal": "SIGSEGV"}},
                resolved_stack={"resolved_frames": [{"function": "RenderEngine::Draw"}]},
                code_context={},
                vector_db_path=str(db),
                vector_db_max_results=5,
                include_domain_knowledge=False,
            )
        self.assertNotIn(
            "knowledge_pack_chunk",
            {item.get("source_type") for item in result["pattern_hits"]},
        )
        self.assertIn("domain_knowledge", [item.get("stage") for item in result["decision_trace"]])


if __name__ == "__main__":
    unittest.main()
