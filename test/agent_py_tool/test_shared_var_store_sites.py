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
            loc.determine_variable_relation("pclThis->m_pCurrentItem = nullptr;", "m_pCurrentItem"),
            "assign",
        )
        self.assertEqual(
            loc.determine_variable_relation("m_pCurrentItem = pclRoute;", "m_pCurrentItem"),
            "assign",
        )
        self.assertEqual(
            loc.determine_variable_relation("routes = m_pCurrentItem->GetDrawSteps();", "m_pCurrentItem"),
            "read",
        )
        self.assertEqual(
            loc.determine_variable_relation("m_pCurrentItem->m_flag = 1;", "m_pCurrentItem"),
            "write",
        )
        self.assertEqual(
            loc.determine_variable_relation("delete m_pCurrentItem;", "m_pCurrentItem"),
            "delete",
        )
        self.assertEqual(
            loc.determine_variable_relation("if (m_pCurrentItem == nullptr)", "m_pCurrentItem"),
            "read",
        )
        self.assertEqual(
            loc.determine_variable_relation("m_renderSteps.Add(renderStep);", "m_renderSteps"),
            "write",
        )
        self.assertEqual(
            loc.determine_variable_relation("m_renderSteps.RemoveAll();", "m_renderSteps"),
            "write",
        )
        self.assertEqual(
            loc.determine_variable_relation("m_renderSteps.SetSize(0);", "m_renderSteps"),
            "write",
        )
        self.assertEqual(
            loc.determine_variable_relation("for (i=0;i<m_renderSteps.GetSize();i++)", "m_renderSteps"),
            "read",
        )

    def test_container_mutator_store_site_finds_build_add(self):
        """``.Add`` 写点须被 store-site / 关系分类命中，不能只搜赋值号。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            route_cpp = root / "demo_route_result.cpp"
            route_cpp.write_text(
                "class CRoute {\n"
                "public:\n"
                "    Array<RenderStep> GetDrawSteps() { return m_renderSteps; }\n"
                "    void Build() {\n"
                "        m_renderSteps.Add(renderStep);\n"
                "    }\n"
                "    Array<RenderStep> m_renderSteps;\n"
                "};\n",
                encoding="utf-8",
            )
            control_cpp = root / "demo_engine_control.cpp"
            control_cpp.write_text(
                "Array<RenderStep> DemoEngineControl::GetDrawSteps() {\n"
                "    Array<RenderStep> routes;\n"
                "    if (m_pCurrentItem) {\n"
                "        routes = m_pCurrentItem->GetDrawSteps();\n"
                "    }\n"
                "    return routes;\n"
                "}\n",
                encoding="utf-8",
            )
            svc = CodeLocatorService(LocatorConfig(max_shared_var_related_functions=12))
            hits = svc.find_variable_usages(
                ["m_renderSteps"],
                "DemoEngineControl::GetDrawSteps",
                [str(root)],
                crash_local_files=[str(control_cpp), str(route_cpp)],
                owner_class="CRoute",
                owner_member_fields=["m_renderSteps"],
                owner_definition_files=[str(route_cpp)],
            )
            write_hits = [
                h for h in hits
                if str(getattr(h, "relation", "")).lower() in {"write", "assign", "delete"}
            ]
            self.assertTrue(
                any("Build" in str(getattr(h, "name", "")) for h in write_hits),
                msg=f"expected DemoRoute::Build write hit, got={[(h.name, h.relation) for h in hits]}",
            )

    def test_forbidden_live_getrendersteps_patch(self):
        """只锁指针仍拷 live getter 的容器返回补丁必须被拒绝。"""
        from services.code_fixer import is_forbidden_patch

        bad = (
            "Array<RenderStep> DemoEngineControl::GetDrawSteps() {\n"
            "    m_clRouteInfoMutex.Lock(V_INFINITY_WAIT);\n"
            "    Array<RenderStep> routes;\n"
            "    if (m_pCurrentItem) {\n"
            "        routes = m_pCurrentItem->GetDrawSteps();\n"
            "    }\n"
            "    m_clRouteInfoMutex.Unlock();\n"
            "    return routes;\n"
            "}\n"
        )
        err = is_forbidden_patch(
            "Array<RenderStep> DemoEngineControl::GetDrawSteps()",
            bad,
        )
        self.assertIsNotNone(err)
        self.assertIn("live getter", str(err))

        good = (
            "Array<RenderStep> DemoEngineControl::GetDrawSteps() {\n"
            "    m_clRouteInfoMutex.Lock(V_INFINITY_WAIT);\n"
            "    Array<RenderStep> routes = m_arrDrawStepsSnapshot;\n"
            "    m_clRouteInfoMutex.Unlock();\n"
            "    return routes;\n"
            "}\n"
        )
        self.assertIsNone(
            is_forbidden_patch(
                "Array<RenderStep> DemoEngineControl::GetDrawSteps()",
                good,
            )
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
                    f"    if (m_pCurrentItem) {{\n"
                    f"        (void)m_pCurrentItem->GetSize();\n"
                    f"    }}\n"
                    f"}}\n"
                )
            cpp.write_text(
                "\n".join(read_funcs)
                + "\n"
                + "void HandleClearItem(Engine* pclThis) {\n"
                + "    pclThis->m_pCurrentItem = nullptr;\n"
                + "}\n"
                + "void GetDrawSteps() {\n"
                + "    if (m_pCurrentItem) {\n"
                + "        routes = m_pCurrentItem->GetDrawSteps();\n"
                + "    }\n"
                + "}\n",
                encoding="utf-8",
            )
            svc = CodeLocatorService(LocatorConfig(max_shared_var_related_functions=12))
            hits = svc.find_variable_usages(
                ["m_pCurrentItem"],
                "GetDrawSteps",
                [str(root)],
                crash_local_files=[str(cpp)],
                owner_class="Engine",
                owner_member_fields=["m_pCurrentItem"],
                owner_definition_files=[str(cpp)],
            )
            rels = {(h.name, h.relation) for h in hits}
            self.assertTrue(
                any(rel == "assign" and "HandleClearItem" in name for name, rel in rels),
                f"expected assign hit for HandleClearItem, got {rels}",
            )
            # 截断后仍应保留至少一条写类
            self.assertTrue(any(h.relation in ("assign", "write", "delete") for h in hits))

    def test_shared_var_write_is_must_tag_for_prompt(self):
        self.assertIn("共享变量写", _MUST_TAGS)
        rec = {
            "norm_id": "func|x|HandleClearItem",
            "tags": {"共享变量写"},
            "node": {
                "signature": "void HandleClearItem()",
                "file": "/tmp/a.cpp",
                "snippet": ["void HandleClearItem() {", "}"],
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
            "void DemoEngineControl::HandleClearItem(\n"
            "                                DemoEngineControl* pclThis ,\n"
            "                                const ClearItemMessage& stMessage )\n"
            "{\n"
            "    pclThis->m_pCurrentItem = nullptr;\n"
            "}\n"
        )
        self.assertTrue(is_plausible_function_code_head(code))


if __name__ == "__main__":
    unittest.main()
