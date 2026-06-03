#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from tools._prompt_context_filter import (
    build_stack_anchor_paths,
    filter_prompt_function_records,
    match_resolved_frames_to_node_ids,
    resolve_prompt_filter_options,
)


def _rec(sig: str, tags, priority: int = 9):
    nid = f"func|/tmp/x.cpp|{sig}"
    return {
        "node": {
            "id": nid,
            "type": "function",
            "signature": sig,
            "file": "/tmp/x.cpp",
            "snippet": [f"void {sig} {{}}"],
        },
        "norm_id": nid.rstrip("{"),
        "tags": set(tags),
        "priority": priority,
        "shared_vars": {},
    }


class TestPromptContextFilter(unittest.TestCase):
    def test_weak_call_chain_excluded(self):
        records = [
            _rec("Crash::hit()", ["崩溃函数"], 0),
            _rec("Route::getLegSize()", ["调用链"], 5),
            _rec("Route::indoor()", ["调用链"], 5),
        ]
        included, index = filter_prompt_function_records(records, max_functions=12)
        sigs = [(r["node"] or {}).get("signature") for r in included]
        self.assertIn("Crash::hit()", sigs)
        self.assertNotIn("Route::getLegSize()", sigs)
        self.assertTrue(any("indoor" in line for line in index))

    def test_stack_frame_force_included(self):
        records = [
            _rec("Crash::hit()", ["崩溃函数"], 0),
            _rec("Caller::fail()", ["栈序保留", "堆栈帧"], 6),
        ]
        norm = records[1]["norm_id"]
        included, _ = filter_prompt_function_records(
            records, stack_frame_norm_ids={norm}, max_functions=12
        )
        sigs = [(r["node"] or {}).get("signature") for r in included]
        self.assertIn("Caller::fail()", sigs)

    def test_budget_caps_optional(self):
        records = [_rec("Crash::hit()", ["崩溃函数"], 0)]
        for i in range(15):
            records.append(_rec(f"Extra{i}::go()", ["上下文候选"], 9))
        included, index = filter_prompt_function_records(records, max_functions=5)
        self.assertLessEqual(len(included), 5)
        self.assertTrue(len(index) >= 10)

    def test_resolve_options_from_code_context(self):
        opts = resolve_prompt_filter_options(
            {"code_context_options": {"max_functions_in_prompt": 8}},
            None,
        )
        self.assertEqual(opts.max_functions_in_prompt, 8)

    def test_build_stack_anchor_paths_from_node_id(self):
        anchors = build_stack_anchor_paths(
            {
                "crash_summary": {
                    "node_id": "func|/tmp/walk/a/a.cpp|void Crash::hit()",
                }
            },
            None,
        )
        self.assertTrue(any("a.cpp" in a for a in anchors))


if __name__ == "__main__":
    unittest.main()
