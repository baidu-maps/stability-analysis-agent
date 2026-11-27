from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from services.context_observation_resolver import (
    TraceSnippetResolver,
    VerificationLogResolver,
    supported_context_request_types_doc,
)
from services.observations import ObservationStore


class ContextObservationResolverTests(unittest.TestCase):
    def test_supported_types_doc_includes_observation_types(self):
        doc = supported_context_request_types_doc()
        self.assertIn("memory_pattern", doc)
        self.assertIn("verification_log", doc)
        self.assertIn("trace_snippet", doc)

    def test_verification_log_from_observation_store(self):
        store = ObservationStore()
        store.record(
            kind="verification",
            source="pytest",
            status="failed",
            summary="compile failed",
        )
        resolver = VerificationLogResolver(observation_store=store, verification={"status": "failed", "error": "x"})
        out = resolver.resolve({"type": "verification_log"})
        self.assertTrue(out["success"])
        self.assertIn("compile failed", "\n".join(out.get("snippet") or []))

    def test_trace_snippet_recent_failures(self):
        trace = MagicMock()
        trace.snapshot.return_value = {
            "events": [
                {"event": "tool.success", "status": "success"},
                {"event": "tool.failed", "status": "failed", "error": "timeout"},
            ],
        }
        out = TraceSnippetResolver(trace=trace).resolve({"type": "trace_snippet", "symbol": "recent"})
        self.assertTrue(out["success"])
        joined = "\n".join(out.get("snippet") or [])
        self.assertIn("tool.failed", joined)

    @patch("rag.memory_retriever.collect_memory_context")
    def test_memory_pattern_resolver(self, mock_collect):
        mock_collect.return_value = {
            "skipped": False,
            "memory_context": "规则命中: NullPtr pattern",
            "pattern_hits": [{"pattern_id": "p1"}],
        }
        from services.context_observation_resolver import MemoryPatternResolver

        out = MemoryPatternResolver(
            prepare={"parse_result": {"x": 1}, "resolved_stack": {"threads": []}},
            problem={"vector_db_path": "./vector_db"},
        ).resolve({"type": "memory_pattern", "symbol": "NullPtr"})
        self.assertTrue(out["success"])

    def test_memory_pattern_queries_knowledge_packs_without_artifacts(self):
        import json
        import tempfile
        from pathlib import Path

        from services.context_observation_resolver import MemoryPatternResolver

        with tempfile.TemporaryDirectory() as tmp:
            pack = Path(tmp) / "demo"
            pack.mkdir()
            (pack / "pack.yaml").write_text(json.dumps({"id": "demo", "match": {"stack_hints": ["CFoo::Run"]}}))
            (pack / "foo.md").write_text(
                "---\ntitle: Foo race\nsymbols: [CFoo::Run]\n---\n\nCFoo::Run races with Stop.\n"
            )
            out = MemoryPatternResolver(
                prepare={},
                problem={"knowledge_pack_dirs": [str(pack.parent)]},
            ).resolve({"type": "memory_pattern", "symbol": "CFoo::Run"})
        self.assertTrue(out["success"])
        self.assertIn("Foo race", "\n".join(out["snippet"]))


if __name__ == "__main__":
    unittest.main()
