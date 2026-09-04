from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from services.context_observation_resolver import build_context_resolver_registry
from services.context_repo_search_resolver import GrepContextResolver, ReadFileContextResolver
from services.context_request_contract import format_context_resolution, parse_context_requests
from services.context_source_resolver import CodeContextRequestResolver


class RepoSearchContextResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        src = self.root / "src"
        src.mkdir()
        (src / "resource.cpp").write_text(
            "void Resource::release() {\n  delete impl_;\n}\n",
            encoding="utf-8",
        )
        self.code_roots = [str(self.root)]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_grep_resolver_finds_symbol(self) -> None:
        resolver = GrepContextResolver(code_roots=self.code_roots)
        out = resolver.resolve({
            "type": "grep",
            "symbol": "Resource::release",
            "reason": "find release path",
        })
        self.assertTrue(out.get("success"))
        self.assertEqual(out.get("context_type"), "grep")
        self.assertTrue(out.get("matches"))

    def test_read_file_resolver_returns_snippet(self) -> None:
        resolver = ReadFileContextResolver(code_roots=self.code_roots)
        out = resolver.resolve({
            "type": "read_file",
            "file": "src/resource.cpp",
            "line_number": 1,
            "line_end": 3,
        })
        self.assertTrue(out.get("success"))
        self.assertEqual(out.get("context_type"), "read_file")
        self.assertIn("Resource::release", "\n".join(out.get("snippet") or []))

    def test_registry_includes_repo_search_resolvers(self) -> None:
        def _code_resolver(request):
            return CodeContextRequestResolver.resolve_requests(
                [request], self.code_roots, max_requests=1,
            )[0]

        registry = build_context_resolver_registry(
            prepare={"code_roots": self.code_roots},
            problem={"code_roots": self.code_roots},
            context=None,
            trace=None,
            code_resolver=_code_resolver,
            code_roots=self.code_roots,
        )
        self.assertIn("grep", registry.request_types)
        self.assertIn("read_file", registry.request_types)

    def test_parse_context_requests_accepts_grep_and_read_file(self) -> None:
        text = """
分析中
```json
{
  "agent_can_fetch_more": true,
  "context_requests": [
    {"type": "grep", "symbol": "release", "priority": "high"},
    {"type": "read_file", "file": "src/resource.cpp", "line_number": 1}
  ]
}
```
"""
        parsed = parse_context_requests(text)
        self.assertTrue(parsed.get("agent_can_fetch_more"))
        types = {item.get("type") for item in parsed.get("context_requests") or []}
        self.assertEqual(types, {"grep", "read_file"})
        self.assertEqual(parsed.get("invalid_context_requests"), [])

    def test_format_context_resolution_grep(self) -> None:
        resolver = GrepContextResolver(code_roots=self.code_roots)
        item = resolver.resolve({"type": "grep", "symbol": "release"})
        rendered = format_context_resolution(item)
        self.assertIn("grep", rendered)
        self.assertIn("resource.cpp", rendered)


if __name__ == "__main__":
    unittest.main()
