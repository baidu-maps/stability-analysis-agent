#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
code_content_provider（当前版本）单元测试（精简版）

目标：验证“add2line 解析结果 + code_root”能够产出当前结构化数据与 analysis_guidance。
说明：历史旧版接口（CodeContext/CrashContext/PromptContent 等）已不再维护，
这里不再用旧接口来约束当前实现，避免为了兼容测试而改动核心逻辑。
"""

import unittest
import json
import sys
import tempfile
from pathlib import Path

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

try:
    from tools.code_content_provider_tool import CodeContentProvider
except ImportError:  # 兼容部分环境仍使用旧包名
    from stability_analyzer_agent.tools import CodeContentProvider  # type: ignore

try:
    from workflows.crash_analysis_workflow import iOSCrashAnalyzeWorkflow
except ImportError:
    iOSCrashAnalyzeWorkflow = None  # type: ignore


@unittest.skipUnless(iOSCrashAnalyzeWorkflow is not None, "workflow import failed")
class TestBuildPromptFinalTip(unittest.TestCase):
    def test_shared_var_sections_in_final_tip(self):
        crash_id = "func|/tmp/x.cpp|void Foo::bar()"
        code_ctx = {
            "crash_summary": {
                "node_id": crash_id + " {",
                "crash_line_number": 10,
                "crash_line_code": "x = y;",
                "error_type": "SIGSEGV",
            },
            "graph": {
                "nodes": [
                    {
                        "id": crash_id,
                        "type": "function",
                        "signature": "void Foo::bar()",
                        "file": "/tmp/x.cpp",
                        "snippet": ["void Foo::bar() {", "  head = 0;", "}"],
                    },
                    {
                        "id": "func|/tmp/x.cpp|void Foo::baz()",
                        "type": "function",
                        "signature": "void Foo::baz()",
                        "file": "/tmp/x.cpp",
                        "snippet": ["void Foo::baz() {", "  tail = 0;", "}"],
                    },
                    {
                        "id": "var|head",
                        "type": "variable",
                        "name": "head",
                        "signature": "int* head;",
                    },
                ],
                "edges": [
                    {
                        "type": "use_shared_var",
                        "from_id": crash_id,
                        "to_id": "var|head",
                        "relation": "write",
                    },
                    {
                        "type": "use_shared_var",
                        "from_id": "func|/tmp/x.cpp|void Foo::baz()",
                        "to_id": "var|head",
                        "relation": "write",
                    },
                ],
                "call_chain_from_code": [{"nodes": [crash_id]}],
            },
            "code_context_options": {"max_shared_var_related_functions": 10},
        }
        resolved = {
            "resolved_frames": [
                {
                    "resolved_function": "bar()",
                    "resolved_file": "x.cpp",
                    "resolved_line": 10,
                }
            ]
        }
        wf = iOSCrashAnalyzeWorkflow()
        text = wf._build_prompt_final_tip({}, resolved, code_ctx, problem={})
        self.assertIn("### 共享成员与写路径交叉（崩溃点关联）", text)
        self.assertIn("head", text)
        self.assertIn("### 函数源码（按函数唯一输出）", text)
        self.assertIn("- 来源:", text)
        self.assertIn("void Foo::baz()", text)


class TestCodeContentProviderV2(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.code_root = Path(self.test_dir)

        (self.code_root / "main.cpp").write_text(
            (
                '#include <iostream>\n'
                '#include "my_lib.h"\n'
                "\n"
                "int main() {\n"
                "    call_example_function();\n"
                "    return 0;\n"
                "}\n"
            ),
            encoding="utf-8",
        )

        (self.code_root / "my_lib.h").write_text(
            (
                "#pragma once\n"
                'extern "C" { void call_example_function(); }\n'
            ),
            encoding="utf-8",
        )

        self.my_lib_cpp = self.code_root / "my_lib.cpp"
        self.my_lib_cpp.write_text(
            (
                '#include "my_lib.h"\n'
                "void call_example_function() {\n"
                "    int* ptr = nullptr;\n"
                "    *ptr = 42;  // crash\n"
                "}\n"
            ),
            encoding="utf-8",
        )

        crash_line = 1
        for i, line in enumerate(self.my_lib_cpp.read_text(encoding="utf-8").splitlines(), start=1):
            if "*ptr = 42" in line:
                crash_line = i
                break

        self.sample_add2line_json = json.dumps(
            {
                "resolved_frames": [
                    {
                        "address": "0x0000000100001234",
                        "resolved_function": "call_example_function()",
                        "resolved_file": "my_lib.cpp",
                        "resolved_line": crash_line,
                    },
                    {
                        "address": "0x0000000100001567",
                        "resolved_function": "main()",
                        "resolved_file": "main.cpp",
                        "resolved_line": 1,
                    },
                ],
                "os_type": "macos",
                "success_count": 2,
                "total_count": 2,
                "library_path": "/path/to/library",
            },
            ensure_ascii=False,
        )

    def tearDown(self):
        import shutil

        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_is_supported_file(self):
        p = CodeContentProvider(code_parser_backend="regex")
        self.assertTrue(p._is_supported_file("a.cpp"))
        self.assertTrue(p._is_supported_file("a.h"))
        self.assertFalse(p._is_supported_file("a.zip"))

    def test_code_content_provider_v2_output(self):
        p = CodeContentProvider(code_parser_backend="regex")
        out = json.loads(p.code_content_provider(self.sample_add2line_json, str(self.code_root)))

        for key in [
            "crash_summary",
            "graph",
            "code_parser_backend",
        ]:
            self.assertIn(key, out)

        self.assertIn("nodes", out["graph"])
        self.assertEqual(out["code_parser_backend"], "regex")
        cs = out["crash_summary"]
        self.assertIn("crash_line_code", cs)
        self.assertIn("node_id", cs)
        self.assertIn("my_lib.cpp", cs["node_id"])

    def test_merge_class_field_usage_into_shared_vars(self):
        """成员函数内未写 this-> 的成员名，应能从类定义合并进共享变量候选。"""
        root = Path(self.test_dir) / "cls_merge"
        root.mkdir(parents=True, exist_ok=True)
        (root / "Foo.h").write_text(
            "class Foo {\npublic:\n    int head;\n    int tail;\n};\n",
            encoding="utf-8",
        )
        cpp = root / "Foo.cpp"
        cpp.write_text(
            "void Foo::bar() {\n    head = tail;\n}\n",
            encoding="utf-8",
        )
        p = CodeContentProvider(code_parser_backend="regex")
        merged = p._merge_class_field_usage_into_shared_vars(
            [],
            "head = tail;",
            "Foo::bar",
            str(cpp),
            [str(root.resolve())],
        )
        self.assertIn("head", merged)
        self.assertIn("tail", merged)


if __name__ == "__main__":
    unittest.main()


