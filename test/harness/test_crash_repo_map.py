from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from services.crash_repo_map import CrashRepoMap, RepoMapSnapshot, render_repo_map


class CrashRepoMapTests(unittest.TestCase):
    def test_build_rank_and_round_trip_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "crash.cpp").write_text(
                "#include \"owner.h\"\nvoid Crash::run() { owner.reset(); }\n", encoding="utf-8"
            )
            (root / "owner.h").write_text(
                "struct Owner { void reset(); int m_value; };\n", encoding="utf-8"
            )
            (root / "tests").mkdir()
            (root / "tests" / "crash_test.cpp").write_text(
                "void CrashTest::run() { Crash::run(); }\n", encoding="utf-8"
            )
            cache = root / "cache"
            service = CrashRepoMap([str(root)], cache_dir=str(cache))
            snapshot = service.build()
            self.assertGreaterEqual(len(snapshot.files), 2)
            self.assertTrue(any(item["name"] == "Crash::run" for item in snapshot.symbols))
            entries = service.rank(snapshot, {"stack_symbols": ["Crash::run"]}, max_files=5, max_tokens=200)
            self.assertTrue(entries)
            self.assertEqual(entries[0].file, str((root / "crash.cpp").resolve()))
            self.assertIn("stack_symbol_match", entries[0].ranking_reasons)
            rendered = render_repo_map(entries)
            self.assertIn("导航摘要", rendered)
            restored = RepoMapSnapshot.from_dict(snapshot.to_dict())
            self.assertEqual(restored.fingerprint, snapshot.fingerprint)

            second = CrashRepoMap([str(root)], cache_dir=str(cache))
            second.build()
            self.assertTrue(second.cache_hit)

    def test_test_anchor_promotes_test_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src.cpp").write_text("void run() {}\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "src_test.cpp").write_text("void run_test() { run(); }\n", encoding="utf-8")
            service = CrashRepoMap([str(root)], cache_dir=str(root / "cache"))
            snapshot = service.build()
            entries = service.rank(snapshot, {"stack_symbols": ["run"], "purpose": "verification"}, max_files=5)
            self.assertTrue(entries)
            self.assertTrue(any(entry.is_test and "test_reference" in entry.ranking_reasons for entry in entries))


if __name__ == "__main__":
    unittest.main()
