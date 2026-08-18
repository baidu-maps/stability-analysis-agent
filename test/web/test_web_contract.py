#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Static Web shell contract tests; business correctness belongs to AI regression."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = REPO_ROOT / "web"


class WebContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
        cls.javascript = (WEB_ROOT / "app.js").read_text(encoding="utf-8")

    def test_static_entry_references_existing_assets(self) -> None:
        self.assertIn('href="/styles.css"', self.html)
        self.assertIn('src="/app.js"', self.html)
        self.assertTrue((WEB_ROOT / "styles.css").is_file())
        self.assertTrue((WEB_ROOT / "app.js").is_file())

    def test_analysis_controls_used_by_javascript_exist(self) -> None:
        html_ids = set(re.findall(r'\bid="([^"]+)"', self.html))
        js_ids = set(re.findall(r'\$\("([^"]+)"\)', self.javascript))
        self.assertFalse(js_ids - html_ids, f"JavaScript references missing DOM ids: {sorted(js_ids - html_ids)}")

    def test_web_uses_daemon_run_contract(self) -> None:
        for endpoint in (
            'fetch("/health")',
            'fetch("/runs"',
            '/events`',
            '/result`',
            '/cancel`',
            '/vector-db/commit`',
            'fetch("/web/preferences")',
        ):
            self.assertIn(endpoint, self.javascript)
        for field in ("crash_log", "library_dir", "code_roots", "scope", "apply_ai_fixes"):
            self.assertIn(field, self.javascript)

    def test_vector_db_commit_dom_exists(self) -> None:
        for dom_id in ("vectorDbCommit", "vectorDbInfo", "btnVectorDbCommit", "btnVectorDbSkip", "vectorDbCommitMsg"):
            self.assertIn(f'id="{dom_id}"', self.html)

    def test_demo_paths_reference_the_canonical_example(self) -> None:
        self.assertIn("examples/crash_cases/demo_basic/logs/mac/", self.javascript)
        self.assertIn("examples/crash_cases/demo_basic/lib/mac", self.javascript)
        self.assertIn("examples/crash_cases/demo_basic/code_dir", self.javascript)


if __name__ == "__main__":
    unittest.main()
