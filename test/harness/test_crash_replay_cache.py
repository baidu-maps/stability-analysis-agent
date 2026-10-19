#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同一崩溃分析缓冲：证据层、结论层和评审复用。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from services.crash_replay_cache import (
    attach_review,
    find_reusable_review,
    lookup_crash_replay,
    remember_crash_replay,
)


def _request(
    *,
    model: str = "Claude Sonnet 5.5",
    dirty: bool = False,
    enterprise: str = "1.5.5",
    core: str = "1.5.5",
    git_commit: str = "404f0a9",
) -> dict:
    """构造一份最小运行记录。"""
    return {
        "crash_input": {"sha256": "abc123", "source": "content"},
        "source_identity": {"base_commit": "50b78d74f1900d8ff197a0fdd8998eba411ed1de", "source_origin": "icafe"},
        "paths": {"library_dir": "/data/symbols"},
        "effective_parameters": {
            "scope": "full",
            "prompt_mode": "analysis",
            "agent_loop": "context_loop",
            "max_agent_rounds": 3,
            "max_context_requests_per_round": 8,
            "domain_knowledge": True,
            "knowledge_pack_id": "",
        },
        "runtime": {
            "core_version": core,
            "enterprise_version": enterprise,
            "git": {"commit": git_commit, "dirty": dirty},
        },
        "llm": {
            "provider": "oneapi",
            "model": model,
            "request_format": "anthropic_messages_compatible",
            "temperature": 0.7,
            "max_tokens": 4096,
            "mode": "fixed",
            "llm_profile": None,
            "endpoint": "https://example/v1/messages",
        },
    }


def _write_report(root: Path, *, success_apply: bool = True) -> None:
    """写齐缓冲要求的产物。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "01_crash_log_parser.json").write_text('{"ok": true}\n', encoding="utf-8")
    (root / "03_add2line_resolver.json").write_text('{"frames": []}\n', encoding="utf-8")
    (root / "04b_code_content_provider.json").write_text('{"graph": {}}\n', encoding="utf-8")
    (root / "04a_crash_diagnosis.json").write_text('{"summary": "race"}\n', encoding="utf-8")
    (root / "final_output.md").write_text("结论：CancelTask 缺锁\n", encoding="utf-8")
    (root / "00_run_summary.json").write_text('{"status": "success"}\n', encoding="utf-8")
    apply = {"success": success_apply, "applied": [{"path": "GridLayer.cpp"}]}
    (root / "08_apply_ai_fixes.json").write_text(json.dumps(apply), encoding="utf-8")
    (root / "09_ai_fix.patch").write_text("diff --git a/GridLayer.cpp b/GridLayer.cpp\n", encoding="utf-8")


class CrashReplayCacheTests(unittest.TestCase):
    """不访问模型和 git 的缓冲匹配。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.index = Path(self._tmpdir.name) / "index.jsonl"
        os.environ["STABILITY_AGENT_CRASH_REPLAY_INDEX"] = str(self.index)

    def tearDown(self) -> None:
        os.environ.pop("STABILITY_AGENT_CRASH_REPLAY_INDEX", None)
        self._tmpdir.cleanup()

    def test_same_model_reuses_conclusion_and_patch(self) -> None:
        """大模型配置一致时复用结论，并带上上次补丁。"""
        source = Path(self._tmpdir.name) / "source"
        _write_report(source)
        remember_crash_replay(source, _request())
        hit = lookup_crash_replay(_request())
        self.assertIsNotNone(hit)
        self.assertEqual(hit["level"], "conclusion")
        self.assertIn("CancelTask", hit["analysis"])
        self.assertIn("GridLayer.cpp", hit["patch_text"])

    def test_different_model_stops_before_llm(self) -> None:
        """大模型不同时只复用模型之前的文件。"""
        source = Path(self._tmpdir.name) / "source"
        _write_report(source)
        remember_crash_replay(source, _request())
        hit = lookup_crash_replay(_request(model="other-model"))
        self.assertEqual(hit["level"], "evidence")
        self.assertIn("parse_result", hit["evidence"])
        self.assertIn("code_context", hit["evidence"])
        self.assertNotIn("patch_text", hit)

    def test_open_source_git_state_still_hits(self) -> None:
        """开源仓库提交不同或有未提交改动时仍可复用。"""
        source = Path(self._tmpdir.name) / "source"
        _write_report(source)
        remember_crash_replay(source, _request(dirty=True, git_commit="aaaa"))
        hit = lookup_crash_replay(_request(dirty=False, git_commit="bbbb"))
        self.assertEqual(hit["level"], "conclusion")

    def test_enterprise_version_mismatch_misses(self) -> None:
        """闭源 Agent 版本不同不能复用。"""
        source = Path(self._tmpdir.name) / "source"
        _write_report(source)
        remember_crash_replay(source, _request(enterprise="1.5.5"))
        self.assertIsNone(lookup_crash_replay(_request(enterprise="1.5.6")))

    def test_core_version_difference_still_hits(self) -> None:
        """开源核心版本不同仍可复用。"""
        source = Path(self._tmpdir.name) / "source"
        _write_report(source)
        remember_crash_replay(source, _request(core="1.5.5"))
        hit = lookup_crash_replay(_request(core="1.5.6"))
        self.assertEqual(hit["level"], "conclusion")

    def test_failed_apply_is_not_a_conclusion(self) -> None:
        """改码失败的运行不能当作可直接应用的结论。"""
        source = Path(self._tmpdir.name) / "source"
        _write_report(source, success_apply=False)
        remember_crash_replay(source, _request())
        hit = lookup_crash_replay(_request())
        self.assertEqual(hit["level"], "evidence")

    def test_review_reused_only_for_same_creator_and_patch(self) -> None:
        """评审链接要求创建者、仓库、分支和补丁一致。"""
        source = Path(self._tmpdir.name) / "source"
        _write_report(source)
        remember_crash_replay(source, _request())
        attach_review(
            source,
            {
                "review_url": "http://icode.example/reviews/1",
                "creator": "liuhong_cd",
                "repo": "baidu/mapclient/engine-dev",
                "branch": "v20.15.0",
                "commit": "b935229",
            },
        )
        current = Path(self._tmpdir.name) / "current"
        _write_report(current)
        remembered = remember_crash_replay(current, _request())
        self.assertTrue(remembered is not None)
        found = find_reusable_review(
            current,
            creator="liuhong_cd",
            repo="baidu/mapclient/engine-dev",
            branch="v20.15.0",
        )
        self.assertEqual(found["review_url"], "http://icode.example/reviews/1")
        self.assertIsNone(
            find_reusable_review(
                current,
                creator="other",
                repo="baidu/mapclient/engine-dev",
                branch="v20.15.0",
            )
        )


if __name__ == "__main__":
    unittest.main()
