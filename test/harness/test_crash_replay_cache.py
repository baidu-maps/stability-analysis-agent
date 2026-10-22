#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同一崩溃分析缓冲：证据层、结论层和评审复用。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.crash_replay_cache import (
    attach_review,
    find_reusable_review,
    lookup_crash_replay,
    peek_reusable_review,
    remember_crash_replay,
    replay_cached_fix,
    settle_deferred_checkout,
)


def _request(
    *,
    model: str = "Claude Sonnet 5.5",
    dirty: bool = False,
    enterprise: str = "1.5.5",
    core: str = "1.5.5",
    git_commit: str = "404f0a9",
    scope: str = "full",
    rounds: int = 3,
) -> dict:
    """构造一份最小运行记录。"""
    return {
        "crash_input": {"sha256": "abc123", "source": "content"},
        "source_identity": {"base_commit": "50b78d74f1900d8ff197a0fdd8998eba411ed1de", "source_origin": "icafe"},
        "paths": {"library_dir": "/data/symbols"},
        "effective_parameters": {
            "scope": scope,
            "prompt_mode": "analysis",
            "agent_loop": "context_loop",
            "max_agent_rounds": rounds,
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

    def test_different_model_reuses_conclusion(self) -> None:
        """大模型不同仍复用最近一次成功结论和补丁。"""
        source = Path(self._tmpdir.name) / "source"
        _write_report(source)
        remember_crash_replay(source, _request())
        hit = lookup_crash_replay(_request(model="other-model"))
        self.assertEqual(hit["level"], "conclusion")
        self.assertIn("CancelTask", hit["analysis"])
        self.assertIn("GridLayer.cpp", hit["patch_text"])

    def test_newer_failure_uses_latest_success(self) -> None:
        """较新的失败记录不挡住更早的成功结论。"""
        older = Path(self._tmpdir.name) / "older"
        _write_report(older)
        (older / "final_output.md").write_text("结论：较早的成功\n", encoding="utf-8")
        remember_crash_replay(older, _request(model="model-a"))
        newer = Path(self._tmpdir.name) / "newer"
        _write_report(newer, success_apply=False)
        (newer / "final_output.md").write_text("结论：较晚的失败\n", encoding="utf-8")
        remember_crash_replay(newer, _request(model="model-b"))
        hit = lookup_crash_replay(_request(model="model-c"))
        self.assertEqual(hit["level"], "conclusion")
        self.assertIn("较早的成功", hit["analysis"])
        self.assertNotIn("较晚的失败", hit["analysis"])
        self.assertEqual(Path(hit["source_report"]).resolve(), older.resolve())

    def test_latest_success_wins_when_several_succeed(self) -> None:
        """多次成功时用最近一次，不用最早一次。"""
        first = Path(self._tmpdir.name) / "first"
        _write_report(first)
        (first / "final_output.md").write_text("结论：第一次成功\n", encoding="utf-8")
        (first / "09_ai_fix.patch").write_text("diff first\n", encoding="utf-8")
        remember_crash_replay(first, _request(model="model-a"))
        second = Path(self._tmpdir.name) / "second"
        _write_report(second)
        (second / "final_output.md").write_text("结论：第二次成功\n", encoding="utf-8")
        (second / "09_ai_fix.patch").write_text("diff second\n", encoding="utf-8")
        remember_crash_replay(second, _request(model="model-b"))
        hit = lookup_crash_replay(_request(model="model-c"))
        self.assertEqual(hit["level"], "conclusion")
        self.assertIn("第二次成功", hit["analysis"])
        self.assertIn("diff second", hit["patch_text"])
        self.assertEqual(Path(hit["source_report"]).resolve(), second.resolve())

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

    def test_full_reuses_prompt_only_evidence(self) -> None:
        """全流程复用只生成提示词的定位结果，不把它当成结论。"""
        source = Path(self._tmpdir.name) / "prompt-only"
        _write_report(source)
        (source / "final_output.md").write_text("", encoding="utf-8")
        (source / "01_crash_log_parser.json").write_text(
            '{"parse_status": "ok"}\n',
            encoding="utf-8",
        )
        remember_crash_replay(source, _request(scope="gen_prompt_only", rounds=1))
        hit = lookup_crash_replay(_request(scope="full", rounds=5))
        self.assertEqual(hit["level"], "evidence")
        self.assertEqual(hit["evidence"]["parse_result"]["parse_status"], "ok")
        self.assertNotIn("analysis", hit)

    def test_narrower_scope_does_not_reuse_full_conclusion(self) -> None:
        """只生成提示词不套用全流程的大模型结论。"""
        source = Path(self._tmpdir.name) / "full-run"
        _write_report(source)
        remember_crash_replay(source, _request(scope="full", rounds=5))
        hit = lookup_crash_replay(_request(scope="gen_prompt_only", rounds=1))
        self.assertEqual(hit["level"], "evidence")

    def test_different_rounds_reuse_evidence_not_conclusion(self) -> None:
        """轮数不同时仍复用定位，但不复用上次结论。"""
        source = Path(self._tmpdir.name) / "full-run"
        _write_report(source)
        remember_crash_replay(source, _request(scope="full", rounds=3))
        hit = lookup_crash_replay(_request(scope="full", rounds=5))
        self.assertEqual(hit["level"], "evidence")

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

    def test_review_reused_when_model_differs(self) -> None:
        """评审链接不看产出结论的大模型。"""
        source = Path(self._tmpdir.name) / "source"
        _write_report(source)
        remember_crash_replay(source, _request(model="model-a"))
        attach_review(
            source,
            {
                "review_url": "http://icode.example/reviews/2",
                "creator": "liuhong_cd",
                "repo": "baidu/mapclient/engine-dev",
                "branch": "v20.15.0",
                "commit": "b935229",
            },
        )
        current = Path(self._tmpdir.name) / "current"
        _write_report(current)
        remember_crash_replay(current, _request(model="model-b"))
        found = find_reusable_review(
            current,
            creator="liuhong_cd",
            repo="baidu/mapclient/engine-dev",
            branch="v20.15.0",
        )
        self.assertEqual(found["review_url"], "http://icode.example/reviews/2")

    def test_peek_reusable_review_before_current_report(self) -> None:
        """复用评审的判断不依赖本次报告已经落盘。"""
        source = Path(self._tmpdir.name) / "source"
        _write_report(source)
        remember_crash_replay(source, _request())
        attach_review(
            source,
            {
                "review_url": "http://icode.example/reviews/9",
                "creator": "liuhong_cd",
                "repo": "baidu/mapclient/engine-vi",
                "branch": "v20.15.0",
                "commit": "b935229",
            },
        )
        cache = json.loads((source / "12_crash_replay_cache.json").read_text(encoding="utf-8"))
        patch = (source / "09_ai_fix.patch").read_text(encoding="utf-8")
        found = peek_reusable_review(
            str(cache.get("evidence_key") or ""),
            patch,
            creator="liuhong_cd",
            code_roots=["/data/engine-dev"],
            repos=["baidu/mapclient/engine-vi"],
            branches=["v20.15.0"],
        )
        self.assertEqual(found["review_url"], "http://icode.example/reviews/9")
        self.assertIsNone(
            peek_reusable_review(
                str(cache.get("evidence_key") or ""),
                patch,
                creator="other",
                code_roots=["/data/engine-dev"],
                repos=["baidu/mapclient/engine-vi"],
                branches=["v20.15.0"],
            )
        )

    def test_settle_keeps_patch_or_discards(self) -> None:
        """对得上评审就留下补丁，对不上就复位。"""
        source = Path(self._tmpdir.name) / "source"
        _write_report(source)
        remember_crash_replay(source, _request())
        attach_review(
            source,
            {
                "review_url": "http://icode.example/reviews/9",
                "creator": "liuhong_cd",
                "repo": "baidu/mapclient/engine-vi",
                "branch": "v20.15.0",
                "commit": "b935229",
            },
        )
        cache = json.loads((source / "12_crash_replay_cache.json").read_text(encoding="utf-8"))
        patch = (source / "09_ai_fix.patch").read_text(encoding="utf-8")
        hit = {
            "level": "conclusion",
            "evidence_key": cache.get("evidence_key"),
            "patch_text": patch,
        }
        os.environ["BD_SA_DEFER_DIRTY_RESET"] = "1"
        os.environ["BD_SA_REVIEW_CREATOR"] = "liuhong_cd"
        os.environ["BD_SA_REVIEW_REPOS"] = json.dumps(["baidu/mapclient/engine-vi"])
        os.environ["BD_SA_REVIEW_BRANCHES"] = json.dumps(["v20.15.0"])
        self.assertTrue(settle_deferred_checkout(hit, ["/data/engine-dev"]))
        self.assertEqual(os.environ.get("BD_SA_KEEP_APPLIED_PATCH"), "1")
        os.environ["BD_SA_DEFER_DIRTY_RESET"] = "1"
        hit["patch_text"] = "different patch\n"
        from unittest.mock import patch as mock_patch
        with mock_patch(
            "services.git_worktree_manager.discard_deferred_checkout",
        ) as discard:
            self.assertFalse(settle_deferred_checkout(hit, ["/data/engine-dev"]))
        discard.assert_called_once()
        self.assertNotEqual(os.environ.get("BD_SA_KEEP_APPLIED_PATCH"), "1")

    def test_reused_source_keeps_patch_but_not_another_creators_review(self) -> None:
        """共享检出已有补丁时继承补丁，不继承别人的评审链接和创建人。"""
        source = Path(self._tmpdir.name) / "source"
        _write_report(source)
        remember_crash_replay(source, _request())
        attach_review(
            source,
            {
                "review_url": "http://icode.example/+/122909085",
                "creator": "liuhong_cd",
                "repo": "baidu/mapclient/engine-dev",
                "branch": "v20.15.0",
                "commit": "b935229",
            },
        )
        current = Path(self._tmpdir.name) / "current"
        _write_report(current)
        (current / "08_apply_ai_fixes.json").unlink()
        (current / "09_ai_fix.patch").unlink()
        request = _request()
        request["crash_replay"] = {
            "level": "conclusion",
            "source_report": str(source),
        }
        remember_crash_replay(current, request)
        cache = json.loads((current / "12_crash_replay_cache.json").read_text(encoding="utf-8"))
        source_cache = json.loads((source / "12_crash_replay_cache.json").read_text(encoding="utf-8"))
        self.assertEqual(cache["patch_sha256"], source_cache["patch_sha256"])
        self.assertTrue(cache["has_patch"])
        self.assertTrue(cache["apply_success"])
        self.assertFalse(str((cache.get("review") or {}).get("review_url") or "").strip())
        self.assertNotEqual((cache.get("review") or {}).get("creator"), "liuhong_cd")
        self.assertIsNone(
            find_reusable_review(
                current,
                creator="hexiaojing",
                repo="baidu/mapclient/engine-dev",
                branch="v20.15.0",
            )
        )
        self.assertEqual(
            find_reusable_review(
                current,
                creator="liuhong_cd",
                repo="baidu/mapclient/engine-dev",
                branch="v20.15.0",
            )["review_url"],
            "http://icode.example/+/122909085",
        )

    def test_same_creator_reuses_own_review_only(self) -> None:
        """同一人再次触发命中自己上次的评审，换一个人则不命中。"""
        first = Path(self._tmpdir.name) / "first"
        _write_report(first)
        remember_crash_replay(first, _request())
        attach_review(
            first,
            {
                "review_url": "http://icode.example/+/122909085",
                "creator": "hexiaojing",
                "repo": "baidu/mapclient/engine-dev",
                "branch": "v20.15.0",
                "commit": "abc",
            },
        )
        again = Path(self._tmpdir.name) / "again"
        _write_report(again)
        remember_crash_replay(again, _request())
        own = find_reusable_review(
            again,
            creator="hexiaojing",
            repo="baidu/mapclient/engine-dev",
            branch="v20.15.0",
        )
        other = find_reusable_review(
            again,
            creator="liuhong_cd",
            repo="baidu/mapclient/engine-dev",
            branch="v20.15.0",
        )
        self.assertEqual(own["review_url"], "http://icode.example/+/122909085")
        self.assertIsNone(other)

    def test_keep_applied_patch_writes_apply_artifact(self) -> None:
        """补丁已在工作区时，把上次改动文件写成成功的 08。"""
        report = Path(self._tmpdir.name) / "kept"
        report.mkdir()
        previous = {
            "success": True,
            "applied": [{"status": "applied", "file": "GridLayer.cpp"}],
        }
        os.environ["BD_SA_KEEP_APPLIED_PATCH"] = "1"
        try:
            with patch(
                "services.git_worktree_manager._apply_in_place_requested",
                return_value=True,
            ), patch(
                "services.git_worktree_manager.adopt_prepared_checkout",
                return_value=object(),
            ), patch(
                "services.git_worktree_manager.write_workspace_artifacts",
            ):
                result = replay_cached_fix(
                    run_id="run-1",
                    code_roots=["/tmp/src"],
                    patch_text="diff --git a/GridLayer.cpp b/GridLayer.cpp\n",
                    previous_apply=previous,
                    report_dir=report,
                    reuse_source_cache=True,
                )
        finally:
            os.environ.pop("BD_SA_KEEP_APPLIED_PATCH", None)
        self.assertTrue(result["success"])
        self.assertTrue(result["kept_applied_patch"])
        self.assertEqual(result["applied"][0]["file"], "GridLayer.cpp")
        written = json.loads((report / "08_apply_ai_fixes.json").read_text(encoding="utf-8"))
        self.assertTrue(written["success"])
        self.assertTrue(written["kept_applied_patch"])
        self.assertEqual(written["applied"][0]["status"], "applied")
        self.assertEqual(written["applied"][0]["file"], "GridLayer.cpp")


if __name__ == "__main__":
    unittest.main()
