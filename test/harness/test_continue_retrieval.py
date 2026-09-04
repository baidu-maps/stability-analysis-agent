import tempfile
import time
import unittest
from pathlib import Path

from services.code_evidence_index import CodeEvidenceIndex
from services.crash_evidence_retriever import CrashEvidenceRetriever
from services.context_compactor import ContextCompactor
from services.context_engine import CallableContextResolver, ContextResolverRegistry


class ContinueRetrievalTests(unittest.TestCase):
    def test_incremental_index_reuses_unchanged_file_and_updates_changed_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.cpp"
            path.write_text("void crash_target() { return; }\n", encoding="utf-8")
            index = CodeEvidenceIndex()
            first = index.update([tmp], revision="r1")
            first_fp = first.files[0]["fingerprint"]
            second = index.update([tmp], revision="r1")
            self.assertEqual(first_fp, second.files[0]["fingerprint"])
            path.write_text("void crash_target() { return; }\nvoid caller() { crash_target(); }\n", encoding="utf-8")
            third = index.update([tmp], revision="r2")
            self.assertNotEqual(first_fp, third.files[0]["fingerprint"])
            self.assertTrue(index.search("caller", mode="symbol"))

    def test_retriever_returns_provenance_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.cpp"
            path.write_text("void crash_target() { int value = 1; }\n", encoding="utf-8")
            index = CodeEvidenceIndex()
            index.update([tmp])
            values = CrashEvidenceRetriever(index).retrieve({"stack_symbols": ["crash_target"]}, limit=5)
            self.assertTrue(values)
            self.assertEqual(values[0]["provider"], "codebase_search")
            self.assertIn("cost", values[0])

    def test_compactor_tracks_token_budget_metadata(self):
        out = ContextCompactor().compact([
            {"priority": "control", "content": "JSON_CONTRACT"},
            {"priority": "history", "content": "old " * 100},
        ], max_chars=120, max_tokens=30, token_counter=lambda text: len(str(text).split()))
        self.assertIn("JSON_CONTRACT", out.text)
        self.assertIn("tokens_before", out.metadata)
        self.assertIn("tokens_after", out.metadata)

    def test_resolver_adds_provider_provenance_and_cost(self):
        registry = ContextResolverRegistry([
            CallableContextResolver("function", lambda request: {"success": True, "content": "void f() {}", "file": "a.cpp"})
        ])
        value = registry.resolve({"type": "function", "symbol": "f"})
        self.assertEqual(value["provider"], "function")
        self.assertEqual(value["provenance"]["file"], "a.cpp")
        self.assertGreater(value["cost"]["tokens"], 0)


if __name__ == "__main__":
    unittest.main()
