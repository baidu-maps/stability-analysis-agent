#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""共享变量赋值点优先扫描与关系分类单测。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from services.code_locator import CodeLocatorService, LocatorConfig
from tools._prompt_context_filter import _MUST_TAGS, _record_bucket


class TestSharedVarStoreSites(unittest.TestCase):
    """验证 assign 分类、store-site 优先与 prompt must 标签。"""

    def test_determine_variable_relation_assign_vs_write_vs_read(self):
        loc = CodeLocatorService(LocatorConfig()).variable_locator
        self.assertEqual(
            loc.determine_variable_relation("pclThis->m_pclCurRoute = V_NULL;", "m_pclCurRoute"),
            "assign",
        )
        self.assertEqual(
            loc.determine_variable_relation("m_pclCurRoute = pclRoute;", "m_pclCurRoute"),
            "assign",
        )
        self.assertEqual(
            loc.determine_variable_relation("routes = m_pclCurRoute->GetRenderSteps();", "m_pclCurRoute"),
            "read",
        )
        self.assertEqual(
            loc.determine_variable_relation("m_pclCurRoute->m_flag = 1;", "m_pclCurRoute"),
            "write",
        )
        self.assertEqual(
            loc.determine_variable_relation("delete m_pclCurRoute;", "m_pclCurRoute"),
            "delete",
        )
        self.assertEqual(
            loc.determine_variable_relation("if (m_pclCurRoute == V_NULL)", "m_pclCurRoute"),
            "read",
        )

    def test_store_site_scan_keeps_assign_over_many_reads(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cpp = root / "engine_control.cpp"
            # 大量读在前，赋值在后；旧逻辑早停会丢写路径
            read_funcs = []
            for i in range(30):
                read_funcs.append(
                    f"void Reader{i}() {{\n"
                    f"    if (m_pclCurRoute) {{\n"
                    f"        (void)m_pclCurRoute->GetSize();\n"
                    f"    }}\n"
                    f"}}\n"
                )
            cpp.write_text(
                "\n".join(read_funcs)
                + "\n"
                + "void HandleRemoveRouteMessage(Engine* pclThis) {\n"
                + "    pclThis->m_pclCurRoute = V_NULL;\n"
                + "}\n"
                + "void GetRenderSteps() {\n"
                + "    if (m_pclCurRoute) {\n"
                + "        routes = m_pclCurRoute->GetRenderSteps();\n"
                + "    }\n"
                + "}\n",
                encoding="utf-8",
            )
            svc = CodeLocatorService(LocatorConfig(max_shared_var_related_functions=12))
            hits = svc.find_variable_usages(
                ["m_pclCurRoute"],
                "GetRenderSteps",
                [str(root)],
                crash_local_files=[str(cpp)],
                owner_class="Engine",
                owner_member_fields=["m_pclCurRoute"],
                owner_definition_files=[str(cpp)],
            )
            rels = {(h.name, h.relation) for h in hits}
            self.assertTrue(
                any(rel == "assign" and "HandleRemoveRouteMessage" in name for name, rel in rels),
                f"expected assign hit for HandleRemoveRouteMessage, got {rels}",
            )
            # 截断后仍应保留至少一条写类
            self.assertTrue(any(h.relation in ("assign", "write", "delete") for h in hits))

    def test_shared_var_write_is_must_tag_for_prompt(self):
        self.assertIn("共享变量写", _MUST_TAGS)
        rec = {
            "norm_id": "func|x|HandleRemoveRouteMessage",
            "tags": {"共享变量写"},
            "node": {
                "signature": "void HandleRemoveRouteMessage()",
                "file": "/tmp/a.cpp",
                "snippet": ["void HandleRemoveRouteMessage() {", "}"],
            },
        }
        self.assertEqual(
            _record_bucket(
                rec,
                stack_norm=set(),
                root_cause_norm=set(),
                anchor_paths=set(),
            ),
            "must",
        )

    def test_multiline_function_head_plausible(self):
        from tools.function_snippet_utils import is_plausible_function_code_head

        code = (
            "VVoid CNaviEngineControl::HandleRemoveRouteMessage(\n"
            "                                CNaviEngineControl* pclThis ,\n"
            "                                const NC_RemoveRoute_Message_t& stMessage )\n"
            "{\n"
            "    pclThis->m_pclCurRoute = V_NULL;\n"
            "}\n"
        )
        self.assertTrue(is_plausible_function_code_head(code))


if __name__ == "__main__":
    unittest.main()
