#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""owner_class_context 条件落盘与 graph 节点解析。"""

import unittest

from tools.owner_class_context import (
    build_class_skeleton_node,
    resolve_owner_class_from_code_context,
    should_persist_owner_class_from_crash_summary,
)


class TestOwnerClassContext(unittest.TestCase):
    def test_should_persist_requires_crash_thread(self):
        self.assertFalse(
            should_persist_owner_class_from_crash_summary(
                {
                    "selected_analysis_is_crash_thread": False,
                    "selected_analysis_confidence": "investigation_hint",
                    "attributed_crash_location_status": "unresolved_crash_thread_no_business_frame",
                }
            )
        )
        self.assertTrue(
            should_persist_owner_class_from_crash_summary(
                {
                    "selected_analysis_is_crash_thread": True,
                    "selected_analysis_confidence": "direct_crash_thread",
                    "attributed_crash_location_status": "resolved_to_business_frame",
                }
            )
        )

    def test_resolve_from_graph_node(self):
        node = build_class_skeleton_node(
            {
                "class_name": "Foo",
                "definition_file": "/tmp/Foo.cpp",
                "member_fields": ["m_x"],
                "class_body_excerpt": ["class Foo {", "};"],
                "skeleton": ["class Foo {", "  void bar();", "};"],
            }
        )
        code_ctx = {
            "crash_summary": {"owner_class_node_id": node["id"]},
            "graph": {"nodes": [node]},
        }
        resolved = resolve_owner_class_from_code_context(code_ctx)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.get("class_name"), "Foo")
        self.assertEqual(resolved.get("definition_file"), "/tmp/Foo.cpp")
        self.assertEqual(resolved.get("member_fields"), ["m_x"])
        self.assertEqual(len(resolved.get("skeleton") or []), 3)

    def test_resolve_legacy_crash_summary_blob(self):
        code_ctx = {
            "crash_summary": {
                "owner_class_context": {
                    "class_name": "Legacy",
                    "definition_file": "/tmp/L.cpp",
                    "member_fields": [],
                    "skeleton": ["class Legacy {};"],
                    "class_body_excerpt": [],
                }
            },
            "graph": {"nodes": []},
        }
        resolved = resolve_owner_class_from_code_context(code_ctx)
        self.assertEqual(resolved.get("class_name"), "Legacy")


if __name__ == "__main__":
    unittest.main()
