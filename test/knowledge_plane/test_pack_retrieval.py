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

    def test_inline_frame_symbol_retrieves_document(self):
        """内联调用者里的工程函数名参与知识检索。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pack = root / "demo"
            (pack / "threading").mkdir(parents=True)
            (pack / "pack.yaml").write_text(
                json.dumps(
                    {
                        "id": "demo",
                        "display_name": "Test Engine",
                        "match": {"stack_hints": ["CGridLayer::"]},
                        "content_globs": ["**/*.md"],
                    }
                ),
                encoding="utf-8",
            )
            (pack / "threading" / "grid.md").write_text(
                """---
title: Grid loading task lists
kb_layer: code
confidence: high
symbols: [CGridLayer::CancelTask]
---

CGridLayer::CancelTask walks the loading task lists.
""",
                encoding="utf-8",
            )
            result = retrieve_knowledge(
                parse_result={"signal": "SIGSEGV"},
                resolved_stack={
                    "resolved_frames": [
                        {
                            "function": "std::shared_ptr<CVTask>::operator->",
                            "inline_frames": [
                                {"function": "CGridLayer::CancelTask(bool)"}
                            ],
                        }
                    ]
                },
                code_context={},
                pack_dirs=[str(root)],
            )
        self.assertFalse(result["skipped"])
        self.assertIn("Grid loading task lists", result["knowledge_context"])

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

    def test_playbook_excerpt_keeps_leading_minimal_fix(self):
        """curated_playbook 从文首截取，最小修复约束不能被 900 字窗口切掉。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pack = root / "demo"
            (pack / "crash-pattern").mkdir(parents=True)
            (pack / "pack.yaml").write_text(
                json.dumps(
                    {
                        "id": "demo",
                        "display_name": "Test Engine",
                        "match": {"stack_hints": ["CVStyleScene::Unload"]},
                        "content_globs": ["**/*.md"],
                        "prompt_budget": {"max_chars": 3600},
                    }
                ),
                encoding="utf-8",
            )
            padding = "\n".join(f"padding line {index} about Unload teardown" for index in range(80))
            (pack / "crash-pattern" / "style.md").write_text(
                "---\n"
                "title: Unload teardown\n"
                "kb_layer: crash\n"
                "confidence: high\n"
                "source_type: curated_playbook\n"
                "symbols: [CVStyleScene::Unload, CVStyle::~CVStyle]\n"
                "---\n\n"
                "# Unload teardown\n\n"
                "## 本次只做最小修复\n\n"
                "1. `CVStyleScene::Unload` 用 `CVArray<StyleMapType*, StyleMapType*>`。\n"
                "2. `CVStyle::~CVStyle` 改 `m_themeLock->WLock()`。\n"
                "若上文没有 `CVStyle::~CVStyle` 完整函数体，先 context_requests。\n\n"
                f"{padding}\n",
                encoding="utf-8",
            )
            result = retrieve_knowledge(
                parse_result={"signal": "SIGSEGV"},
                resolved_stack={
                    "resolved_threads": [{
                        "frames": [{"function": "CVStyleScene::Unload() +648"}],
                    }]
                },
                code_context={},
                pack_dirs=[str(root)],
                max_chars=3600,
            )
        text = str(result.get("knowledge_context") or "")
        self.assertIn("最小修复", text)
        self.assertIn("WLock", text)
        self.assertIn("CVArray<StyleMapType*, StyleMapType*>", text)
        self.assertIn("context_requests", text)

    def test_multiline_symbols_frontmatter(self):
        """跨行 symbols 列表必须解析成完整列表，而不是拼成一个字符串。"""
        from knowledge_plane.api import _parse_frontmatter

        meta, _ = _parse_frontmatter(
            "---\ntitle: T\nsymbols: [A::Run, runTask,\n  B::Leave, C::Wait]\nconfidence: high\n---\nbody\n"
        )
        self.assertEqual(meta["symbols"], ["A::Run", "runTask", "B::Leave", "C::Wait"])
        self.assertEqual(meta["confidence"], "high")

    def test_upstream_fixed_doc_is_annotated_and_demoted(self):
        """检出已包含 fixed_in 提交时，文档降权并注明上游已修复。"""
        import subprocess

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pack = self._create_pack(root)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "a.cpp").write_text("int a;\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-qm", "fix"],
                check=True,
            )
            head = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            doc = pack / "threading" / "render.md"
            doc.write_text(
                doc.read_text(encoding="utf-8").replace(
                    "confidence: high", f"confidence: high\nfixed_in: [{head}]"
                ),
                encoding="utf-8",
            )
            kwargs = dict(
                parse_result={"signal": "SIGSEGV"},
                resolved_stack={"resolved_frames": [{"module": "libdemo.so", "function": "RenderEngine::Draw"}]},
                code_context={},
                pack_dirs=[str(root)],
            )
            plain = retrieve_knowledge(**kwargs)["hits"][0]
            fixed = retrieve_knowledge(code_roots=[str(repo)], **kwargs)["hits"][0]
            self.assertEqual(plain["upstream_fixed"], [])
            self.assertEqual(fixed["upstream_fixed"], [head])
            self.assertLess(fixed["score"], plain["score"])
            self.assertIn("上游修复提交", fixed["excerpt"])


if __name__ == "__main__":
    unittest.main()
