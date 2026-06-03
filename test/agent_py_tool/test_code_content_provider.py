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
        self.assertIn("### 函数源码（按置信度筛选", text)
        self.assertIn("- 来源:", text)
        self.assertIn("void Foo::baz()", text)

    def test_add2line_stack_frames_included_in_final_tip(self):
        crash_id = "func|/tmp/Crash.h|bool Crash::hit() const"
        stack_b = "func|/tmp/Caller.cpp|void Caller::run()"
        stack_c = "func|/tmp/Util.h|void Util::cleanup()"
        code_ctx = {
            "crash_summary": {
                "node_id": crash_id + " {",
                "crash_line_number": 10,
                "crash_line_code": "return flag;",
                "error_type": "SIGSEGV",
            },
            "graph": {
                "nodes": [
                    {
                        "id": crash_id,
                        "type": "function",
                        "signature": "bool Crash::hit() const",
                        "file": "/tmp/Crash.h",
                        "snippet": ["bool Crash::hit() const { return flag; }"],
                    },
                    {
                        "id": stack_b,
                        "type": "function",
                        "signature": "void Caller::run()",
                        "file": "/tmp/Caller.cpp",
                        "snippet": ["void Caller::run() {", "  hit();", "}"],
                    },
                    {
                        "id": stack_c,
                        "type": "function",
                        "signature": "void Util::cleanup()",
                        "file": "/tmp/Util.h",
                        "snippet": ["void Util::cleanup() {", "  run();", "}"],
                    },
                ],
                "edges": [],
                "call_chain_from_code": [
                    {
                        "id": "path_0",
                        "nodes": [crash_id],
                        "description": "inferred_from_add2line_stack_order",
                    }
                ],
                "call_chain_from_add2line": [
                    {
                        "thread_id": "t1",
                        "nodes": [crash_id, stack_b, stack_c],
                    }
                ],
                "stack_kept_original_indices": [0, 1, 2],
            },
        }
        resolved = {
            "resolved_frames": [
                {"resolved_function": "Crash::hit()", "resolved_file": "Crash.h", "resolved_line": 10},
                {"resolved_function": "Caller::run()", "resolved_file": "Caller.cpp", "resolved_line": 2},
                {"resolved_function": "Util::cleanup()", "resolved_file": "Util.h", "resolved_line": 1},
            ]
        }
        wf = iOSCrashAnalyzeWorkflow()
        text = wf._build_prompt_final_tip({}, resolved, code_ctx, problem={})
        self.assertIn("void Caller::run()", text)
        self.assertIn("void Util::cleanup()", text)
        self.assertIn("堆栈帧", text)

    def test_evidence_gate_message_in_final_tip(self):
        crash_id = "func|/tmp/Crash.h|void Crash::hit()"
        stack_b = "func|/tmp/Caller.cpp|void Caller::run()"
        code_ctx = {
            "crash_summary": {"node_id": crash_id + " {", "crash_line_number": 10},
            "graph": {
                "nodes": [
                    {
                        "id": crash_id,
                        "type": "function",
                        "signature": "void Crash::hit()",
                        "file": "/tmp/Crash.h",
                        "snippet": ["void Crash::hit() { x=1; }"],
                    },
                    {
                        "id": stack_b,
                        "type": "function",
                        "signature": "void Caller::run()",
                        "file": "/tmp/Caller.cpp",
                        "snippet": ["void Caller::run() { hit(); }"],
                    },
                ],
                "edges": [
                    {"type": "calls_stack_order", "from_id": stack_b, "to_id": crash_id},
                ],
                "evidence_summary": {
                    "auto_fix_allowed": False,
                    "has_calls_stack_order": True,
                    "auto_fix_block_reason": "test block",
                },
                "call_chain_from_code": [
                    {
                        "nodes": [crash_id, stack_b],
                        "inference": "inferred_from_add2line_stack_order",
                    }
                ],
                "call_chain_from_add2line": [{"thread_id": "t", "nodes": [crash_id, stack_b]}],
            },
        }
        wf = iOSCrashAnalyzeWorkflow()
        text = wf._build_prompt_final_tip({}, {"resolved_frames": []}, code_ctx)
        self.assertIn("auto_fix_allowed", text)
        self.assertIn("auto_fix_allowed=false", text)
        self.assertIn("calls_stack_order", text)

    def test_symbol_only_prompt_omits_crash_line_number_and_suspicious_snippet(self):
        crash_id = "func|/tmp/mtl.mm|void MTLVertexBuffer::createPrivateBuffer()"
        code_ctx = {
            "crash_summary": {
                "node_id": crash_id + " {",
                "crash_line_number": 9,
                "crash_line_code": '#include "mtl.h"',
                "error_type": "SIGSEGV",
                "crash_location_source": "from_log_deduce",
                "crash_line_note": "启发式展示，不是指令级精确崩溃位置。",
            },
            "graph": {
                "nodes": [
                    {
                        "id": crash_id,
                        "type": "function",
                        "signature": "void MTLVertexBuffer::createPrivateBuffer()",
                        "file": "/tmp/mtl.mm",
                        "snippet": ["void MTLVertexBuffer::createPrivateBuffer() {", "}"],
                    }
                ],
                "edges": [],
            },
        }
        resolved = {
            "resolved_frames": [
                {
                    "resolved_function": "MTLVertexBuffer::createPrivateBuffer()",
                    "resolved_file": None,
                    "resolved_line": None,
                }
            ]
        }
        wf = iOSCrashAnalyzeWorkflow()
        text = wf._build_prompt_final_tip({}, resolved, code_ctx, problem={})
        self.assertNotIn("crash_line_number:", text)
        self.assertNotIn("crash_location_source:", text)
        self.assertNotIn("crash_line_note:", text)
        self.assertNotIn("## 可疑代码片段", text)
        self.assertNotIn("可疑崩溃代码行", text)
        self.assertNotIn("#include", text)
        self.assertIn("## 崩溃点定位", text)
        self.assertIn("结论：未精确定位到 file:line 级崩溃行", text)
        self.assertNotIn("启发式展示，不是指令级精确崩溃位置。", text)
        self.assertNotIn("自栈顶向下优先", text)

    def test_add2line_prompt_includes_suspicious_snippet_without_summary_line_number(self):
        crash_id = "func|/tmp/Crash.cpp|void Crash::hit()"
        code_ctx = {
            "crash_summary": {
                "node_id": crash_id + " {",
                "crash_line_number": 42,
                "crash_line_code": "*ptr = 0;",
                "error_type": "SIGSEGV",
                "crash_location_source": "from_add2line",
            },
            "graph": {
                "nodes": [
                    {
                        "id": crash_id,
                        "type": "function",
                        "signature": "void Crash::hit()",
                        "file": "/tmp/Crash.cpp",
                        "snippet": ["void Crash::hit() {", "  *ptr = 0;", "}"],
                    }
                ],
                "edges": [],
            },
        }
        resolved = {
            "resolved_frames": [
                {
                    "resolved_function": "Crash::hit()",
                    "resolved_file": "/tmp/Crash.cpp",
                    "resolved_line": 42,
                }
            ]
        }
        wf = iOSCrashAnalyzeWorkflow()
        text = wf._build_prompt_final_tip({}, resolved, code_ctx, problem={})
        self.assertNotIn("- crash_line_number:", text)
        self.assertNotIn("crash_location_source:", text)
        self.assertIn("## 崩溃点定位", text)
        self.assertIn("/tmp/Crash.cpp:42", text)
        self.assertIn("*ptr = 0;", text)
        self.assertIn("结论：崩溃点已通过符号化堆栈关联到具体源码行", text)
        self.assertNotIn("## 可疑代码片段", text)

    def test_owner_class_context_not_in_prompt_summary(self):
        crash_id = "func|/tmp/mtl.mm|void MTLVertexBuffer::createPrivateBuffer()"
        code_ctx = {
            "crash_summary": {
                "node_id": crash_id + " {",
                "error_type": "SIGSEGV",
                "owner_class_context": {
                    "class_name": "MTLVertexBuffer",
                    "definition_file": "/tmp/mtl_render_vertex_buffer.mm",
                    "member_fields": ["VertexFormatInvalid", "_buffer"],
                    "class_body_excerpt": [],
                    "skeleton": [],
                },
            },
            "graph": {
                "nodes": [
                    {
                        "id": crash_id,
                        "type": "function",
                        "signature": "void MTLVertexBuffer::createPrivateBuffer()",
                        "file": "/tmp/mtl.mm",
                        "snippet": ["void MTLVertexBuffer::createPrivateBuffer() {", "}"],
                    }
                ],
                "edges": [],
            },
        }
        wf = iOSCrashAnalyzeWorkflow()
        text = wf._build_prompt_final_tip({}, {"resolved_frames": []}, code_ctx, problem={})
        self.assertNotIn("### 崩溃所属类型/类上下文", text)
        self.assertNotIn("definition_file:", text)
        self.assertNotIn("member_fields:", text)
        self.assertNotIn("- class_name:", text)

    def test_fix_output_prompt_omits_no_change_optional_sections(self):
        crash_id = "func|/tmp/Crash.cpp|void Crash::hit()"
        code_ctx = {
            "crash_summary": {"node_id": crash_id + " {", "error_type": "SIGSEGV"},
            "graph": {
                "nodes": [
                    {
                        "id": crash_id,
                        "type": "function",
                        "signature": "void Crash::hit()",
                        "file": "/tmp/Crash.cpp",
                        "snippet": ["void Crash::hit() {", "}"],
                    }
                ],
                "edges": [],
            },
        }
        wf = iOSCrashAnalyzeWorkflow()
        text = wf._build_prompt_final_tip({}, {"resolved_frames": []}, code_ctx, problem={})
        self.assertNotIn("**可选提供**", text)
        self.assertNotIn("无需修改但与根因相关", text)
        self.assertNotIn("#### 无需修改但关键相关的函数", text)
        self.assertIn("「需要修改的函数」与「修复代码」必须一一对应", text)
        self.assertIn("**禁止**单独列出「无需修改的函数」章节", text)


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

    def test_objc_mm_function_snippet_excludes_namespace_macro(self):
        """readlines + 函数体提取不应产生双换行，snippet 不得从 NAMESPACE 宏起跨函数。"""
        mm = self.code_root / "mtl_render_vertex_buffer.mm"
        mm.write_text(
            '#include "mtl_render_vertex_buffer.h"\n\n'
            "NAMESPACE_BAIDU_VI_BEGIN\n\n"
            "void MTLVertexBuffer::createPrivateBuffer(id<MTLDevice> device, id<MTLCommandQueue> commandQueue, id<MTLBuffer> sharedBuffer)\n"
            "{\n"
            "    @autoreleasepool\n"
            "    {\n"
            "        id<MTLCommandBuffer> cmd_buffer = [commandQueue commandBuffer];\n"
            "    }\n"
            "}\n\n"
            "MTLVertexBuffer::MTLVertexBuffer(id<MTLDevice> device, size_t len, StorageMode type)\n"
            "{\n"
            "    if (device) {\n"
            "        _buffer = nil;\n"
            "    }\n"
            "}\n\n"
            "NAMESPACE_BAIDU_VI_END\n",
            encoding="utf-8",
        )
        sig = (
            "void MTLVertexBuffer::createPrivateBuffer(id<MTLDevice> device, "
            "id<MTLCommandQueue> commandQueue, id<MTLBuffer> sharedBuffer)"
        )
        p = CodeContentProvider(code_parser_backend="regex")
        p.current_code_roots = [str(self.code_root.resolve())]
        cf = p._extract_crash_function(sig, str(mm.resolve()), 5, [str(self.code_root.resolve())])
        self.assertIsNotNone(cf)
        self.assertEqual(cf.snippet_start_line, 5)
        self.assertEqual(cf.snippet_end_line, 11)
        self.assertTrue(cf.snippet[0].startswith("void MTLVertexBuffer::createPrivateBuffer"))
        self.assertFalse(any("NAMESPACE_BAIDU_VI_BEGIN" in ln for ln in cf.snippet))
        self.assertFalse(any("size_t len" in ln for ln in cf.snippet))
        self.assertEqual(len(cf.snippet), cf.snippet_end_line - cf.snippet_start_line + 1)

        refined_snippet, ss, se = p._refine_function_snippet_bounds(
            str(mm.resolve()), cf.snippet, sig, cf.snippet_start_line, cf.snippet_end_line
        )
        self.assertEqual(ss, 5)
        self.assertEqual(se, 11)
        self.assertEqual(refined_snippet[0], cf.snippet[0])


if __name__ == "__main__":
    unittest.main()


