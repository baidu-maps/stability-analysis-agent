#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""崩溃行参数求值被调（arg_eval）展开与图边方向单测。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from tools.code_content_provider_tool import (  # noqa: E402
    CallChainFunction,
    CodeContentProvider,
    CrashFunction,
    CrashSummary,
)


class TestCrashLineArgEvalCallees(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = CodeContentProvider()

    def test_classify_arg_eval_nested_in_call_args(self) -> None:
        blob = (
            "bool done = initWithShaderSources(\n"
            "    GetShaderValue(m_shaderType, ShaderValueType_VertShader),\n"
            "    GetShaderValue(m_shaderType, ShaderValueType_FragShader),\n"
            "    m_shaderType);\n"
        )
        classified = self.provider._classify_call_names_in_blob(blob, "compile")
        by_name = {name: kind for name, kind in classified}
        self.assertEqual(by_name.get("initWithShaderSources"), "statement")
        self.assertEqual(by_name.get("GetShaderValue"), "arg_eval")

    def test_find_crash_line_callee_cross_file_arg_eval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            crash_cpp = root / "gl_render_shader.cpp"
            helper_cpp = root / "shader_cache_helper.cpp"
            crash_cpp.write_text(
                """
bool initWithShaderSources(const char* a, const char* b, int t) {
    return a && b;
}

bool compile() {
    if (m_ProgramID == 0) {
        bool done = initWithShaderSources(
            GetShaderValue(1, 0),
            GetShaderValue(1, 1),
            1);
        return done;
    }
    return true;
}
""".lstrip(),
                encoding="utf-8",
            )
            helper_cpp.write_text(
                """
#include <string>
std::string g_strShaderVert;

const char* GetShaderValue(int shaderType, int shaderValueType) {
    g_strShaderVert = "vert";
    return g_strShaderVert.c_str();
}
""".lstrip(),
                encoding="utf-8",
            )
            crash_func = CrashFunction(
                name="compile",
                signature="bool compile()",
                snippet=crash_cpp.read_text(encoding="utf-8").splitlines(),
                crash_line="bool done = initWithShaderSources(",
            )
            items = self.provider._find_crash_line_callee_functions(
                crash_func, str(crash_cpp), [str(root)]
            )
            by_name = {it.name: it for it in items}
            self.assertIn("initWithShaderSources", by_name)
            self.assertEqual(
                by_name["initWithShaderSources"].chain_origin, "crash_line_callee"
            )
            self.assertIn("GetShaderValue", by_name)
            self.assertEqual(by_name["GetShaderValue"].chain_origin, "arg_eval_callee")
            self.assertTrue(
                str(by_name["GetShaderValue"].file).endswith("shader_cache_helper.cpp")
            )
            body = "\n".join(by_name["GetShaderValue"].snippet)
            self.assertIn("g_strShaderVert", body)

    def test_truncate_arg_eval_keeps_lifetime_hits(self) -> None:
        # 前半是静态字面量 return，后半才出现全局 buffer —— 旧逻辑只留前 120 行会丢证据。
        head = ["const char* GetShaderValue(int a, int b) {", "  switch (a) {"]
        mid = [f'    case {i}: return "static_{i}";' for i in range(200)]
        tail = [
            "    case 999:",
            '      g_strShaderVert = "anim";',
            "      return g_strShaderVert.c_str();",
            "  }",
            "  return nullptr;",
            "}",
        ]
        snippet = head + mid + tail
        out = CodeContentProvider._truncate_arg_eval_snippet(snippet)
        text = "\n".join(out)
        self.assertLess(len(out), len(snippet))
        self.assertIn("g_strShaderVert", text)
        self.assertIn("c_str()", text)
        self.assertIn("lifetime-risk hits", text)

    def test_build_graph_arg_eval_edge_direction(self) -> None:
        crash_func = CrashFunction(
            name="compile",
            signature="bool compile()",
            snippet=["bool compile() {", "  return initWithShaderSources(GetShaderValue(1,0), \"\", 1);", "}"],
            crash_line="  return initWithShaderSources(GetShaderValue(1,0), \"\", 1);",
        )
        summary = CrashSummary(
            file="/tmp/a.cpp",
            function="compile",
            crash_line_number=2,
            stack_address="0x0",
            error_type="SIGABRT",
            thread_id="1",
        )
        callees = [
            CallChainFunction(
                name="initWithShaderSources",
                file="/tmp/a.cpp",
                snippet=["bool initWithShaderSources(const char* a, const char* b, int t)"],
                chain_origin="crash_line_callee",
            ),
            CallChainFunction(
                name="GetShaderValue",
                file="/tmp/b.cpp",
                snippet=["const char* GetShaderValue(int a, int b)"],
                chain_origin="arg_eval_callee",
            ),
        ]
        graph = self.provider._build_crash_graph(
            crash_summary=summary,
            crash_file="/tmp/a.cpp",
            crash_func=crash_func,
            direct_call_crash_fun=[],
            pre_call_fun_in_same_parent_fun=[],
            use_same_var_related_fun=[],
            sibling_member_func_in_same_class=[],
            thread_context=[],
            os_type="android",
            crash_line_callee_funs=callees,
        )
        roles = {
            n.name: n.role
            for n in graph.nodes
            if n.type == "function" and n.name in ("GetShaderValue", "initWithShaderSources")
        }
        self.assertEqual(roles.get("GetShaderValue"), "arg_eval_callee")
        self.assertEqual(roles.get("initWithShaderSources"), "crash_line_callee")

        edge_types = {(e.from_id, e.to_id, e.type) for e in graph.edges}
        # crash → GetShaderValue via arg_eval
        self.assertTrue(
            any(
                "compile" in fr and "GetShaderValue" in to and et == "arg_eval"
                for fr, to, et in edge_types
            ),
            edge_types,
        )
        # crash → initWith via calls_direct
        self.assertTrue(
            any(
                "compile" in fr and "initWithShaderSources" in to and et == "calls_direct"
                for fr, to, et in edge_types
            ),
            edge_types,
        )
        # GetShaderValue feeds_arg_of crash
        self.assertTrue(
            any(
                "GetShaderValue" in fr and "compile" in to and et == "feeds_arg_of"
                for fr, to, et in edge_types
            ),
            edge_types,
        )


class TestArgEvalPromptMustTag(unittest.TestCase):
    def test_arg_eval_tag_is_must(self) -> None:
        from tools._prompt_context_filter import filter_prompt_function_records

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

        records = [
            _rec("Crash::compile()", ["崩溃函数"], 0),
            _rec("GetShaderValue()", ["参数求值被调", "崩溃行被调"], 1),
            _rec("Noise::find()", ["调用链"], 5),
        ]
        included, index = filter_prompt_function_records(records, max_functions=4)
        sigs = [(r["node"] or {}).get("signature") for r in included]
        self.assertIn("GetShaderValue()", sigs)
        self.assertNotIn("Noise::find()", sigs)


if __name__ == "__main__":
    unittest.main()
