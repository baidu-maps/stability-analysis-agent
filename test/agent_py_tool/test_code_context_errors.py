#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest

from tools.code_context_errors import (
    NO_EXTRACTABLE_CONTEXT,
    NO_FRAMES_IN_CODE_ROOT,
    CodeContextUserError,
    build_error_payload,
    code_context_failure_message,
    code_context_has_failure,
    code_context_has_usable_code,
    code_context_skip_pipeline_message,
    pipeline_skip_metadata_code,
    user_message_for_code,
)


class TestCodeContextErrors(unittest.TestCase):
    def test_build_error_payload_fields(self):
        p = build_error_payload(NO_FRAMES_IN_CODE_ROOT, detail="test detail")
        self.assertEqual(p["error_code"], NO_FRAMES_IN_CODE_ROOT)
        self.assertEqual(p["user_message"], p["error"])
        self.assertIn("code-root", p["user_message"].lower() or p["user_message"])
        self.assertEqual(p["error_detail"], "test detail")

    def test_code_context_user_error(self):
        err = CodeContextUserError(NO_EXTRACTABLE_CONTEXT, detail="x")
        self.assertIn("C++", str(err))
        self.assertEqual(err.payload["error_code"], NO_EXTRACTABLE_CONTEXT)

    def test_failure_message_new_and_legacy(self):
        new_ctx = build_error_payload(NO_FRAMES_IN_CODE_ROOT)
        self.assertTrue(code_context_has_failure(new_ctx))
        self.assertEqual(
            code_context_failure_message(new_ctx),
            user_message_for_code(NO_FRAMES_IN_CODE_ROOT),
        )
        legacy = {"error": "没有可用的堆栈帧（所有堆栈帧都被过滤或未解析）"}
        self.assertTrue(code_context_has_failure(legacy))
        self.assertIn("代码目录", code_context_failure_message(legacy) or "")

    def test_no_failure_when_ok(self):
        self.assertFalse(code_context_has_failure({"crash_summary": {}}))
        self.assertIsNone(code_context_failure_message({"crash_summary": {}}))

    def test_usable_code_from_graph_snippet(self):
        ctx = {
            "crash_summary": {},
            "graph": {
                "nodes": [
                    {
                        "type": "function",
                        "snippet": ["void f() {", "  return;", "}"],
                    }
                ]
            },
        }
        self.assertTrue(code_context_has_usable_code(ctx))

    def test_no_usable_code_placeholder_only(self):
        ctx = {
            "crash_summary": {},
            "graph": {
                "nodes": [
                    {
                        "type": "function",
                        "snippet": ["（源码提取失败，请检查 code_roots 与 addr2line 路径是否一致）"],
                        "snippet_scope": "error",
                    }
                ]
            },
        }
        self.assertFalse(code_context_has_usable_code(ctx))

    def test_no_usable_code_top_level_error(self):
        p = build_error_payload(NO_FRAMES_IN_CODE_ROOT)
        self.assertFalse(code_context_has_usable_code(p))

    def test_usable_crash_line_code_only(self):
        ctx = {
            "crash_summary": {"crash_line_code": "ptr->foo();"},
            "graph": {"nodes": []},
        }
        self.assertTrue(code_context_has_usable_code(ctx))

    def test_pipeline_skip_gen_prompt_only_message(self):
        meta = pipeline_skip_metadata_code({"graph": {"nodes": []}}, scope="gen_prompt_only")
        self.assertEqual(meta.get("pipeline_skip_reason"), "no_usable_code")
        self.assertTrue(meta.get("pipeline_skipped"))
        msg = code_context_skip_pipeline_message({"graph": {"nodes": []}}, scope="gen_prompt_only")
        # 文案中应提示跳过 05 提示词生成
        self.assertIn("05_ai_prompt.md", msg)


if __name__ == "__main__":
    unittest.main()
