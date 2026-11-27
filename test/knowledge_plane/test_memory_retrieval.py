"""Knowledge retrieval as agent memory: related packs, status, usage stats, candidate fallback."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from knowledge_plane import retrieve_knowledge


def _pack(root: Path, pack_id: str, hints, related=None, globs=None) -> Path:
    pack = root / pack_id
    pack.mkdir(parents=True)
    manifest = {"id": pack_id, "match": {"stack_hints": hints}, "content_globs": globs or ["**/*.md"]}
    if related:
        manifest["related_packs"] = related
    (pack / "pack.yaml").write_text(json.dumps(manifest), encoding="utf-8")
    return pack


def _doc(pack: Path, name: str, title: str, symbol: str, extra: str = "") -> None:
    (pack / name).write_text(
        f"---\ntitle: {title}\nsymbols: [{symbol}]\n{extra}---\n\n{symbol} crashes.\n",
        encoding="utf-8",
    )


def _query(dirs, *functions, **kwargs):
    return retrieve_knowledge(
        parse_result={"signal": "SIGSEGV"},
        resolved_stack={"resolved_frames": [{"function": f} for f in functions]},
        code_context={},
        pack_dirs=[str(d) for d in dirs],
        include_env_dirs=False,
        **kwargs,
    )


class MemoryRetrievalTest(unittest.TestCase):
    def test_related_pack_documents_join_main_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main = _pack(root, "engine", ["CMap::Draw"], related=["vi"])
            vi = _pack(root, "vi", ["CThread::Run"])
            _doc(main, "a.md", "Draw crash", "CMap::Draw")
            _doc(vi, "b.md", "Thread race", "CThread::Run")
            result = _query([root], "CMap::Draw", "CThread::Run")
        # 关联是双向的：无论路由到哪个包，另一个包的文档都一起参与。
        self.assertIn(result["pack_id"], {"engine", "vi"})
        ids = {hit["memory_id"] for hit in result["hits"]}
        self.assertEqual(ids, {"engine/a.md", "vi/b.md"})

    def test_inactive_status_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack = _pack(root, "engine", ["CMap::Draw"])
            _doc(pack, "a.md", "Active", "CMap::Draw")
            _doc(pack, "b.md", "Demoted", "CMap::Draw", "status: demoted\n")
            result = _query([root], "CMap::Draw")
        self.assertEqual([hit["title"] for hit in result["hits"]], ["Active"])

    def test_usage_stats_reorder_equal_scores(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack = _pack(root, "engine", ["CMap::Draw"])
            _doc(pack, "a.md", "Rejected one", "CMap::Draw")
            _doc(pack, "b.md", "Adopted one", "CMap::Draw")
            stats = root / "stats.json"
            stats.write_text(json.dumps({
                "engine/a.md": {"adopted": 0, "rejected": 3},
                "engine/b.md": {"adopted": 4, "rejected": 0},
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, {"STABILITY_AGENT_KNOWLEDGE_STATS": str(stats)}):
                result = _query([root], "CMap::Draw")
        self.assertEqual(result["hits"][0]["title"], "Adopted one")

    def test_candidate_pool_is_used_only_when_memory_misses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = _pack(root / "memory", "engine", ["CMap::Draw"])
            _doc(memory, "a.md", "Draw crash", "CMap::Draw")
            pool = _pack(root / "candidates", "engine", ["CTile::Load"])
            _doc(pool, "c.md", "Tile load crash", "CTile::Load")
            env = {"STABILITY_AGENT_KNOWLEDGE_CANDIDATE_DIRS": str(root / "candidates")}
            with mock.patch.dict(os.environ, env):
                hit_memory = _query([root / "memory"], "CMap::Draw")
                miss_memory = _query([root / "memory"], "CTile::Load")
        self.assertNotIn("candidate_fallback", hit_memory)
        self.assertTrue(miss_memory["candidate_fallback"])
        self.assertEqual(miss_memory["hits"][0]["provenance"], "candidate")
        self.assertIn("未准入候选", miss_memory["knowledge_context"])

    def test_query_terms_extend_the_stack(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack = _pack(root, "engine", ["CMap::Draw"])
            _doc(pack, "a.md", "Draw crash", "CMap::Draw")
            result = _query([root], "start_thread", query_terms=["CMap::Draw"])
        self.assertEqual(result["hits"][0]["title"], "Draw crash")


if __name__ == "__main__":
    unittest.main()
