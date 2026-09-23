import json
import tempfile
import unittest
from pathlib import Path

from knowledge_plane import retrieve_knowledge


class KnowledgePackRetrievalTest(unittest.TestCase):
    def _create_pack(self, root: Path) -> Path:
        pack = root / "demo"
        (pack / "threading").mkdir(parents=True)
        (pack / "pack.yaml").write_text(
            json.dumps(
                {
                    "id": "demo",
                    "display_name": "Test Engine",
                    "match": {
                        "so_names": ["libdemo.so"],
                        "stack_hints": ["RenderEngine::Draw"],
                    },
                    "content_globs": ["**/*.md"],
                }
            ),
            encoding="utf-8",
        )
        (pack / "threading" / "render.md").write_text(
            """---
title: Render thread lifecycle
kb_layer: code
confidence: high
symbols: [RenderEngine::Draw, glBindFramebuffer]
---

RenderEngine::Draw must stop before the framebuffer and context are released.
""",
            encoding="utf-8",
        )
        return pack

    def test_routes_pack_and_retrieves_exact_symbol(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._create_pack(root)
            result = retrieve_knowledge(
                parse_result={"signal": "SIGSEGV"},
                resolved_stack={
                    "resolved_frames": [
                        {
                            "module": "libdemo.so",
                            "function": "RenderEngine::Draw",
                        }
                    ]
                },
                code_context={},
                pack_dirs=[str(root)],
            )

        self.assertFalse(result["skipped"])
        self.assertEqual(result["pack_id"], "demo")
        self.assertEqual(len(result["hits"]), 1)
        self.assertEqual(result["hits"][0]["confidence"], "high")
        self.assertIn("Render thread lifecycle", result["knowledge_context"])
        self.assertIn("framebuffer", result["knowledge_context"])

    def test_no_matching_pack_is_non_fatal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._create_pack(root)
            result = retrieve_knowledge(
                parse_result={"signal": "SIGSEGV"},
                resolved_stack={"resolved_frames": [{"function": "Other::Run"}]},
                code_context={},
                pack_dirs=[str(root)],
            )

        self.assertTrue(result["success"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["skip_reason"], "no_matching_knowledge_pack")

    def test_nested_resolved_threads_frames_route_pack(self):
        """真实 add2line 结构是 resolved_threads[].frames[]，不得因深度截断丢符号。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._create_pack(root)
            result = retrieve_knowledge(
                parse_result={"threads": [{"is_crash_thread": True}]},
                resolved_stack={
                    "resolved_threads": [
                        {
                            "is_crash_thread": True,
                            "frames": [
                                {
                                    "module": "libdemo.so",
                                    "resolved_function": "RenderEngine::Draw",
                                    "resolved_file": "/src/RenderEngine.cpp",
                                    "resolved_line": 10,
                                }
                            ],
                        }
                    ]
                },
                code_context={},
                pack_dirs=[str(root)],
            )

        self.assertFalse(result["skipped"])
        self.assertEqual(result["pack_id"], "demo")
        self.assertIn("Render thread lifecycle", result["knowledge_context"])


if __name__ == "__main__":
    unittest.main()
